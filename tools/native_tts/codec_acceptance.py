#!/usr/bin/env python3
"""Bounded native codec numerical qualification, not spoken-content acceptance."""
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
              gpu_model_loads=1, cpu_reference_model_loads=1, cases=3,
              decoder_forwards=5, input_frames=155, output_samples=218880,
              native_advances=4096, main_llm_loads=0, talker_loads=0,
              http_requests=0)
THRESHOLDS = dict(cosine=0.999999, relative_l2=0.001)
FIRST_CODES = [
    [1995, 1159, 355, 22, 1174, 1093, 625, 1814, 1058, 905, 1846, 1247,
     1677, 889, 812, 901],
    [1028, 1650, 1713, 929, 1226, 567, 1953, 426, 439, 748, 1606, 1761,
     747, 6, 896, 1765]]
CASES = [dict(codes=FIRST_CODES, chunk_frames=300)] + [
    dict(codes=[[(row * 197 + group * 131 + row * group * 17 + seed) % 2048
                 for group in range(16)] for row in range(frames)],
         chunk_frames=chunk)
    for frames, chunk, seed in ((75, 300, 43), (37, 16, 97))]
# Independently frozen expected slicing of the official 25-frame context.
CHUNKS = [[(0, 2, 0, 2)], [(0, 75, 0, 75)],
          [(0, 16, 0, 16), (0, 32, 16, 16), (7, 30, 25, 5)]]
DRIVERS = ('codec_replay.cpp', 'codec_reference.py', 'codec_acceptance.py',
           'acceptance.py', 'model_reference.py')


def validate_plan(plan):
    assert plan['schema'] == 'q4t-native-tts-codec-fp32-v1'
    assert plan['limits'] == LIMITS and plan['thresholds'] == THRESHOLDS
    assert plan['cases'] == CASES
    assert plan['eos_qualified'] is False and plan['speech_quality_qualified'] is False
    assert plan['execution_dtype'] == 'F32' and plan['tf32'] is False


def operands(frames):
    assert type(frames) is int and 0 < frames <= 325
    stages = [('codec-rvq', 0, 1, 512), ('codec-pre-conv', 0, 1, 1024),
              ('codec-transformer', 0, 1, 1024),
              ('codec-upsample', 0, 2, 1024), ('codec-upsample', 1, 4, 1024),
              ('codec-vocoder', 0, 32, 768), ('codec-vocoder', 1, 160, 384),
              ('codec-vocoder', 2, 640, 192), ('codec-vocoder', 3, 1920, 96)]
    return [(f'{name}-{index}-{side}.f32', min(frames * factor, 8) * width)
            for name, index, factor, width in stages for side in range(2)]


def compare_case(native, reference, index):
    import numpy as np
    checks = []
    frames = len(CASES[index]['codes'])
    nr = json.loads((native / 'result.json').read_text())
    rr = json.loads((reference / 'result.json').read_text())
    assert nr['samples'] == rr['samples'] == frames * 1920
    assert len(nr['chunks']) == len(rr['chunks']) == len(CHUNKS[index])
    expected_dirs = {f'chunk-{i}' for i in range(len(CHUNKS[index]))}
    for directory in (native, reference):
        assert {p.name for p in directory.iterdir() if p.is_dir()} == expected_dirs
        assert {p.name for p in directory.glob('*.f32')} == {'pcm.f32'}
        pcm = np.fromfile(directory / 'pcm.f32', '<f4')
        assert pcm.size == frames * 1920 and np.isfinite(pcm).all()
        assert np.max(np.abs(pcm)) <= 1 and float(np.linalg.norm(pcm)) > 0
    checks.append(dict(case=index, operand='pcm.f32', elements=frames * 1920,
                       **compare_pair(native / 'pcm.f32', reference / 'pcm.f32',
                                      frames * 1920, THRESHOLDS)))
    for chunk, (start, count, discard, output) in enumerate(CHUNKS[index]):
        n, r = nr['chunks'][chunk], rr['chunks'][chunk]
        assert n['input_frames'] == r['input_frames'] == count
        assert n['input_begin'] == start and n['discard_samples'] == discard * 1920
        assert n['output_samples'] == output * 1920
        assert 0 < n['workspace_bytes'] < 1 << 30
        names = operands(count)
        left, right = native / f'chunk-{chunk}', reference / f'chunk-{chunk}'
        for directory in (left, right):
            assert {p.name for p in directory.iterdir()} == {n for n, _ in names}
        for name, elements in names:
            checks.append(dict(case=index, chunk=chunk, operand=name,
                               elements=elements,
                               **compare_pair(left / name, right / name,
                                              elements, THRESHOLDS)))
    return checks


def compare(output, plan):
    validate_plan(plan)
    nr = json.loads((output / 'native/result.json').read_text())
    rr = json.loads((output / 'reference/result.json').read_text())
    assert nr['status'] == 'PASS_CODEC_NATIVE_EXECUTION_ONLY'
    assert nr['gpu_model_loads'] == 1 and nr['cases'] == 3
    assert nr['decoder_forwards'] == rr['decoder_forwards'] == 5
    assert nr['input_frames'] == 155 and nr['output_samples'] == 218880
    assert 0 < nr['advances'] <= LIMITS['native_advances']
    assert 0 < nr['seconds'] < LIMITS['native_seconds']
    assert rr['status'] == 'PASS_CODEC_REFERENCE_EXECUTION_ONLY'
    assert rr['cpu_model_loads'] == 1 and rr['cuda_initialized'] is False
    assert 0 < rr['seconds'] < LIMITS['reference_seconds']
    return [check for index in range(3) for check in compare_case(
        output / 'native' / f'case-{index}',
        output / 'reference' / f'case-{index}', index)]


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
                       str(ROOT / 'tools/native_tts/codec_reference.py'),
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
        assert len(result['comparisons']) == 93
        assert all(x['passed'] for x in result['comparisons']), 'codec numerical comparison failed'
        result['status'] = 'PASS_CODEC_PENDING_LEASE_RECONCILIATION'
    except Exception as error:
        result['status'], result['error'] = 'FAILED', str(error)
    finally:
        result['defaults_after'] = defaults()
        result['defaults_unchanged'] = result['defaults_after'] == result['defaults_before']
        if not result['defaults_unchanged']:
            result['status'], result['error'] = 'FAILED', 'protected default changed'
        result['seconds'] = time.monotonic() - started
        result['artifacts'] = {str(p.relative_to(args.output)): digest(p)
            for p in sorted(args.output.rglob('*')) if p.is_file()}
        (args.output / 'result.json').write_text(json.dumps(result,
            ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('status', 'seconds', 'defaults_unchanged')}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    raise SystemExit(main())
