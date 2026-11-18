"""Fixed native speaker numerical qualification; not voice synthesis acceptance."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import time

try:
    from .acceptance import ROOT, defaults, digest
except ImportError:
    from acceptance import ROOT, defaults, digest

LIMITS = dict(seconds=600, native_seconds=180, reference_seconds=240,
              gpu_model_loads=1, cpu_model_loads=1, cases=4, native_stages=188,
              reference_forwards=8, boundary_checks=8, lifecycle_checks=16,
              http_requests=0, main_llm_loads=0, artifact_mib=64,
              minimum_free_mib=256)
THRESHOLDS = dict(
    stage=dict(relative_l2=0.0005, cosine=0.999999, max_abs=0.02),
    embedding=dict(relative_l2=0.0001, cosine=0.99999999, max_abs=0.01),
    frontend_embedding=dict(relative_l2=0.0005, cosine=0.999999, max_abs=0.02),
    canonical_feature_max_abs=0.0001)


def pair(a, b, count, threshold):
    import numpy as np
    assert a.stat().st_size == b.stat().st_size == count * 4
    x, y = [np.fromfile(p, '<f4').astype(np.float64) for p in (a, b)]
    assert np.isfinite(x).all() and np.isfinite(y).all()
    nx, ny = float(np.linalg.norm(x)), float(np.linalg.norm(y))
    assert nx > 0 and ny > 0
    error = float(np.linalg.norm(x - y) / ny)
    cosine = float(x @ y / (nx * ny))
    maximum = float(np.max(np.abs(x - y)))
    return dict(relative_l2=error, cosine=cosine, max_abs=maximum,
                passed=error <= threshold['relative_l2'] and
                       cosine >= threshold['cosine'] and maximum <= threshold['max_abs'])


def compare(output, plan):
    import numpy as np
    native = json.loads((output / 'native/result.json').read_bytes())
    reference = json.loads((output / 'reference/result.json').read_bytes())
    assert native['status'] == 'PASS_NATIVE_COMPONENT' and native['cases'] == 4
    assert native['boundary_checks'] == 8 and native['lifecycle_checks'] == 16
    assert native['gpu_model_loads'] == 1 and native['weight_bytes'] == 48004352
    assert 0 < native['max_explicit_workspace_bytes'] <= 300 * 1024**2
    assert reference['status'] == 'PASS_REFERENCE_EXECUTED'
    assert reference['cpu_model_loads'] == 1 and reference['model_forwards'] == 8
    assert reference['cuda_initialized'] is False
    comparisons = []
    for index, case in enumerate(plan['cases']):
        a, b = [output / name / f'case-{index}' for name in ('native', 'reference')]
        frames = case['frames']
        assert 5 <= frames <= 5625
        na, ca = [np.fromfile(p, '<f4').astype(np.float64) for p in
                  (a / 'mel.f32', b / 'canonical-mel.f32')]
        assert na.size == ca.size == 128 * frames
        assert np.isfinite(na).all() and np.isfinite(ca).all()
        error = float(np.max(np.abs(na - ca)))
        comparisons.append(dict(case=index, operand='canonical_frontend',
            max_abs=error, passed=error <= THRESHOLDS['canonical_feature_max_abs']))
        for stage in range(7):
            count = 512 * frames if stage < 4 else (
                1536 * frames if stage == 4 else 3072 if stage == 5 else 2048)
            threshold = THRESHOLDS['embedding' if stage == 6 else 'stage']
            name = f'stage-{stage}.f32'
            comparisons.append(dict(case=index, operand=name,
                **pair(a / name, b / name, count, threshold)))
        assert (a / 'embedding.f32').read_bytes() == (a / 'stage-6.f32').read_bytes()
        comparisons.append(dict(case=index, operand='official_frontend_effect',
            **pair(b / 'embedding.f32', b / 'official-front-embedding.f32',
                   2048, THRESHOLDS['frontend_embedding'])))
    assert len(comparisons) == 36
    return comparisons


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.environ.get('PYTHONOPTIMIZE')
    assert not any(n.startswith('Q4T_ABLATE_') for n in os.environ)
    plan = json.loads(args.plan.read_bytes())
    assert plan['schema'] == 'q4t-base-speaker-fp32-qualification-v1'
    assert plan['limits'] == LIMITS and plan['thresholds'] == THRESHOLDS
    assert len(plan['cases']) == 4 and defaults() == plan['defaults']
    assert all(digest(Path(p)) == h for p, h in plan['sources'].items())
    space = os.statvfs(args.output.parent)
    assert space.f_bavail * space.f_frsize >= 256 * 1024**2
    args.output.mkdir(exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', runs=[], comparisons=[],
                  defaults_before=defaults(), plan_sha256=digest(args.plan),
                  custom_voice_synthesis_qualified=False)
    commands = [
        ('native', [plan['binary'], plan['assets'], str(args.plan.absolute()),
                    str(args.output.absolute() / 'native')]),
        ('reference', [plan['reference_python'], '-B',
                       str(ROOT / 'tools/native_tts/speaker_reference.py'),
                       '--plan', str(args.plan.absolute()), '--native',
                       str(args.output.absolute() / 'native'), '--output',
                       str(args.output.absolute() / 'reference')])]
    try:
        for name, command in commands:
            remaining = LIMITS['seconds'] - (time.monotonic() - started)
            assert remaining > 0
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
                       TRANSFORMERS_OFFLINE='1', OMP_NUM_THREADS='1')
            if name == 'reference':
                env['CUDA_VISIBLE_DEVICES'] = ''
            step = time.monotonic()
            record = dict(name=name, command=command)
            result['runs'].append(record)
            with (args.output / (name + '.log')).open('wb') as log:
                try:
                    child = subprocess.run(command, cwd=ROOT, env=env,
                        pass_fds=(lease,), stdout=log, stderr=subprocess.STDOUT,
                        timeout=min(remaining, LIMITS[name + '_seconds']))
                    record['returncode'] = child.returncode
                finally:
                    record['seconds'] = time.monotonic() - step
            assert child.returncode == 0, name + ' execution failed'
        result['comparisons'] = compare(args.output, plan)
        assert all(x['passed'] for x in result['comparisons']), 'numerical comparison failed'
        result['status'] = 'PASS_SPEAKER_COMPONENT_PENDING_LEASE'
    except Exception as error:
        result.update(status='FAILED_SPEAKER_COMPONENT', error=repr(error))
    finally:
        result['defaults_unchanged'] = defaults() == result['defaults_before']
        result['seconds'] = time.monotonic() - started
        result['artifact_bytes'] = sum(p.stat().st_size for p in args.output.rglob('*') if p.is_file())
        if not result['defaults_unchanged'] or result['artifact_bytes'] > 64 * 1024**2:
            result.update(status='FAILED_PROTECTION', error='default or artifact bound')
        result['artifacts'] = {str(p.relative_to(args.output)): digest(p)
                              for p in args.output.rglob('*') if p.is_file()}
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('status', 'seconds', 'defaults_unchanged')}, ensure_ascii=False))
    return 0 if result['status'] == 'PASS_SPEAKER_COMPONENT_PENDING_LEASE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
