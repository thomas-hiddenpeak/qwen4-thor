"""Independent wire/ledger replay and lossless bounded-deadline evidence."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import deadline_stage as s, engine, store, migration
from tools.serve_service import package as pkg
from tools.serve_cuda_quota import correction as custody

DEST = ROOT/'docs/evidence/serve-task-deadline-20261104'
READERS = (*engine.RUNTIME_FILES, *(s.HERE+name for name in
    ('deadline_stage.py', 'deadline_report.py', 'deadline_probes.py', 'deadline_cpu.py',
     'deadline_cpu_repair.py', 'deadline_driver_cpu.py', 'test_transport_deadline.py', 'protocol_stage.py',
     'migration_worker.py', 'test_tasks.py', 'test_trust.py')))
sha = lambda raw: hashlib.sha256(raw).hexdigest()
require = pkg.require


def postflight():
    plan = s.check()
    require(pkg.read(s.OUT/'lease/exit.json')['status'] == 'COMPLETED', 'guard not terminal successful')
    out = s.BASE/'postflight'
    out.mkdir(mode=0o700)
    s.prior.s.a.require_idle(out/'gpu.json')
    lock = s.prior.s.a.guard.common_lock()
    info = lock.stat()
    require((info.st_dev, info.st_ino) == (66305, 3294067) and
            s.prior.s.a.lock_available(lock), 'original lock not released')
    owners = []
    for path in sorted(s.OUT.rglob('identity.json')):
        owner = pkg.read(path)
        try: current = custody.proc(owner['pid'])
        except FileNotFoundError: current = None
        require(current is None or current['start_ticks'] != owner['start_ticks'], 'owned process remains')
        owners.append(dict(pid=owner['pid'], start_ticks=owner['start_ticks'], gone=True))
    require(len(owners) == 4, 'backend/gateway/two application identities')
    for port in (s.PORT, s.TLS_PORT):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', port))
    listeners = subprocess.check_output(['ss', '-ltn', f'( sport = :{s.PORT} or sport = :{s.TLS_PORT} )'], text=True)
    (out/'listeners.txt').write_text(listeners)
    require(len(listeners.splitlines()) == 1, 'retained listener')
    unit = 'soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    raw = subprocess.check_output(['systemctl', '--user', 'show', unit,
        '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID'], text=True)
    (out/'soar-unit.txt').write_text(raw)
    require(dict(line.split('=', 1) for line in raw.splitlines()) ==
        dict(LoadState='masked', ActiveState='inactive', SubState='dead', MainPID='0'), 'restore service changed')
    containers = subprocess.check_output(['sudo', '-n', 'docker', 'ps', '--format', '{{.ID}} {{.Names}}'], text=True)
    (out/'containers.txt').write_text(containers)
    require(not containers.strip(), 'running container')
    pkg.save(out/'result.json', dict(passed=True, owners=owners,
        lock=dict(dev=info.st_dev, inode=info.st_ino, released=True),
        ports=[s.PORT, s.TLS_PORT], default_unchanged=True, day_plans=plan['day_plans'], long_run=False))


def audit(data):
    read = lambda name: json.loads(data[name])
    plan = read('plan.json')
    first, fixed = (read(name+'/result.json') for name in ('cpu', 'cpu-repair'))
    require(first['tests_run'] == len(set(first['names'])) == 16 and first['failures'] == 2 and
        not first['errors'] and not first['skipped'] and fixed['passed'] and fixed['tests_run'] == 2 and
        not fixed['failures'] and not fixed['errors'] and not fixed['skipped'], 'CPU outcomes')
    for name, digest in plan['receipts'].items():
        require(sha(data[name+'/result.json']) == digest, 'original receipt changed')
    for name in ('cpu', 'cpu-repair'):
        for path, digest in read(name+'/result.json')['sources'].items():
            require(sha(data[name+'/source/'+path]) == digest, 'CPU source identity')
    driver = read('cpu-driver-final/result.json')
    require(driver['passed'] and driver['tests_run'] == 2 and not driver['errors'] and
        not driver['failures'] and not driver['skipped'], 'driver gate')
    for name, digest in driver['sources'].items():
        require(sha(data['cpu-driver-final/source/'+name]) == digest == plan['source_files'][name], 'driver identity')
    require(fixed['sources'] == {n:plan['source_files'][n] for n in fixed['sources']}, 'final CPU source drift')
    for name in READERS:
        require(sha(data['current-source/'+name]) == plan['source_files'][name], 'reader drift')
    for name, digest in plan['historical']['sources'].items():
        require(sha(data['historical-source/'+name]) == digest, 'historical drift')
    repro = read('reproduction/result.json')
    require(repro['tests_run'] == repro['failures'] == 1 and repro['simulated_elapsed'] >
            repro['deadline_seconds'] and not repro['errors'], 'original defect absent')
    network = read('execution/network/result.json')
    require(network['status'] == 'PASS_TLS_DEADLINES' and network['probes'] == 12 and
        len(network['records']) == 12 and network['candidate_transport_sha256'] ==
        plan['source_files']['tools/project_tasks/transport.py'] and
        network['baseline_transport_sha256'] == plan['historical']['sources']['tools/project_tasks/transport.py'],
        'network sources/probes')
    for row in network['records']:
        require(row['accepts'] == 1 and row['thread_stopped'] and row['listener_released'] and
                row == read('execution/network/'+row['name']+'.json'), 'network peer cleanup/receipt')
        name = row['name']
        if name == 'baseline-headers':
            require(row['error'] == 'TimeoutError' and .75 < row['elapsed_seconds'] < 2.5, 'real baseline defect')
        elif name in s.deadline_probes.CASES:
            if name.endswith('-ok'):
                require(row['body'] == 'ok' and row['error'] is None, 'normal response')
            else:
                require(row['error'] == 'TimeoutError' and .15 < row['elapsed_seconds'] < .8,
                        'bounded deadline: '+name)
        else:
            expected = 'REQUEST_UNKNOWN' if name == 'generation-deadline' else 'PREFLIGHT_REJECTED'
            require(row['state']['state'] == expected and row['real_tools'] == 0, 'network task safety')
    result = read('execution/result.json')
    require(result['status'] == 'PASS_TASK_DEADLINE_SHORT' and result['cleanup_confirmed'] and
        result['counts']['starts'] == 1 and result['counts']['generation_attempts'] == 4 and
        result['counts']['controls'] <= 48 and result['seconds'] <= 210, 'real execution bounds')
    log = data['execution/new/scoped-model/stderr.log'].decode()
    ends = [dict(re.findall(r'([a-z_]+)=([^ ]+)', line)) for line in log.splitlines()
            if line.startswith('[q4t][request]') and ' event=end ' in line]
    require(len(ends) == 4 and all(row['outcome'] == 'success' and row['reason'] == 'completed' and
        row['released'] == '1' for row in ends) and log.count('grammar worker reaped=1 ') == 4 and
        'shutdown complete (0 in-flight remaining)' in log, 'backend completion/cleanup')
    metrics = read('execution/final-metrics.json')
    require(metrics['q4t_requests_success_total'] == metrics['q4t_structured_workers_started_total'] ==
        metrics['q4t_structured_workers_reaped_total'] == 4 and all(metrics[n] == 0 for n in s.prior.s.a.EMPTY),
        'request resource counters')
    ids, usage = set(), {}
    parent = s.BASE/'offline'
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as temporary:
        for client, tokens in (('alpha', 512), ('beta', 64)):
            known, final = (read('execution/'+client+suffix) for suffix in ('-known-ledger.json', '-ledger.json'))
            require(known['meta'] == final['meta'] and known['events'] == final['events'][:len(known['events'])] and
                    known['events'][-1]['kind'] == 'tool_result', 'saved tool history changed')
            prefix = 'execution/'+client+'-completed-task/'
            folder = Path(temporary)/final['meta']['task_id']
            folder.mkdir(mode=0o700)
            for name, raw in data.items():
                if name.startswith(prefix): store.write_new(folder/name[len(prefix):], raw)
            require(sha(data[prefix+'ledger.db']) == final['database_sha256'], 'ledger bytes')
            db = store.connection(folder)
            try:
                ledger = store.Ledger(folder, db, final['meta']['store_id'])
                state = engine.replay(ledger)
                public = engine.public(ledger, state)
                expected = json.loads(read('execution/'+client+'-completed.json')['stdout'])
                require(ledger.meta == final['meta'] and ledger.events() == final['events'] and public == expected and
                    public['state'] == 'COMPLETED' and public['requests'] == 2 and public['max_tokens'] == tokens and
                    public['tool_attempts'] == public['saved_tool_results'] == 1 and
                    public['migrations'] == int(client == 'alpha') and
                    'Qwen4ExpForConditionalGeneration' in public['answer'] and '[docs/MODEL.md:L1-L24]' in public['answer'],
                    'actual task result/budget')
                require(state['execution_spec']['runtime_sources'] == {n:plan['source_files'][n] for n in engine.RUNTIME_FILES} and
                    state['execution_spec']['binding'] == ledger.spec['binding'] == plan['bindings'][client+'-new'], 'task binding')
                by = lambda kind: [row['payload'] for row in ledger.events() if row['kind'] == kind]
                if client == 'alpha':
                    change = by('migration')[0]['plan']
                    require(change['source_release'] == plan['historical']['release'] == 'srv11i-3967b862' and
                        change['format'] == migration.FORMAT and change['source_spec']['runtime_sources'] ==
                        plan['historical']['sources'], 'registered source migration')
                require(by('tool_started')[0]['arguments'] == dict(path='docs/MODEL.md', start_line=1, line_count=24) and
                    by('tool_started')[0]['name'] == 'read_project_record', 'real tool arguments')
                source = json.loads(by('tool_result')[0]['content'])
                require(source['ok'] and source['source']['sha256'] == plan['source_files']['docs/MODEL.md'], 'real project source')
                observations = data['execution/'+client+'-old/read-observations.jsonl'].splitlines()
                require(len(observations) == 1 and json.loads(observations[0])['result'] ==
                    source, 'exactly one real project read')
                usage[client] = [row['completion']['usage'] for row in by('response')]
                require(len(usage[client]) == 2 and all(0 < row['completion_tokens'] <= tokens for row in usage[client]), 'usage limits')
                ids.update(row['completion']['id'] for row in by('response'))
                require(json.loads(read('execution/'+client+'-duplicate-submit.json')['stdout']) ==
                        dict(public, created=False), 'duplicate submitted execution')
            finally:
                db.close()
            require(read('execution/'+client+'-old/reaped.json')['returncode'] == -9, 'application death boundary')
    require(ids == {row['id'] for row in ends}, 'wire/backend correlation')
    for name in ('scoped-model', 'nginx'):
        require(read('execution/new/'+name+'/reaped.json')['returncode'] == 0, 'service child exit')
    events = [json.loads(raw) for raw in data['execution/lease/events.jsonl'].splitlines()]
    starts = [r for r in events if r['event'] == 'lease_acquired']
    stops = [r for r in events if r['event'] == 'lease_released']
    observations = [r for r in events if r['event'] in ('preflight', 'monitor')]
    require(len(starts) == len(stops) == 1 and stops[0]['status'] ==
        read('execution/lease/exit.json')['status'] == 'COMPLETED' and observations and
        all(not row['conflicts'] for row in observations), 'lease ownership/observations')
    seconds = stops[0]['monotonic']-starts[0]['monotonic']
    require(0 < seconds <= 210, 'lease duration')
    post = read('postflight/result.json')
    require(post['passed'] and len(post['owners']) == 4 and all(r['gone'] for r in post['owners']) and
        post['lock'] == dict(dev=66305, inode=3294067, released=True) and post['default_unchanged'] and
        post['day_plans'] == plan['day_plans'] and read('postflight/gpu.json')['pids'] == [], 'postflight resource release')
    require(not data['postflight/containers.txt'].strip() and len(data['postflight/listeners.txt'].splitlines()) == 1 and
        dict(line.split('=', 1) for line in data['postflight/soar-unit.txt'].decode().splitlines()) ==
        dict(LoadState='masked', ActiveState='inactive', SubState='dead', MainPID='0'), 'service state')
    return dict(status='ACCEPT_TASK_NETWORK_DEADLINE', cpu_effective_tests=16, driver_tests=2,
        original_fixture_failures=2, repaired_fixture_tests=2, original_defect_reproduced=True,
        real_tls_probes=12, models=1, complete_responses=4, real_tools=2, usage=usage,
        application_kills=2, source_migrations=1, duplicate_submits=2,
        gpu_seconds=seconds, guard_observations=len(observations), owned_exited=4,
        default_unchanged=True, long_run=False, performance_acceptance=False)


def collect():
    data = {name:(s.BASE/name).read_bytes() for name in ('plan.json', 'run.stdout', 'run.stderr', 'baseline.json')}
    for name in ('reproduction', 'cpu', 'cpu-repair', 'ca-analysis', 'cpu-driver', 'cpu-driver-final'):
        for path in sorted((s.BASE/name).rglob('*')):
            relative = path.relative_to(s.BASE/name)
            if path.is_file() and (str(relative) in ('result.json', 'tests.log') or relative.parts[0] == 'source'):
                data[name+'/'+str(relative)] = path.read_bytes()
    for name in ('execution', 'postflight', 'plans'):
        for path in sorted((s.BASE/name).rglob('*')):
            if path.is_file(): data[str(path.relative_to(s.BASE))] = path.read_bytes()
    for name in READERS: data['current-source/'+name] = (ROOT/name).read_bytes()
    plan = pkg.read(s.PLAN)
    for name in plan['historical']['sources']:
        data['historical-source/'+name] = (s.HISTORICAL/name).read_bytes()
    secrets = [row['api_key'].encode() for row in pkg.read(plan['frontend'])['clients']]
    secrets.append(pkg.read(plan['policy'])['gateway_token'].encode())
    for name, raw in data.items():
        require(len(raw) < 16*1024**2 and not any(secret in raw for secret in secrets) and
            not re.search(rb'(?m)^-----BEGIN [A-Z ]*PRIVATE KEY-----\r?$', raw), 'private/oversize record: '+name)
    return data


def seal():
    s.check()
    data = collect()
    result = audit(data)
    DEST.mkdir()
    with tarfile.open(DEST/'records.tar.gz', 'w:gz') as archive:
        for name, raw in sorted(data.items()):
            member = tarfile.TarInfo(name)
            member.mode, member.size = 0o400, len(raw)
            archive.addfile(member, io.BytesIO(raw))
    pkg.save(DEST/'result.json', result)
    pkg.save(DEST/'manifest.json', dict(records={n:sha(raw) for n,raw in sorted(data.items())},
        archive_sha256=pkg.sha(DEST/'records.tar.gz'), result_sha256=pkg.sha(DEST/'result.json'),
        readers={n:pkg.sha(ROOT/n) for n in READERS},
        binaries={v['binary']:v['binary_sha256'] for v in pkg.read(s.PLAN)['versions']}))


def verify():
    manifest = pkg.read(DEST/'manifest.json')
    require(pkg.sha(DEST/'records.tar.gz') == manifest['archive_sha256'] and
        pkg.sha(DEST/'result.json') == manifest['result_sha256'], 'archive identity')
    with tarfile.open(DEST/'records.tar.gz') as archive:
        members = archive.getmembers()
        require(len(members) == len(manifest['records']) == len({m.name for m in members}) and
            all(m.isfile() and m.name in manifest['records'] and m.size < 16*1024**2 for m in members), 'archive inventory')
        data = {m.name:archive.extractfile(m).read() for m in members}
    require({n:sha(raw) for n,raw in data.items()} == manifest['records'], 'archive bytes')
    for name, digest in {**manifest['readers'], **manifest['binaries']}.items():
        require(pkg.sha(ROOT/name) == digest, 'local identity: '+name)
    require(audit(data) == pkg.read(DEST/'result.json'), 'offline reconstructed result')
    return dict(status='PASS_ARCHIVE', records=len(data), binaries=len(manifest['binaries']))


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('postflight', 'seal', 'verify'))
    action = parser.parse_args().action
    if action == 'postflight': postflight(); print('postflight passed')
    elif action == 'seal': seal(); print(json.dumps(verify()))
    else: print(json.dumps(verify()))
