#!/usr/bin/env python3
"""Frozen native reference-audio encoder qualification, before ICL integration."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import time

try:
    from .acceptance import ROOT, compare_pair, defaults, digest
except ImportError:
    from acceptance import ROOT, compare_pair, defaults, digest

LIMITS = dict(seconds=1200, native_seconds=600, reference_seconds=600,
              gpu_model_loads=1, cpu_reference_model_loads=1,
              cases=3, encoder_forwards=3, native_advances=8192,
              main_llm_loads=0, talker_loads=0, http_requests=0)
THRESHOLDS = dict(cosine=0.999999, relative_l2=0.001)
SAMPLES = [17, 55693, 278419]
DRIVERS = ('encoder_replay.cpp', 'encoder_reference.py',
           'encoder_acceptance.py', 'codec_reference.py', 'model_reference.py',
           'acceptance.py')


def validate_plan(plan):
    assert plan['schema'] == 'q4t-native-tts-encoder-fp32-v1'
    assert plan['limits'] == LIMITS and plan['thresholds'] == THRESHOLDS
    assert [c['samples'] for c in plan['cases']] == SAMPLES
    assert all(Path(c['pcm']).is_absolute() and len(c['sha256']) == 64
               for c in plan['cases'])
    assert plan['execution_dtype'] == 'F32' and plan['tf32'] is False
    assert plan['icl_qualified'] is False and plan['speech_quality_qualified'] is False
    assert plan['attention'] == 'pinned-mimi-4.57.6-eager-full-causal'


def operands(samples):
    assert type(samples) is int and 0 < samples <= 1440000
    stages = [('encoder-input', 0, samples, 64)]
    rows, channels = samples, 64
    for index, stride in enumerate((4, 5, 6, 8)):
        stages.append(('encoder-residual', index, rows, channels))
        rows = (rows + stride - 1) // stride
        channels *= 2
        stages.append(('encoder-downsample', index, rows, channels))
    stages.append(('encoder-seanet', 0, rows, 512))
    stages.extend(('encoder-transformer', i, rows, 512) for i in range(8))
    frames = (rows + 1) // 2
    stages.append(('encoder-latent', 0, frames, 512))
    stages.extend(('encoder-projection', i, frames, 256) for i in range(2))
    stages.extend(('encoder-rvq-residual', i, frames, 256) for i in range(16))
    return [(f'{name}-{index}-{side}.f32', min(count, 8) * width)
            for name, index, count, width in stages for side in range(2)]


def compare_codes(native, reference, frames):
    import numpy as np
    assert native.stat().st_size == reference.stat().st_size == frames * 16 * 4
    left = np.fromfile(native, '<i4').reshape(frames, 16)
    right = np.fromfile(reference, '<i4').reshape(frames, 16)
    assert ((left >= 0) & (left < 2048)).all()
    assert ((right >= 0) & (right < 2048)).all()
    return dict(operand='codes.i32', elements=frames * 16,
                differing=int(np.count_nonzero(left != right)),
                passed=bool(np.array_equal(left, right)))


def compare_case(native, reference, samples):
    frames = (samples + 1919) // 1920
    for directory in (native, reference):
        record = json.loads((directory / 'result.json').read_text())
        assert record['samples'] == samples and record['frames'] == frames
        assert record['transformer_rows'] == (samples + 959) // 960
        assert {p.name for p in directory.iterdir()} == {
            'result.json', 'codes.i32', *(name for name, _ in operands(samples))}
    record = json.loads((reference / 'result.json').read_text())
    assert record['mask'] == dict(rows=(samples + 959) // 960,
        final_row_sees_first=True, future_masked=True, sliding_window_applied=False)
    checks = [compare_codes(native / 'codes.i32', reference / 'codes.i32', frames)]
    checks.extend(dict(operand=name, elements=count, **compare_pair(
        native / name, reference / name, count, THRESHOLDS))
        for name, count in operands(samples))
    return checks


def compare(output, plan):
    validate_plan(plan)
    nr = json.loads((output / 'native/result.json').read_text())
    rr = json.loads((output / 'reference/result.json').read_text())
    assert nr['status'] == 'PASS_ENCODER_NATIVE_EXECUTION_ONLY'
    assert rr['status'] == 'PASS_ENCODER_REFERENCE_EXECUTION_ONLY'
    assert nr['gpu_model_loads'] == rr['cpu_model_loads'] == 1
    assert nr['cases'] == rr['cases'] == 3
    assert nr['encoder_forwards'] == rr['encoder_forwards'] == 3
    assert nr['boundary_checks'] == 15 and nr['owned_allocations_released'] is True
    assert nr['output_frames'] == sum((samples + 1919) // 1920 for samples in SAMPLES)
    assert 0 < nr['advances'] <= LIMITS['native_advances']
    assert 0 < nr['seconds'] <= LIMITS['native_seconds']
    assert 0 < rr['seconds'] <= LIMITS['reference_seconds']
    assert rr['cuda_initialized'] is False
    checks = []
    for index, samples in enumerate(SAMPLES):
        checks.extend(dict(case=index, **item) for item in compare_case(
            output / 'native' / f'case-{index}',
            output / 'reference' / f'case-{index}', samples))
    assert len(checks) == 225
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('binary', 'reference-python', 'reference-deps', 'assets',
                 'official-source', 'plan', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.environ.get('PYTHONOPTIMIZE')
    assert not any(k.startswith('Q4T_ABLATE_') for k in os.environ)
    plan = json.loads(args.plan.read_text())
    validate_plan(plan)
    assert digest(args.binary) == plan['binary_sha256']
    assert defaults() == plan['default_before']
    assert {n: digest(Path(__file__).parent / n) for n in DRIVERS} == plan['driver_sources']
    assert {n: digest(ROOT / n) for n in plan['runtime_sources']} == plan['runtime_sources']
    assert all(digest(Path(c['pcm'])) == c['sha256'] for c in plan['cases'])
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', limits=LIMITS, thresholds=THRESHOLDS,
                  defaults_before=defaults(), binary_sha256=digest(args.binary),
                  plan_sha256=digest(args.plan), runs=[])
    env = dict(os.environ, PYTHONPATH=str(args.reference_deps.absolute()),
               PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
               TRANSFORMERS_OFFLINE='1', OMP_NUM_THREADS='8')
    commands = [
        ('native', [str(args.binary.absolute()), str(args.assets.absolute()),
                    str(args.plan.absolute()), str(args.output.absolute() / 'native')]),
        ('reference', [str(args.reference_python.absolute()), '-B',
                       str(ROOT / 'tools/native_tts/encoder_reference.py'),
                       '--assets', str(args.assets.absolute()), '--official-source',
                       str(args.official_source.absolute()), '--plan', str(args.plan.absolute()),
                       '--output', str(args.output.absolute() / 'reference')])]
    try:
        for name, command in commands:
            remaining = LIMITS['seconds'] - (time.monotonic() - started)
            assert remaining > 0
            step = time.monotonic()
            record = dict(name=name, command=command)
            result['runs'].append(record)
            with (args.output / f'{name}.log').open('wb') as log:
                try:
                    child = subprocess.run(command, cwd=ROOT, env=env,
                        pass_fds=(lease,), stdout=log, stderr=subprocess.STDOUT,
                        timeout=min(remaining, LIMITS[name + '_seconds']))
                    record['exit_code'] = child.returncode
                finally:
                    record['seconds'] = time.monotonic() - step
            assert child.returncode == 0, f'{name} execution failed'
        result['comparisons'] = compare(args.output, plan)
        assert all(x['passed'] for x in result['comparisons']), 'encoder numerical comparison failed'
        result['status'] = 'PASS_ENCODER_PENDING_LEASE_RECONCILIATION'
    except Exception as error:
        result['status'], result['error'] = 'FAILED', str(error)
    finally:
        result['defaults_after'] = defaults()
        result['defaults_unchanged'] = result['defaults_after'] == result['defaults_before']
        result['runtime_sources_unchanged'] = {
            n: digest(ROOT / n) for n in plan['runtime_sources']} == plan['runtime_sources']
        if not result['defaults_unchanged'] or not result['runtime_sources_unchanged']:
            result['status'], result['error'] = 'FAILED', 'protected default or runtime source changed'
        result['seconds'] = time.monotonic() - started
        result['artifacts'] = {str(p.relative_to(args.output)): digest(p)
            for p in sorted(args.output.rglob('*')) if p.is_file()}
        (args.output / 'result.json').write_text(json.dumps(result,
            ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('status', 'seconds', 'defaults_unchanged')}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    raise SystemExit(main())
