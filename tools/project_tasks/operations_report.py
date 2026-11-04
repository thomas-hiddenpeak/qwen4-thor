"""Preserve and replay the exact SRV-11i CPU and guarded business evidence."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import operations_stage as s
from tools.project_tasks import protocol_report as prior
from tools.serve_service import package as pkg

DEST = ROOT/'docs/evidence/serve-task-operations-20261104'
READERS = sorted(set((*s.CPU_SOURCES,
    'tools/project_tasks/operations_report.py',
    'tools/project_tasks/operations_retry_cpu.py',
    'tools/project_tasks/protocol_report.py')))
sha = lambda raw: hashlib.sha256(raw).hexdigest()
require = pkg.require


def collect():
    data = {}
    for name in ('plan.json', 'run.stdout', 'run.stderr'):
        data[name] = (s.ATTEMPT/name).read_bytes()
    for name in ('cpu-import-01.json', 'python-dependencies.json'):
        data[name] = (s.BASE/name).read_bytes()
    for folder in ('cpu', 'cpu-entry-fix', 'execution', 'postflight', 'plans'):
        root = s.BASE if folder.startswith('cpu') else s.ATTEMPT
        for path in sorted((root/folder).rglob('*')):
            if path.is_file():
                pkg.regular(path)
                data[str(path.relative_to(root))] = path.read_bytes()
    for name in s.check()['prior_attempt']:
        data['original/'+name] = (s.BASE/name).read_bytes()
    for name in s.check()['historical']['sources']:
        data['historical-source/'+name] = (s.HISTORICAL/name).read_bytes()
    for path in sorted(s.TASKS.rglob('*')):
        if path.is_file():
            pkg.regular(path)
            data['tasks/'+str(path.relative_to(s.TASKS))] = path.read_bytes()
    for name in READERS:
        data['current-source/'+name] = (ROOT/name).read_bytes()
    plan = pkg.read(s.PLAN)
    front = pkg.read(plan['frontend'])
    private = [c['api_key'].encode() for c in front['clients']]
    private.append(pkg.read(plan['policy'])['gateway_token'].encode())
    for name, raw in data.items():
        require(all(secret not in raw for secret in private) and
                b'-----BEGIN PRIVATE KEY-----' not in raw,
                'private credential in archive: '+name)
    return data


def audit(data):
    read = lambda name: json.loads(data[name])
    plan, result, cpu = (read(n) for n in ('plan.json', 'execution/result.json', 'cpu/result.json'))
    require(cpu['passed'] and cpu['tests_run'] == len(set(cpu['names'])) == 23 and
            not cpu['errors'] and not cpu['failures'] and not cpu['skipped'], 'CPU gate')
    require(len(cpu['file_operation_kills']) == 4 and
            read('cpu-import-01.json')['tests_run'] == 0, 'CPU interruption/import evidence')
    require(sha(data['cpu/result.json']) == plan['cpu_sha256'], 'CPU receipt changed')
    original_plan = read('original/plan.json')
    for name, digest in cpu['sources'].items():
        source_plan = original_plan if name == 'tools/project_tasks/operations_stage.py' else plan
        require(sha(data['cpu/source/'+name]) == digest == source_plan['source_files'][name],
                'CPU source changed: '+name)
    repair = read('cpu-entry-fix/result.json')
    require(repair['passed'] and repair['tests_run'] == 2 and
            sha(data['cpu-entry-fix/result.json']) == plan['entry_repair_sha256'], 'entry repair gate')
    for name, digest in repair['sources'].items():
        require(sha(data['cpu-entry-fix/source/'+name]) == digest == plan['source_files'][name],
                'entry repair source changed')
    for name, digest in plan['prior_attempt'].items():
        require(sha(data['original/'+name]) == digest, 'original guard evidence changed')
    for name, digest in plan['historical']['sources'].items():
        require(sha(data['historical-source/'+name]) == digest, 'historical reader changed')
    for name in READERS:
        require(sha(data['current-source/'+name]) == plan['source_files'][name],
                'acceptance reader changed: '+name)
    require(result['status'] == 'PASS_TASK_PROTOCOL_SHORT' and result['cleanup_confirmed'] and
            result['counts']['starts'] == 2 and result['counts']['generation_attempts'] == 4 and
            result['counts']['controls'] <= 80 and result['seconds'] <= 270 and
            not result['long_run'], 'bounded business result')
    ends = []
    for label in ('old', 'new'):
        log = data[f'execution/{label}/scoped-model/stderr.log'].decode()
        rows = [dict(re.findall(r'([a-z_]+)=([^ ]+)', line)) for line in log.splitlines()
                if line.startswith('[q4t][request]') and ' event=end ' in line]
        require(len(rows) == 2 and all(row['outcome'] == 'success' and
                row['reason'] == 'completed' and row['released'] == '1' for row in rows),
                'backend completion counts')
        require(log.count('grammar worker reaped=1 ') == 2 and
                'shutdown complete (0 in-flight remaining)' in log, 'backend cleanup')
        ends.extend(rows)
        metrics = read('execution/'+label+'-metrics.json')
        require(metrics['q4t_requests_success_total'] ==
                metrics['q4t_structured_workers_started_total'] ==
                metrics['q4t_structured_workers_reaped_total'] == 2 and
                all(metrics[n] == 0 for n in s.prior.s.a.EMPTY), 'phase resource ownership')
        for component in ('scoped-model', 'nginx'):
            require(read(f'execution/{label}/{component}/reaped.json')['returncode'] == 0,
                    'service child exit')
    require(read('execution/old-stopped.json')['monotonic'] <
            read('execution/new/scoped-model/identity.json')['registered_monotonic'], 'backend overlap')
    for client in ('alpha', 'beta'):
        require(read('execution/'+client+'-old/reaped.json')['returncode'] == -9,
                'application not interrupted after saved tool')
        for label, code in (('unmigrated', 'binding_changed'),
                            ('wrong-operation', 'plan_changed'),
                            ('archive-reverse', 'protocol_unsupported')):
            value = read('execution/'+client+'-'+label+'.json')
            require(value['returncode'] == 1 and json.loads(value['stderr'])['code'] == code,
                    'metadata refusal')
    # Replays immutable wire, budget checks, review receipts, two completed
    # ledgers, four duplicate submissions and permanent-key receipts.
    usage = prior.tasks(data, plan, ends)
    delta = read('execution/capability-difference.json')
    require(delta == dict(changed=['http', 'observability', 'protocol_source_sha256'],
                         old_version=4, new_version=5), 'actual capability delta differs')
    state = read('execution/retention-status.json')
    require((state['active_tasks'], state['archived_keys'], state['retained_payloads']) ==
            (0, 2, 1), 'permanent key counts')
    events = [json.loads(v) for v in data['execution/lease/events.jsonl'].splitlines()]
    starts = [v for v in events if v['event'] == 'lease_acquired']
    stops = [v for v in events if v['event'] == 'lease_released']
    observations = [v for v in events if v['event'] in ('preflight', 'monitor')]
    require(len(starts) == len(stops) == 1 and stops[0]['status'] ==
            read('execution/lease/exit.json')['status'] == 'COMPLETED' and
            all(not v['conflicts'] for v in observations), 'GPU lease evidence')
    failed_events = [json.loads(v) for v in data['original/execution/lease/events.jsonl'].splitlines()]
    failed_starts = [v for v in failed_events if v['event'] == 'lease_acquired']
    failed_stops = [v for v in failed_events if v['event'] == 'lease_released']
    failed_observed = [v for v in failed_events if v['event'] in ('preflight', 'monitor')]
    require(len(failed_starts) == len(failed_stops) == 1 and failed_stops[0]['status'] ==
            read('original/execution/lease/exit.json')['status'] == 'COMMAND_FAILED' and
            all(not v['conflicts'] for v in failed_observed) and
            b'no matching deployment guard ancestor' in data['original/run.stderr'] and
            'original/execution/begin.json' not in data, 'original pre-work guard refusal')
    failed_post = read('original/failed-postflight/result.json')
    require(failed_post['lock_released'] and failed_post['gpu_processes'] == '' and
            failed_post['model_starts'] == failed_post['http_requests'] ==
            failed_post['identity_records'] == 0, 'original pre-work cleanup')
    failed_seconds = failed_stops[0]['monotonic']-failed_starts[0]['monotonic']
    retry_seconds = stops[0]['monotonic']-starts[0]['monotonic']
    require(failed_seconds+retry_seconds <= 270, 'total GPU lease bound')
    post = read('postflight/result.json')
    require(post['passed'] and len(post['owners']) == 6 and all(v['gone'] for v in post['owners']) and
            post['lock'] == dict(dev=66305, inode=3294067, released=True) and
            read('postflight/gpu.json')['pids'] == [], 'postflight resources')
    return dict(status='ACCEPT_TASK_OPERATIONS_PROTOCOL', cpu_tests=23,
        application_cpu_tests=17, driver_cpu_tests=6, entry_repair_tests=2, file_operation_kills=4,
        models=2, gateways=2, responses=4, real_tools=2, application_kills=2,
        active_migrations=2, duplicate_applies=2, duplicate_submits=4,
        archive_resumes_without_execution=2, metadata_refusals=6,
        actual_archived_protocol_migrations=0, archived_protocol_migrations_cpu_only=True,
        permanent_keys=2, retained_payloads=1, usage=usage,
        seconds=result['seconds'], controls=result['counts']['controls'],
        gpu_seconds=failed_seconds+retry_seconds, failed_gpu_seconds=failed_seconds,
        retry_gpu_seconds=retry_seconds,
        guard_observations=len(observations)+len(failed_observed), owned_exited=6,
        default_unchanged=True, original_cpu_import_failure_preserved=True,
        original_guard_invocation_failure_preserved=True, original_models=0,
        reviewed_transition=plan['protocol_review'], long_run=False,
        actual_lan_accepted=False, physical_ram_limit=False, performance_acceptance=False)


def seal():
    s.check()
    data = collect()
    result = audit(data)
    DEST.mkdir(exist_ok=False)
    with tarfile.open(DEST/'records.tar.gz', 'w:gz') as archive:
        for name, raw in sorted(data.items()):
            member = tarfile.TarInfo(name)
            member.mode = 0o400
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    pkg.save(DEST/'result.json', result)
    plan = json.loads(data['plan.json'])
    pkg.save(DEST/'manifest.json', dict(records={n:sha(raw) for n,raw in sorted(data.items())},
        archive_sha256=pkg.sha(DEST/'records.tar.gz'), result_sha256=pkg.sha(DEST/'result.json'),
        readers={n:pkg.sha(ROOT/n) for n in READERS},
        binaries={v['binary']:v['binary_sha256'] for v in plan['versions']}))


def verify():
    manifest = pkg.read(DEST/'manifest.json')
    require(pkg.sha(DEST/'records.tar.gz') == manifest['archive_sha256'] and
            pkg.sha(DEST/'result.json') == manifest['result_sha256'], 'sealed archive changed')
    with tarfile.open(DEST/'records.tar.gz') as archive:
        members = archive.getmembers()
        require(len(members) == len(manifest['records']) and
                len({m.name for m in members}) == len(members) and
                all(m.isfile() and m.name in manifest['records'] and
                    m.size <= 16*1024**2 for m in members), 'archive inventory')
        data = {m.name:archive.extractfile(m).read() for m in members}
    require({n:sha(raw) for n,raw in data.items()} == manifest['records'], 'raw record differs')
    for name, digest in {**manifest['readers'], **manifest['binaries']}.items():
        require(pkg.sha(ROOT/name) == digest, 'local source/binary changed: '+name)
    require(audit(data) == pkg.read(DEST/'result.json'), 'reconstructed result differs')
    return dict(status='PASS_ARCHIVE', records=len(data), binaries=len(manifest['binaries']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('postflight', 'seal', 'verify'))
    action = parser.parse_args().action
    if action == 'postflight': prior.postflight()
    elif action == 'seal': seal(); print(json.dumps(verify()))
    else: print(json.dumps(verify()))
