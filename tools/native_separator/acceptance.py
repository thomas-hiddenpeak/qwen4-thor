"""Fixed-source native SepFormer arithmetic and actual two-source separation."""
import argparse
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_speaker.acceptance import compare_arrays, defaults, digest

LIMITS = dict(seconds=600, native_seconds=180, reference_seconds=240,
              gpu_model_loads=1, cpu_model_loads=1, cases=4,
              boundary_checks=8, lifecycle_checks=16, http_requests=0)
THRESHOLDS = dict(
    stage=dict(relative_l2=0.0005, cosine=0.999999, max_abs=0.02),
    pcm=dict(relative_l2=0.0005, cosine=0.999999, max_abs=0.001),
    separation_minimum_si_sdr_improvement_db=5.)


def stage_counts(samples):
    if not isinstance(samples, int) or not 16 <= samples <= 64000:
        raise ValueError('sample budget differs')
    frames = (samples-16)//8+1
    gap = 250-(125+frames % 250) % 250
    segments = (frames+gap+125)//125
    return [frames*256]+[250*segments*256]*4+[2*frames*256, 2*samples]


def separation_quality(sources, output):
    import numpy as np
    if (sources.ndim != 2 or sources.shape[0] != 2 or
            sources.shape != output.shape or sources.shape[1] < 16 or
            not np.isfinite(sources).all() or not np.isfinite(output).all()):
        raise ValueError('two finite aligned source tracks required')
    sources, output = sources.astype(np.float64), output.astype(np.float64)
    mixture = sources.sum(axis=0)
    def score(reference, estimate):
        reference = reference - reference.mean()
        estimate = estimate - estimate.mean()
        energy = float(reference @ reference)
        if energy < 1e-12:
            raise ValueError('known source must contain actual signal')
        projected = (estimate @ reference) / energy * reference
        residual = estimate - projected
        return float(10*np.log10(max(float(projected @ projected), 1e-30) /
                                max(float(residual @ residual), 1e-30)))
    baseline = [score(s, mixture) for s in sources]
    trials = []
    for permutation in itertools.permutations(range(2)):
        separated = [score(sources[i], output[permutation[i]]) for i in range(2)]
        improvement = [separated[i]-baseline[i] for i in range(2)]
        trials.append(dict(permutation=list(permutation),
            separated_si_sdr_db=separated, mixture_si_sdr_db=baseline,
            improvement_db=improvement))
    result = max(trials, key=lambda r: sum(r['improvement_db']))
    result['passed'] = all(x >= THRESHOLDS['separation_minimum_si_sdr_improvement_db']
                           for x in result['improvement_db'])
    return result


def compare(plan, output):
    import numpy as np
    checks, finishes = [], []
    for index, case in enumerate(plan['cases']):
        for stage, count in enumerate(stage_counts(case['samples'])):
            name = f'stage-{stage}.f32'
            a = np.fromfile(output/'native'/f'case-{index}'/name, '<f4')
            b = np.fromfile(output/'reference'/f'case-{index}'/name, '<f4')
            checks.append(dict(case=index, stage=stage, **compare_arrays(
                a, b, count, THRESHOLDS['pcm' if stage == 6 else 'stage'])))
        for backend in ['native', 'reference']:
            path = output/backend/f'case-{index}'
            if (path/'stage-6.f32').read_bytes() != (path/'sources.f32').read_bytes():
                raise ValueError('final source publication differs from completed stage')
        finishes.append(dict(case=index, passed=True))
    case = plan['cases'][2]
    sources_path = Path(case['known_sources'])
    if digest(sources_path) != case['known_sources_sha256']:
        raise ValueError('known original sources changed')
    sources = np.fromfile(sources_path, '<f4').reshape(2, case['samples'])
    quality = {}
    for backend in ['native', 'reference']:
        samples = np.fromfile(output/backend/'case-2/sources.f32', '<f4')
        quality[backend] = separation_quality(sources, samples.reshape(sources.shape))
    return dict(checks=checks, finish_checks=finishes, separation_quality=quality)


def run(plan_path):
    plan = json.loads(plan_path.read_bytes())
    assert plan['limits'] == LIMITS and plan['thresholds'] == THRESHOLDS
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    os.fstat(lease)
    assert not os.environ.get('PYTHONOPTIMIZE')
    for path, expected in plan['identities'].items():
        assert digest(path) == expected, 'input changed: ' + path
    assert [c['samples'] for c in plan['cases']] == [16, 257, 32000, 64000]
    output = Path(plan['output'])/'run'
    output.mkdir(exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', defaults_before=defaults(), runs=[],
                  plan_sha256=digest(plan_path), limits=LIMITS)
    commands = [
        ('native', [plan['binary'], plan['assets'], str(plan_path), str(output/'native')]),
        ('reference', [plan['reference_python'], '-I', '-B',
            str(ROOT/'tools/native_separator/model_reference.py'),
            '--source', plan['source'], '--checkpoints', plan['checkpoints'],
            '--plan', str(plan_path), '--output', str(output/'reference')])]
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
        result.update(compare(plan, output))
        assert len(result['checks']) == 28
        result['numerical_passed'] = all(c['passed'] for c in result['checks'])
        result['separation_passed'] = all(v['passed'] for v in
                                           result['separation_quality'].values())
        assert result['numerical_passed'], 'numerical contract failed'
        assert result['separation_passed'], 'actual known-source separation failed'
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
    print(json.dumps({k: result[k] for k in ['status', 'seconds', 'defaults_unchanged']}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    raise SystemExit(run(parser.parse_args().plan.resolve(strict=True)))
