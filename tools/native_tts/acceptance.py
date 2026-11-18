#!/usr/bin/env python3
"""Two-frame TTS numerical qualification; does not certify spoken audio."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
LIMITS = {'seconds': 1200, 'native_seconds': 600, 'reference_seconds': 600,
          'gpu_model_loads': 1, 'cpu_reference_model_loads': 1, 'cases': 2,
          'frames_per_case': 2, 'native_advances': 600,
          'reference_talker_forwards': 4, 'reference_predictor_forwards': 60,
          'main_llm_loads': 0, 'http_requests': 0}
THRESHOLDS = {'conditioning': {'cosine': 0.99995, 'relative_l2': 0.01},
              'dense': {'cosine': 0.9995, 'relative_l2': 0.04},
              'identical_teacher_forced_greedy_codes': True}
CASES = [dict(text='Hello, this is a native speech test.',
              instruction='Speak calmly.', voice='ryan', language='english',
              non_streaming=False),
         dict(text='你好，会议下午三点开始。', instruction='用温柔的语气说话。',
              voice='vivian', language='chinese', non_streaming=True)]


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1 << 20):
            value.update(chunk)
    return value.hexdigest()


def defaults():
    return {name: digest(ROOT / name) for name in (
        'build/q4t', 'build/q4t.release.json', 'build/service/manifest.json',
        'build/service.release.json', 'build/service-native-state/ledger.json')}


def validate_plan(plan):
    assert plan['schema'] == 'q4t-native-tts-component-v1'
    assert plan['limits'] == LIMITS and plan['thresholds'] == THRESHOLDS
    assert plan['cases'] == CASES
    assert plan['eos_qualified'] is False and plan['audio_qualified'] is False


def operands(native_result):
    text, prefill = native_result['text_tokens'], native_result['prefill_tokens']
    assert type(text) is int and 0 < text <= 512
    assert type(prefill) is int and 0 < prefill <= 512
    names = [('text-projected-0-0.bf16', text * 2048, 'conditioning'),
             ('prefill-0-0.bf16', prefill * 2048, 'conditioning')]
    for frame in range(2):
        names += [(f'talker-hidden-{frame}-0.bf16', 2048, 'dense'),
                  (f'talker-logits-{frame}-0.bf16', 3072, 'dense')]
        for group in range(1, 16):
            names += [(f'predictor-hidden-{frame}-{group}.bf16', 1024, 'dense'),
                      (f'predictor-logits-{frame}-{group}.bf16', 2048, 'dense')]
    return names


def compare_pair(native, reference, count, threshold):
    import numpy as np
    assert native.stat().st_size == reference.stat().st_size == count * 2

    def read(path):
        return (np.fromfile(path, '<u2').astype(np.uint32) << 16).view(
            np.float32).astype(np.float64)

    a, b = read(native), read(reference)
    assert a.size == b.size == count
    assert np.isfinite(a).all() and np.isfinite(b).all(), 'nonfinite TTS operand'
    anorm, bnorm = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    assert anorm > 0 and bnorm > 0, 'zero TTS operand'
    cosine = float(a @ b / (anorm * bnorm))
    relative = float(np.linalg.norm(a - b) / bnorm)
    return dict(cosine=cosine, relative_l2=relative,
                max_abs=float(np.max(np.abs(a - b))),
                passed=bool(cosine >= threshold['cosine'] and
                            relative <= threshold['relative_l2']))


def compare_codes(native, reference):
    import numpy as np
    assert native.stat().st_size == 2 * 16 * 4
    codes = np.fromfile(native, '<i4').reshape(2, 16)
    expected = np.asarray(reference['expected_greedy_codes'])
    assert expected.shape == codes.shape and expected.dtype.kind in 'iu'
    assert ((codes >= 0) & (codes < 2048)).all()
    assert ((expected >= 0) & (expected < 2048)).all()
    assert reference['teacher_forced_frames'] == 2
    assert reference['eos_qualified'] is False
    assert reference['native_codes'] == codes.tolist()
    return dict(check='teacher_forced_greedy_codes',
                native=codes.tolist(), reference=expected.tolist(),
                differing=int(np.count_nonzero(codes != expected)),
                passed=bool(np.array_equal(codes, expected)))


def compare(output, plan):
    validate_plan(plan)
    checks = []
    nr = json.loads((output / 'native/result.json').read_text())
    rr = json.loads((output / 'reference/result.json').read_text())
    assert nr['status'] == 'PASS_NATIVE_EXECUTION_ONLY'
    assert nr['gpu_model_loads'] == 1 and nr['cases'] == 2 and nr['frames'] == 4
    assert 0 < nr['advances'] <= LIMITS['native_advances']
    assert 0 < nr['seconds'] < LIMITS['native_seconds']
    assert rr['status'] == 'PASS_CPU_REFERENCE_EXECUTION_ONLY'
    assert rr['cpu_model_loads'] == 1 and rr['cuda_initialized'] is False
    assert rr['forwards'] == {'talker': 4, 'predictor': 60}
    assert 0 < rr['seconds'] < LIMITS['reference_seconds']
    for index in range(2):
        native = output / 'native' / f'case-{index}'
        reference = output / 'reference' / f'case-{index}'
        record = json.loads((native / 'result.json').read_text())
        assert record['frames'] == 2 and record['groups'] == 16
        assert record['at_limit'] is True and record['eos'] is False
        assert 0 < record['workspace_bytes'] <= 512 << 20
        names = operands(record)
        assert len(names) == 66
        for directory in (native, reference):
            assert {p.name for p in directory.glob('*.bf16')} == {n[0] for n in names}
        for name, count, kind in names:
            checks.append(dict(case=index, operand=name, elements=count,
                **compare_pair(native / name, reference / name, count,
                               THRESHOLDS[kind])))
        checks.append(dict(case=index, **compare_codes(native / 'codes.i32',
            json.loads((reference / 'result.json').read_text()))))
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
    assert not any(key.startswith('Q4T_ABLATE_') for key in os.environ)
    plan = json.loads(args.plan.read_text())
    validate_plan(plan)
    assert digest(args.binary) == plan['binary_sha256']
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', limits=LIMITS, thresholds=THRESHOLDS,
                  defaults_before=defaults(), binary_sha256=digest(args.binary),
                  plan_sha256=digest(args.plan), runs=[])
    source_names = ('model_replay.cpp', 'model_reference.py', 'acceptance.py')
    result['driver_sources'] = {n: digest(Path(__file__).parent / n)
                                for n in source_names}
    assert result['driver_sources'] == plan['driver_sources']
    env = dict(os.environ, PYTHONPATH=str(args.reference_deps.absolute()),
               PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
               TRANSFORMERS_OFFLINE='1', OMP_NUM_THREADS='8')
    commands = [
        ('native', [str(args.binary.absolute()), str(args.assets.absolute()),
                    str(args.plan.absolute()), str(args.output.absolute() / 'native')]),
        ('reference', [str(args.reference_python.absolute()), '-B',
                       str(ROOT / 'tools/native_tts/model_reference.py'),
                       '--assets', str(args.assets.absolute()), '--official-source',
                       str(args.official_source.absolute()), '--plan', str(args.plan.absolute()),
                       '--native', str(args.output.absolute() / 'native'),
                       '--output', str(args.output.absolute() / 'reference')])]
    try:
        for name, command in commands:
            remaining = LIMITS['seconds'] - (time.monotonic() - started)
            assert remaining > 0
            step = time.monotonic()
            record = {'name': name, 'command': command}
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
        assert len(result['comparisons']) == 134
        assert all(x['passed'] for x in result['comparisons']), 'TTS numerical comparison failed'
        result['status'] = 'PASS_COMPONENT_PENDING_LEASE_RECONCILIATION'
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
