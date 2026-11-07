"""Offline evidence audit; no model, NVML, or running service operations."""
from dataclasses import asdict
import fcntl
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tarfile

ORIGINAL = Path('/home/rm01/models/dev/qwen4-thor')
ROOT = ORIGINAL/'.q4t-work/serve-closeout-20261107/source'
BASE = ROOT/'.q4t-work/serve-closeout-business-20261107'
STAGE = BASE/'attempt-02'
OUT = STAGE/'execution'
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_service import closeout_acceptance as driver, package as pkg
from tools.examples import chat_client as client

plan = driver.check()
read = pkg.read
result, lease = read(OUT/'result.json'), read(STAGE/'lease/exit.json')
assert result['status'] == 'PASS_BOUNDED_SERVE_RESILIENCE' and result['failure'] is None
assert result['counts'] == dict(starts=3, chats=12, controls=10)
assert result['seconds'] < 1100
assert lease['status'] == 'COMPLETED' and lease['child_returncode'] == 0
assert not any(key in lease for key in ('cleanup_error', 'monitor_cleanup_error'))
queries = [json.loads(s) for s in (STAGE/'lease/nvml-queries.jsonl').read_text().splitlines()]
events = [json.loads(s) for s in (STAGE/'lease/events.jsonl').read_text().splitlines()]
assert queries and all(q['status'] == 'OK' for q in queries)
assert queries[0]['pids'] == queries[-1]['pids'] == []
observations = [e for e in events if e['event'] in ('monitor', 'preflight')]
assert observations and all(not e['conflicts'] and not e['unreadable_processes'] for e in observations)
assert events[-1]['event'] == 'lease_released'

tree = subprocess.check_output(['git', 'ls-tree', '-rz', plan['commit']], cwd=ROOT)
blobs = {row.split(b'\t', 1)[1].decode(): row.split(b'\t', 1)[0].split()[2].decode()
         for row in tree.split(b'\0') if row}
