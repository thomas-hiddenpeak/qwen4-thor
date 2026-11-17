#!/usr/bin/env python3
"""Bounded native ASR component qualification inside one strict GPU lease."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parents[2]
LIMITS = {'seconds': 1200, 'native_seconds': 600, 'reference_seconds': 600,
          'gpu_model_loads': 1, 'cpu_reference_model_loads': 1, 'cases': 2,
          'native_decode_steps': 256, 'reference_decoder_forwards': 264,
          'http_requests': 0}
THRESHOLDS = {
    'encoder-input.bf16': {'cosine': 0.99995, 'relative_l2': 0.01},
    'encoder.bf16': {'cosine': 0.9995, 'relative_l2': 0.04},
    'logits': {'cosine': 0.9995, 'relative_l2': 0.04},
    'identical_teacher_forced_argmax': True,
    'identical_free_generation_text_and_eos': True,
}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as file:
        while chunk := file.read(1 << 20):
            value.update(chunk)
    return value.hexdigest()


def defaults():
    return {name: digest(ROOT / name) for name in [
        'build/q4t', 'build/q4t.release.json', 'build/service/manifest.json',
        'build/service.release.json', 'build/service-native-state/ledger.json']}


def compare(output, plan):
    import numpy as np
    checks = []

    def read(path):
        return (np.fromfile(path, '<u2').astype(np.uint32) << 16).view(
            np.float32).astype(np.float64)

    for index, case in enumerate(plan['cases']):
        native = output / 'native' / f'case-{index}'
        reference = output / 'reference' / f'case-{index}'
        count = min(4, json.loads((native / 'result.json').read_text())[
            'generated_tokens'])
        names = ['encoder-input.bf16', 'encoder.bf16'] + [
            f'logits-{i}.bf16' for i in range(count)]
        for name in names:
            a, b = read(native / name), read(reference / name)
            assert a.shape == b.shape and np.isfinite(a).all() and np.isfinite(b).all()
            expected = case['audio_tokens'] * (
                896 if name == 'encoder-input.bf16' else 1024)
            if name.startswith('logits-'):
                expected = 151936
            assert a.size == expected
            norm = float(np.linalg.norm(b))
            cosine = float(a @ b / (np.linalg.norm(a) * norm))
            relative = float(np.linalg.norm(a - b) / norm)
            threshold = THRESHOLDS['logits' if name.startswith('logits-') else name]
            passed = cosine >= threshold['cosine'] and relative <= threshold['relative_l2']
            entry = {'case': index, 'operand': name, 'cosine': cosine,
                     'relative_l2': relative, 'max_abs': float(np.max(np.abs(a - b)))}
            if name.startswith('logits-'):
                entry.update(native_argmax=int(a.argmax()), reference_argmax=int(b.argmax()))
                passed = passed and entry['native_argmax'] == entry['reference_argmax']
            entry['passed'] = bool(passed)
            checks.append(entry)
        na, rb = json.loads((native / 'result.json').read_text()), json.loads(
            (reference / 'result.json').read_text())
        checks.append({'case': index, 'check': 'real_audio_generation',
                       'passed': bool(na['eos'] and rb['eos'] and
                           (native / 'text.txt').read_bytes() ==
                           (reference / 'text.txt').read_bytes()),
                       'native_text': (native / 'text.txt').read_text(),
                       'reference_text': (reference / 'text.txt').read_text()})
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['binary', 'reference-python', 'reference-deps', 'assets',
                 'official-source', 'plan', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    os.fstat(lease)
    assert not os.environ.get('PYTHONOPTIMIZE')
    assert not any(key.startswith('Q4T_ABLATE_') for key in os.environ)
    plan = json.loads(args.plan.read_text())
    assert len(plan['cases']) == LIMITS['cases']
    assert [c['frames'] for c in plan['cases']] == [420, 1505]
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = {'status': 'RUNNING', 'limits': LIMITS, 'thresholds': THRESHOLDS,
              'defaults_before': defaults(), 'binary_sha256': digest(args.binary),
              'plan_sha256': digest(args.plan), 'runs': []}
    for name in ['model_replay.cpp', 'model_reference.py', 'acceptance.py']:
        result.setdefault('driver_sources', {})[name] = digest(Path(__file__).parent / name)
    env = dict(os.environ, PYTHONPATH=str(args.reference_deps.absolute()),
               PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
               TRANSFORMERS_OFFLINE='1', OMP_NUM_THREADS='8')
    # Keep the venv interpreter path intact; resolving its symlink loses venv.
    commands = [
        ('native', [str(args.binary.absolute()), str(args.assets.absolute()),
                    str(args.plan.absolute()), str(args.output.absolute() / 'native')]),
        ('reference', [str(args.reference_python.absolute()), '-B',
                       str(ROOT / 'tools/native_asr/model_reference.py'),
                       '--assets', str(args.assets.absolute()), '--official-source',
                       str(args.official_source.absolute()), '--plan', str(args.plan.absolute()),
                       '--native', str(args.output.absolute() / 'native'),
                       '--output', str(args.output.absolute() / 'reference')]),
    ]
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
        assert all(x['passed'] for x in result['comparisons']), 'numerical/business comparison failed'
        result['status'] = 'PASS_COMPONENT_PENDING_LEASE_RECONCILIATION'
    except Exception as error:
        result['status'] = 'FAILED'
        result['error'] = str(error)
    finally:
        result['defaults_after'] = defaults()
        result['defaults_unchanged'] = result['defaults_after'] == result['defaults_before']
        if not result['defaults_unchanged']:
            result['status'] = 'FAILED'
            result['error'] = 'protected default changed'
        result['seconds'] = time.monotonic() - started
        result['artifacts'] = {str(p.relative_to(args.output)): digest(p)
            for p in sorted(args.output.rglob('*')) if p.is_file()}
        (args.output / 'result.json').write_text(json.dumps(result,
            ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ['status', 'seconds', 'defaults_unchanged']}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    raise SystemExit(main())
