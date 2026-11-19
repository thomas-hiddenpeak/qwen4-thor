"""One explicit padding-fix replay; immutable official reference is reused."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.native_tts import encoder_acceptance as gate

PHASE = Path(__file__).resolve().parent
OLD = PHASE.parent / 'component-01'
plan = json.loads((PHASE / 'plan.json').read_text())
gate.validate_plan(plan)
lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
assert plan['execution_limits'] == dict(seconds=600, gpu_model_loads=1,
    cpu_reference_model_loads=0, cases=3, encoder_forwards=3, http_requests=0)
assert not os.environ.get('PYTHONOPTIMIZE')
assert not any(k.startswith('Q4T_ABLATE_') for k in os.environ)
old_plan = json.loads((OLD / 'plan.json').read_text())
old_result = json.loads((OLD / 'run/result.json').read_text())
assert old_result['status'] == 'FAILED'
assert old_result['error'] == 'encoder numerical comparison failed'
assert old_result['defaults_unchanged'] and old_result['runtime_sources_unchanged']
assert [r['exit_code'] for r in old_result['runs']] == [0, 0]
assert json.loads((OLD / 'lease/exit.json').read_text())['status'] == 'COMMAND_FAILED'
assert all(json.loads(line)['status'] == 'OK'
           for line in (OLD / 'lease/nvml-queries.jsonl').read_text().splitlines())
events = [json.loads(line) for line in (OLD / 'lease/events.jsonl').read_text().splitlines()]
assert not any(e.get('conflicts') or e.get('unreadable_processes') for e in events)
assert events[-1]['event'] == 'lease_released'
assert plan['cases'] == old_plan['cases']
assert plan['driver_sources'] == old_plan['driver_sources']
assert plan['reference_mimi_sha256'] == old_plan['reference_mimi_sha256']
assert plan['reference_mask_sha256'] == old_plan['reference_mask_sha256']
assert {n for n, h in plan['runtime_sources'].items()
        if old_plan['runtime_sources'].get(n) != h} == {
    'include/q4t/audio/tts_encoder.h', 'src/audio/tts_encoder.cu',
    'src/audio/tts_encoder_conv.cu'}
assert {n: gate.digest(OLD / n) for n in plan['previous_evidence']} == plan['previous_evidence']
assert all(gate.digest(OLD / 'run' / n) == h for n, h in old_result['artifacts'].items())
assert all(gate.digest(OLD / 'sources' / n) == h for n, h in old_plan['runtime_sources'].items())
assert gate.defaults() == plan['default_before'] == old_plan['default_before']
assert {n: gate.digest(ROOT / n) for n in plan['runtime_sources']} == plan['runtime_sources']
assert {n: gate.digest(ROOT / 'tools/native_tts' / n) for n in gate.DRIVERS} == plan['driver_sources']
assert all(gate.digest(Path(c['pcm'])) == c['sha256'] for c in plan['cases'])
assert gate.digest(Path(plan['binary'])) == plan['binary_sha256']
assert gate.digest(Path(__file__)) == plan['continuation_driver_sha256']
output = PHASE / 'run'
output.mkdir(exist_ok=False)
shutil.copytree(OLD / 'run/reference', output / 'reference', copy_function=os.link)
result = dict(status='RUNNING', gpu_model_loads=0, cpu_reference_model_loads=0,
              reference_reused=True, plan_sha256=gate.digest(PHASE / 'plan.json'))
started = time.monotonic()
try:
    command = [plan['binary'], plan['assets'], str(PHASE / 'plan.json'), str(output / 'native')]
    result['gpu_model_loads'] = 1
    with (output / 'native.log').open('wb') as log:
        completed = subprocess.run(command, cwd=ROOT, pass_fds=(lease,),
            stdout=log, stderr=subprocess.STDOUT, timeout=590)
    result['native_exit_code'] = completed.returncode
    assert completed.returncode == 0, 'corrected native execution failed'
    result['comparisons'] = gate.compare(output, plan)
    assert all(c['passed'] for c in result['comparisons']), 'corrected encoder comparison failed'
    # The correction starts after the transformer. Earlier native snapshots
    # must be byte-identical; their existing reference evidence is preserved.
    names = [name for name, _ in gate.operands(17) if not name.startswith(
        ('encoder-latent', 'encoder-projection', 'encoder-rvq-residual'))]
    assert all(gate.digest(output / 'native' / f'case-{i}' / name) ==
               gate.digest(OLD / 'run/native' / f'case-{i}' / name)
               for i in range(3) for name in names)
    result['unchanged_upstream_operands'] = len(names) * 3
    result['status'] = 'PASS_ENCODER_PADDING_FIX_PENDING_LEASE_RECONCILIATION'
except Exception as error:
    result.update(status='FAILED', error=str(error))
finally:
    result['seconds'] = time.monotonic() - started
    result['defaults_unchanged'] = gate.defaults() == plan['default_before']
    result['runtime_sources_unchanged'] = {n: gate.digest(ROOT / n)
        for n in plan['runtime_sources']} == plan['runtime_sources']
    if not result['defaults_unchanged'] or not result['runtime_sources_unchanged']:
        result.update(status='FAILED', error='protected identity changed')
    result['artifacts'] = {str(p.relative_to(output)): gate.digest(p)
        for p in sorted(output.rglob('*')) if p.is_file()}
    (output / 'result.json').write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps({k: result[k] for k in ('status', 'seconds', 'defaults_unchanged')}))
raise SystemExit(0 if result['status'].startswith('PASS_') else 1)