for name, expected in plan['sources'].items():
    raw = (ROOT/name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected
    assert hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest() == blobs[name]
protocol_sources = read(driver.BINARY.parent/'generated/q4t/server/chat_protocol_sources.json')
assert len(protocol_sources) == 107
assert all(pkg.sha(ROOT/name) == digest for name, digest in protocol_sources.items())

models, owners = set(), set()
responses = []
for label in ('native', 'v9', 'v10'):
    folder = OUT/label
    owners.add(read(folder/'owner.json')['pid'])
    for name in ('json', 'sse', 'tool-call', 'tool-final'):
        body = read(folder/(name+'-request.json'))
        saved = read(folder/(name+'-completion.json'))
        parsed = (client.read_sse if body['stream'] else client.read_json)(
            io.BytesIO((folder/(name+'-wire.bin')).read_bytes()))
        assert parsed.id == body['request_id'] and parsed.model == body['model']
        assert asdict(parsed) == {k:v for k,v in saved.items() if k != 'seconds'}
        assert saved['seconds'] > 0
        if name in ('json', 'sse'):
            assert parsed.finish_reason == 'stop' and parsed.message['content'].strip() == '42'
        elif name == 'tool-call':
            assert parsed.finish_reason == 'tool_calls' and len(parsed.message['tool_calls']) == 1
        else:
            tool = read(folder/'tool-result.json')
            assert tool['source']['sha256'] == plan['sources']['docs/STATUS.md']
            assert parsed.finish_reason == 'stop' and tool['source']['citation'] in parsed.message['content']
        responses.append(dict(stage=label, request=name, request_id=parsed.id, seconds=saved['seconds']))
    if label == 'native':
        reaped = read(folder/'reaped.json')
        assert reaped['returncode'] == 0 and not reaped['remaining']
        models.add(reaped['owner']['pid'])
        continue
    receipt = read(STAGE/(label+'-package.json'))
    manifest = pkg.verify(receipt['package'], receipt['manifest_sha256'])
    assert manifest['version'] == int(label[1:])
    assert manifest['files']['q4t'] == plan['candidate']['sha256']
    for name in pkg.V10_SOURCES:
        assert pkg.sha(Path(receipt['package'])/'source'/name) == plan['sources'][name]
    ready, gap, before, recovered, final = (read(folder/(name+'.json')) for name in
        ('ready', 'gap', 'before-recovery', 'recovered', 'business-final'))
    owner = ready['worker']['backend']
    models.add(owner['pid'])
    owners.add(ready['worker']['owner']['pid'])
    for value in (ready, gap, before, recovered, final):
        assert value['worker']['backend'] == owner
        assert value['worker']['attempts'] == 1 and value['worker']['restarts'] == 0
    assert gap['gpu_monitor']['visibility'] == before['gpu_monitor']['visibility'] == 'UNKNOWN'
    assert gap['gpu_monitor']['query_category'] == 'QUERY_ERROR'
    assert recovered['gpu_monitor']['visibility'] == 'AVAILABLE'
    assert recovered['gpu_monitor']['failed_queries'] > 0 and recovered['gpu_monitor']['resume_attempts'] == 1
    terminal = read(folder/'terminal.json')
    assert terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed']
    assert terminal['guard']['status'] == 'COMPLETED_WITH_MONITOR_GAPS'
    assert terminal['guard']['compute_monitor']['query_worker_reaped']
    faults = read(folder/'fault-counts.json')
    assert faults['log_errors'] > 0 and faults['metrics_attempts'] == faults['manager_attempts'] == 0
    assert before['worker']['had_auxiliary_gaps']
    if label == 'v10':
        assert gap['worker']['host_resources']['statistics_gap_samples'] > 0
        assert gap['worker']['host_resources']['observation']['memory.current'] is None
        assert terminal['host_resource_cleanup']['scope_removed']
        resource = read(folder/'managed/resources-final.json')
        assert resource['payload_removed'] and resource['violation'] is None
        assert not Path(resource['record']['root']).exists()

assert all(set(q['pids']) <= models for q in queries)
owners.update(models)
owners.update(q['worker_pid'] for q in queries if q.get('worker_pid'))
for e in events:
    owners.add(e['pid'])
    if e.get('child_pid'): owners.add(e['child_pid'])
assert all(not Path(f'/proc/{pid}').exists() for pid in owners)
with driver.service.guard.common_lock().open() as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock_identity = dict(device=lock.stat().st_dev, inode=lock.stat().st_ino) if hasattr(lock,'stat') else dict(device=__import__('os').fstat(lock.fileno()).st_dev,inode=__import__('os').fstat(lock.fileno()).st_ino)
    driver.port_free()
first = read(BASE/'execution/result.json')
assert first['status'] == 'FAILED' and first['counts'] == dict(starts=0, chats=0, controls=0)
evidence = ROOT/'docs/evidence/serve-closeout-business-20261107'
evidence.mkdir(parents=True, exist_ok=False)
files = [p for folder in (STAGE/'execution', STAGE/'lease') for p in folder.rglob('*') if p.is_file()]
files += [STAGE/name for name in ('plan.json','candidate.release.json','v9-package.json','v10-package.json')]
for label in ('v9', 'v10'):
    files += [STAGE/(label+'-package')/name for name in ('manifest.json','q4t.release.json','acceptance.json')]
files += [p for p in (STAGE/'declarations').rglob('*') if p.is_file()]
files += [BASE/'entry-error.json', BASE/'execution/result.json', BASE/'lease/exit.json']
files += [Path(__file__)]
rows = []
with tarfile.open(evidence/'raw-records.tar.gz', 'w:gz') as tar:
    for i,p in enumerate(sorted(set(files))):
        name = str(p.relative_to(BASE)) if p.is_relative_to(BASE) else 'audit_business.py'
        tar.add(p, arcname=name)
        rows.append(dict(path=name,sha256=pkg.sha(p),bytes=p.stat().st_size))
audit = dict(status='ACCEPT_BOUNDED_SERVE_RESILIENCE', source_commit=plan['commit'],
    candidate=plan['candidate'], counts=result['counts'], seconds=result['seconds'],
    outer_guard=lease['status'], real_nvml_queries=len(queries), nvml_errors=0,
    source_files=len(plan['sources']), protocol_sources=len(protocol_sources),
    responses=responses, source_verified=True, raw_responses_reparsed=True,
    model_restarts=0, owned_pids_reaped=len(owners), final_gpu_pids=[],
    port_available=True, lock_available=True, lock_identity=lock_identity,
    package_results=result['stages'], real_v10_scope_removed=True,
    default_replaced=False, performance_acceptance=False,
    retained_zero_model_failure=True, raw_records=len(rows))
pkg.save(evidence/'result.json',audit)
pkg.save(evidence/'manifest.json',dict(records=rows,archive_sha256=pkg.sha(evidence/'raw-records.tar.gz')))
print(json.dumps({k:v for k,v in audit.items() if k not in ('responses','package_results')},indent=2))
