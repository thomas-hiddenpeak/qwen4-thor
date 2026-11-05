"""Independently rebuild the bounded deployment result from preserved bytes."""
import argparse
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_deploy import stage as s

ARCHIVE = ROOT / 'docs/evidence/serve-bounded-deploy-20261105'


def audit(read):
    def obj(name):
        return json.loads(read(name))
    plan, result = obj('plan.json'), obj('execution/result.json')
    previous = obj('original/execution/result.json')
    s.pkg.require(previous['status'] == 'FAILED' and
        previous['error'] == "KeyError('q4t_cuda_quota_live_bytes')" and
        previous['counts'] == dict(starts=1, generation_attempts=4, controls=3) and
        previous['cleanup_confirmed'], 'original failure changed')
    s.pkg.require(result['status'] == 'PASS_BOUNDED_DEPLOYMENT' and
        result['cleanup_confirmed'] and result['counts']['starts'] == 1 and
        result['counts']['generation_attempts'] == 2 and
        result['counts']['controls'] <= 80 and result['seconds'] < 240 and
        result['seconds'] + previous['seconds'] < 420, 'stage result')
    cpu = obj('cpu-01/result.json')
    s.pkg.require(cpu['passed'] and cpu['models'] == cpu['http'] == 0, 'CPU gate')
    old_terminal = obj('original/execution/managed-run/terminal.json')
    s.pkg.require(old_terminal['state'] == 'STOPPED' and old_terminal['cleanup_confirmed'] and
                  old_terminal['guard']['status'] == 'COMPLETED', 'original lifecycle')
    quota = s.cuda_accounting(obj('original/execution/business/business-final-http.json')['body'])
    terminal = obj('execution/managed-run/terminal.json')
    s.protected_terminal(terminal)
    s.pkg.require(terminal['worker']['replayed_requests'] == 0 and
                  terminal['host_resource_cleanup']['scope_removed'], 'replay or host residual')
    final = obj('execution/managed-run/resources-final.json')
    s.pkg.require(final['payload_removed'] and final['violation'] is None and
                  not final['final']['members'], 'payload cleanup')
    before = obj('execution/before-pressure.json')
    m = before['metrics']
    s.pkg.require(m['q4t_queued_requests'] == 1 and m['q4t_seq_slots_free'] == 0 and
        m['q4t_structured_workers_active'] == 1 and m['q4t_structured_preparing'] == 0,
        'prepared queue evidence missing')
    s.pkg.require(before['host']['policy'] == s.POLICY if 'policy' in before['host'] else
                  obj('execution/managed-run/resources.json')['policy'] == s.POLICY,
                  'host policy differs')
    rows = [json.loads(line) for line in read('execution/pressure.jsonl').splitlines()]
    s.pkg.require(rows[-1]['final'] and rows[-1]['allocated'] == 0 and
                  rows[-1]['error'] is None, 'pressure helper cleanup')
    samples = [row[key] for row in rows for key in ('before', 'after') if key in row]
    peak = max(row['allocated'] for row in rows)
    available = min(row['MemAvailable'] for row in samples)
    free = min(row['MemFree'] for row in samples)
    s.pkg.require(peak <= s.pressure.MAX_BYTES and available >= s.pressure.MIN_AVAILABLE and
        free >= s.pressure.MIN_FREE, 'pressure safety bound')
    trip = obj('execution/pressure-trigger.json')
    s.pkg.require(trip['monotonic'] > before['monotonic'] and
        trip['after']['MemAvailable'] < s.FLOOR_MIB*s.pressure.MIB-s.pressure.CHUNK,
        'real memory pressure missing')
    validator = s.Draft202012Validator(obj('schema.json'))
    for label in ('active', 'queued'):
        raw = read('execution/business/' + label + '-wire.bin')
        checked = s.stream_terminal(raw, graceful=True, validator=validator)
        s.pkg.require(checked == obj('execution/' + label + '-verified.json'), 'SSE proof differs')
    active = read('execution/business/active-wire.bin')
    s.pkg.require(any(any(c.get('delta', {}).get('content') for c in
        json.loads(line[6:]).get('choices', [])) for line in active.splitlines()
        if line.startswith(b'data: {')), 'no actual active output')
    responses = []
    for client, ceiling in (('alpha', 512), ('beta', 64)):
        tool = json.loads(obj('execution/business/project-' + client + '-tool-result.json')['content'])
        source = tool['source']
        s.pkg.require(tool['ok'] and source['path'] == 'docs/MODEL.md' and
            source['start_line'] == 1 and source['end_line'] == 24 and
            source['sha256'] == hashlib.sha256(read('model-document.md')).hexdigest(), 'tool source')
        for part in ('read', 'answer'):
            name = 'execution/business/project-' + client + '-' + part
            value = s.base.client.a.read_sse(io.BytesIO(read(name + '-wire.bin')))
            stored = obj(name + '-response.json')
            seconds = stored.pop('elapsed')
            s.pkg.require(stored == asdict(value) and
                value.usage['completion_tokens'] <= ceiling, 'business wire/budget')
            if part == 'answer':
                s.base.client.a.validate_answer(value.message['content'], {source['citation']}, 1)
                s.pkg.require('Qwen4ExpForConditionalGeneration' in value.message['content'], 'answer value')
            responses.append(dict(client=client, part=part, usage=value.usage, seconds=seconds))
    log = read('execution/managed-run/backend.log').decode()
    reaped = s.re.findall(r'grammar worker reaped=(\d+) ', log)
    old_log = read('original/execution/managed-run/backend.log').decode()
    old_reaped = s.re.findall(r'grammar worker reaped=(\d+) ', old_log)
    s.pkg.require(len(old_reaped) == 4 and all(n == '1' for n in old_reaped), 'original grammar cleanup')
    s.pkg.require(len(reaped) == 1 and reaped[0] == '1' and
                  'shutdown complete (0 in-flight remaining)' in log, 'native cleanup')
    frontend = obj('execution/front-1/terminal.json')
    s.pkg.require(frontend['state'] == 'FAILED' and frontend['cleanup_confirmed'] and
                  not frontend['automatic_reactivation'], 'gateway cleanup')
    guard = [json.loads(line) for line in read('execution/managed-run/lease/events.jsonl').splitlines()]
    s.pkg.require(guard[0]['event'] == 'lease_acquired' and guard[-1]['event'] == 'lease_released', 'guard lease')
    polls = [row for row in guard if row['event'] in ('monitor', 'preflight')]
    s.pkg.require(polls and all(not row['conflicts'] and not row['unreadable_processes']
                              for row in polls), 'guard environment')
    custody = [json.loads(line) for line in read('execution/custody/events.jsonl').splitlines()]
    s.pkg.require(custody and all(not r['observation']['conflicts'] and
        not r['observation']['unreadable_processes'] for r in custody) and
        obj('execution/custody/exit.json')['status'] == 'COMPLETED', 'custody environment')
    observed = obj('execution/stopped-inspection.json')
    s.pkg.require(observed['complete'] and observed['cleanup_observed'] and
        not observed['listeners'] and not observed['gpu']['pids'] and
        observed['lock']['state'] == 'AVAILABLE', 'remaining resource')
    elapsed = custody[-1]['monotonic'] - guard[0]['monotonic']
    old_guard = [json.loads(line) for line in read('original/execution/managed-run/lease/events.jsonl').splitlines()]
    old_custody = [json.loads(line) for line in read('original/execution/custody/events.jsonl').splitlines()]
    s.pkg.require(old_guard[0]['event'] == 'lease_acquired' and
        old_guard[-1]['event'] == 'lease_released' and
        obj('original/execution/custody/exit.json')['status'] == 'COMPLETED' and
        all(not r['conflicts'] and not r['unreadable_processes'] for r in old_guard
            if r['event'] in ('monitor', 'preflight')) and
        all(not r['observation']['conflicts'] and not r['observation']['unreadable_processes']
            for r in old_custody), 'original monitored lease')
    old_elapsed = old_custody[-1]['monotonic'] - old_guard[0]['monotonic']
    s.pkg.require(elapsed <= 180 and elapsed + old_elapsed <= 300, 'GPU window')
    return dict(status='ACCEPT_BOUNDED_DEPLOYMENT_SHORT', package=plan['package'],
        deployment=plan['deployment'], rollback=plan['rollback'], models=2,
        responses=responses, real_tools=2, interrupted_streams=2, grammar_reaped=5,
        cpu_checks=13 + cpu['tests_run'], controls=result['counts']['controls'] + 3,
        driver_seconds=result['seconds'] + previous['seconds'],
        monitored_lock_seconds=elapsed + old_elapsed, separate_leases=2,
        original_failure_preserved=True, reused_responses=4, cuda_accounting=quota,
        pressure_peak_bytes=peak, minimum_available_bytes=available, minimum_free_bytes=free,
        host_peak_bytes=final['final'].get('memory.peak'),
        critical_memory_mib=s.FLOOR_MIB, physical_ram_hard_limit=False,
        actual_lan_accepted=False, long_run=False, default_changed=False)


