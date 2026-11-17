"""Bounded fixed ECAPA arithmetic checks, under one strict GPU lease."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
LIMITS = dict(seconds=600, native_seconds=180, reference_seconds=240,
              gpu_model_loads=1, cpu_model_loads=1, cases=4,
              boundary_checks=8, lifecycle_checks=16, http_requests=0)
THRESHOLDS = dict(features_max_abs=0.002,
    stage=dict(relative_l2=0.0005, cosine=0.999999, max_abs=0.02),
    embedding=dict(relative_l2=0.0001, cosine=0.99999999, max_abs=0.01))


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as file:
        while chunk := file.read(1 << 20):
            value.update(chunk)
    return value.hexdigest()


def defaults():
    return {name: digest(ROOT/name) for name in [
        'build/q4t', 'build/q4t.release.json', 'build/service/manifest.json',
        'build/service.release.json', 'build/service-native-state/ledger.json']}


def compare_arrays(a, b, count, threshold):
    import numpy as np
    if a.shape != (count,) or b.shape != (count,):
        raise ValueError('operand shape differs')
    if not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError('operand is nonfinite')
    a, b = a.astype(np.float64), b.astype(np.float64)
    delta = float(np.linalg.norm(a-b))
    norm_a, norm_b = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    relative = delta / max(norm_b, 1e-30)
    cosine = (float(a @ b / (norm_a*norm_b)) if norm_a and norm_b
              else 1.0 if not norm_a and not norm_b else 0.0)
    maximum = float(np.max(np.abs(a-b)))
    passed = maximum <= threshold['max_abs']
    if 'relative_l2' in threshold:
        passed = passed and relative <= threshold['relative_l2']
    if 'cosine' in threshold:
        passed = passed and cosine >= threshold['cosine']
    return dict(passed=bool(passed), cosine=cosine, relative_l2=relative,
                max_abs=maximum, elements=count)


def compare(plan, output):
    import numpy as np
    checks = []
    for index, case in enumerate(plan['cases']):
        frames = case['frames']
        counts = [1024*frames]*4 + [3072*frames, 6144, 192]
        operands = [('mel.f32', 80*frames,
                     dict(max_abs=THRESHOLDS['features_max_abs']))]
        operands += [(f'stage-{i}.f32', count, THRESHOLDS['stage'])
                     for i, count in enumerate(counts)]
        operands.append(('embedding.f32', 192, THRESHOLDS['embedding']))
        for name, count, threshold in operands:
            a = np.fromfile(output/'native'/f'case-{index}'/name, '<f4')
            b = np.fromfile(output/'reference'/f'case-{index}'/name, '<f4')
            result = compare_arrays(a, b, count, threshold)
            checks.append(dict(case=index, operand=name, **result))
    return checks


def run(plan_path):
    plan = json.loads(plan_path.read_bytes())
    assert plan['limits'] == LIMITS and plan['thresholds'] == THRESHOLDS
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    os.fstat(lease)
    assert not os.environ.get('PYTHONOPTIMIZE')
    for path, expected in plan['identities'].items():
        assert digest(path) == expected, 'input changed: '+path
    assert len(plan['cases']) == 4 and [c['frames'] for c in plan['cases']][:2] == [5, 3001]
    output = Path(plan['output'])/'run'
    output.mkdir(exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', defaults_before=defaults(), runs=[],
                  plan_sha256=digest(plan_path), limits=LIMITS)
    commands = [
        ('native', [plan['binary'], plan['assets'], str(plan_path), str(output/'native')]),
        ('reference', [plan['reference_python'], '-I', '-B',
            str(ROOT/'tools/native_speaker/model_reference.py'),
            '--source', plan['source'], '--mask-source', plan['mask_source'],
            '--checkpoint', plan['checkpoint'], '--plan', str(plan_path),
            '--native', str(output/'native'), '--output', str(output/'reference')])]
    try:
        for name, command in commands:
            remaining = LIMITS['seconds'] - (time.monotonic()-started)
            assert remaining > 0
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='1')
            if name == 'reference':
                env['CUDA_VISIBLE_DEVICES'] = ''
            step = time.monotonic()
            row = dict(name=name, command=command)
            result['runs'].append(row)
            with (output/(name+'.log')).open('w') as log:
                try:
                    child = subprocess.run(command, cwd=ROOT, env=env,
                        pass_fds=(lease,), stdout=log, stderr=subprocess.STDOUT,
                        timeout=min(remaining, LIMITS[name+'_seconds']))
                    row['exit_code'] = child.returncode
                finally:
                    row['seconds'] = time.monotonic()-step
            assert child.returncode == 0, name+' execution failed'
        native = json.loads((output/'native/result.json').read_bytes())
        reference = json.loads((output/'reference/result.json').read_bytes())
        assert native['gpu_model_loads'] == 1 and native['cases'] == 4
        assert native['boundary_checks'] == 8 and native['lifecycle_checks'] == 16
        assert reference['cpu_model_loads'] == 1 and not reference['cuda_initialized']
        assert reference['gpu_model_loads'] == 0 and len(reference['cases']) == 4
        result['checks'] = compare(plan, output)
        assert len(result['checks']) == 36 and all(c['passed'] for c in result['checks']), 'numerical contract failed'
        result['status'] = 'PASS_COMPONENT_PENDING_LEASE_RECONCILIATION'
    except Exception as error:
        result.update(status='FAILED', error=repr(error))
    finally:
        result['defaults_after'] = defaults()
        result['defaults_unchanged'] = result['defaults_after'] == result['defaults_before']
        result['seconds'] = time.monotonic()-started
        if not result['defaults_unchanged'] or result['seconds'] > LIMITS['seconds']:
            result.update(status='FAILED', error='protected default or time budget differs')
        result['artifacts'] = {str(p.relative_to(output)): digest(p)
                              for p in sorted(output.rglob('*')) if p.is_file()}
        (output/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k: result[k] for k in ['status','seconds','defaults_unchanged']}))
    return 0 if result['status'].startswith('PASS_') else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    return run(args.plan.resolve(strict=True))


if __name__ == '__main__':
    raise SystemExit(main())
