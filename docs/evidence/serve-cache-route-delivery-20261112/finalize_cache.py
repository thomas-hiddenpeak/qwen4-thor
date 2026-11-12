"""Accept only the original cache execution, strict guard and complete cleanup."""
import fcntl
import json
import struct
from pathlib import Path
from tools.serve_cache_io import acceptance as cache
from tools.serve_responses.background_acceptance import probe_ports
from tools.serve_service.package import read, require, save, sha

ROOT = Path.cwd()
WORK = ROOT / '.q4t-work/serve-main-integration-20261112'
OUT = WORK / 'cache-business-02'
plan, result, cleanup, verdict, progress = [read(OUT/n) for n in
    ('plan.json', 'result.json', 'cleanup.json', 'business-verdict.json',
     'progress-summary.json')]
require(result['status'] == cache.Stage.success_status and
        result['counts']['starts'] == 1 and result['counts']['chats'] == 7 and
        result['seconds'] < 600, 'business/budget not accepted')
require(result['binary_sha256'] == plan['binary_sha256'] == sha(plan['binary']),
        'engine changed')
for name, digest in {**plan['sources'], **plan['protected']}.items():
    require(sha(ROOT/name) == digest, 'postflight drift: ' + name)
require(cleanup['remaining'] == [] and cleanup['model_exit'] == 0 and
        cleanup['ports_released'] and cleanup['lease_continuously_held'],
        'cleanup incomplete')
guard = cache.guard
processes = guard.processes()
require(not any(processes.get(int(pid), {}).get('start_ticks') == row['start_ticks']
                for pid, row in cleanup['owned'].items()), 'owned process alive')
probe_ports((plan['port'], plan['operations_port']))
with guard.common_lock().open('r+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
exit_record = read(OUT/'lease/exit.json')
queries = [json.loads(line) for line in
           (OUT/'lease/nvml-queries.jsonl').read_text().splitlines()]
require(exit_record['status'] == 'COMPLETED' and exit_record['child_returncode'] == 0
        and exit_record['compute_backend'] == 'nvml' and
        exit_record['lease_inherited_by_child'] and queries and
        all(q['status'] == 'OK' and q['returncode'] == 0 for q in queries) and
        queries[-1]['pids'] == [], 'strict environment failed')
require(verdict['same_model'] and verdict['cache_fault']['prefix'] == 0 and
        verdict['cache_fault']['error'] == 'cache storage time limit exceeded' and
        verdict['recovered']['error'] == verdict['mtp_restore']['error'] == 'none' and
        verdict['cache_before']['prefix']['generation'] ==
        verdict['cache_after']['prefix']['generation'] and
        verdict['cache_after']['session']['workers_reaped'] == 1,
        'cache isolation/recovery differs')
require(progress['error'] is None and progress['last']['successes'] == 7 and
        progress['last']['active'] == 0, 'progress differs')
raw = (OUT/'progress.bin').read_bytes()
offset = frames = payload = 0
while offset < len(raw):
    size = struct.unpack_from('<I', raw, offset)[0]
    offset += 4
    require(size >= 184 and offset + size <= len(raw), 'truncated progress frame')
    offset += size
    frames += 1
    payload += size
require(frames == progress['frames'] and payload == progress['bytes'],
        'progress raw counts differ')
decision = dict(status='ACCEPT_TARGETED_CACHE_CANCELLATION_BUSINESS',
    binary_sha256=result['binary_sha256'], source_commit=plan['source_commit'],
    protocol_sha256=read(OUT/'capabilities.json')['protocol_source_sha256'],
    env_abort=False, counts=result['counts'], seconds=result['seconds'],
    nvml_queries=len(queries), same_model=True, cache_recovered=True,
    shared_cache_unchanged=True, postflight=dict(sources_unchanged=True,
        owned_processes_gone=True, ports_free=True, lock_released=True),
    actual_verdict=verdict, performance_acceptance=False,
    full_model_quality=False, long_stability_accepted=False,
    default_changed=False, remaining_master_tasks_open=True,
    records={name:sha(OUT/name) for name in ('plan.json', 'result.json',
        'business-verdict.json', 'cleanup.json', 'lease/exit.json',
        'lease/nvml-queries.jsonl', 'progress.bin')})
require(not (OUT/'acceptance.json').exists(), 'preserve prior acceptance')
save(OUT/'acceptance.json', decision)
print(json.dumps({k:decision[k] for k in ('status','counts','seconds','nvml_queries')}))