def archive():
    from tools.serve_deploy import continuation as follow
    follow.configure()
    plan = follow.check()
    selected = {}
    for folder, prefix in ((s.OUT, ''), (follow.ORIGINAL / 'execution', 'original/')):
        for path in folder.rglob('*'):
            if path.is_file() and not path.name.endswith('-request.json') and path.suffix in (
                    '.json', '.jsonl', '.log', '.bin'):
                selected[prefix + str(path.relative_to(folder.parent))] = path.read_bytes()
                if prefix and path.parent == folder / 'business' and path.name.startswith('project-'):
                    selected['execution/business/' + path.name] = path.read_bytes()
    for relative in ('plan.json', 'cpu-01/result.json', 'cpu-01/tests.log'):
        selected[relative] = (s.BASE / relative).read_bytes()
    for relative in ('plan.json', 'cpu-01/result.json', 'cpu-01/tests.log',
                     'cpu-02/result.json', 'cpu-02/tests.log',
                     'deployment/profile.json', 'backend-package/manifest.json'):
        selected['original/' + relative] = (follow.ORIGINAL / relative).read_bytes()
    for path in (follow.ORIGINAL / 'frozen-source').rglob('*'):
        if path.is_file():
            selected['original-source/' + str(path.relative_to(follow.ORIGINAL / 'frozen-source'))] = path.read_bytes()
    selected['schema.json'] = (ROOT / 'docs/serve-transport-v1.schema.json').read_bytes()
    selected['model-document.md'] = (ROOT / 'docs/MODEL.md').read_bytes()
    for path in (ROOT / 'tools/serve_deploy').glob('*'):
        if path.is_file():
            selected['source/' + path.name] = path.read_bytes()
    secrets = [s.pkg.read(plan['policy'])['gateway_token']]
    secrets += [c['api_key'] for c in s.pkg.read(plan['frontend'])['clients']]
    for path in (s.OUT / 'business').glob('*-request.json'):
        body = s.pkg.read(path).get('body') or {}
        if body.get('cancel_token'):
            secrets.append(body['cancel_token'])
    for name, raw in selected.items():
        s.pkg.require(b'PRIVATE KEY' not in raw and
                      not any(secret.encode() in raw for secret in secrets), 'secret in ' + name)
    result = audit(selected.__getitem__)
    ARCHIVE.mkdir(parents=True)
    records = []
    with tarfile.open(ARCHIVE / 'records.tar.gz', 'w:gz') as out:
        for name, raw in sorted(selected.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(raw), 0o600
            out.addfile(info, io.BytesIO(raw))
            records.append(dict(member=name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
    s.pkg.save(ARCHIVE / 'manifest.json', dict(records=records,
        archive_sha256=s.pkg.sha(ARCHIVE / 'records.tar.gz')))
    s.pkg.save(ARCHIVE / 'result.json', result)
    s.pkg.save(s.BASE / 'accepted-package.json', dict(package=plan['package'],
        acceptance=result['status'], deployment=plan['deployment'],
        archive=str(ARCHIVE), archive_sha256=s.pkg.sha(ARCHIVE / 'records.tar.gz')))
    return verify()


def verify():
    manifest = s.pkg.read(ARCHIVE / 'manifest.json')
    s.pkg.require(s.pkg.sha(ARCHIVE / 'records.tar.gz') == manifest['archive_sha256'], 'archive hash')
    expected = {row['member']: row for row in manifest['records']}
    selected = {}
    with tarfile.open(ARCHIVE / 'records.tar.gz', 'r:gz') as archive:
        for member in archive:
            s.pkg.require(member.isfile() and member.name in expected and
                          member.name not in selected, 'unexpected archived record')
            raw = archive.extractfile(member).read()
            row = expected[member.name]
            s.pkg.require(len(raw) == row['bytes'] and
                hashlib.sha256(raw).hexdigest() == row['sha256'], 'record identity')
            selected[member.name] = raw
    s.pkg.require(selected.keys() == expected.keys(), 'missing records')
    result = audit(selected.__getitem__)
    s.pkg.require(result == s.pkg.read(ARCHIVE / 'result.json'), 'rebuilt result differs')
    return dict(passed=True, records=len(selected), result=result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('archive', 'verify'))
    print(json.dumps(globals()[parser.parse_args().action]()))
