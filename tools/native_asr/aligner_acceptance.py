#!/usr/bin/env python3
"""Bounded alignment component and directly affected ASR regression checks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from acceptance import defaults, digest


LIMITS = {'seconds': 600, 'native_seconds': 120, 'asr_seconds': 120,
          'reference_seconds': 240, 'gpu_model_loads': 2,
          'cpu_reference_model_loads': 1, 'alignment_cases': 2,
          'asr_cases': 2, 'asr_decode_steps': 8, 'http_requests': 0}
THRESHOLDS = {
    'encoder-input.bf16': {'cosine': 0.99995, 'relative_l2': 0.01},
    'encoder.bf16': {'cosine': 0.9995, 'relative_l2': 0.04},
    'logits.bf16': {'cosine': 0.9995, 'relative_l2': 0.04},
    'identical_raw_timestamp_argmax': True,
    'asr_affected_outputs_bitwise_identical': True,
}


def compare(plan, output):
    import numpy as np
    cases = json.loads(Path(plan['alignment_plan']).read_text())['cases']
    checks = []
    for index, case in enumerate(cases):
        native = output / 'native' / f'case-{index}'
        reference = output / 'reference' / f'case-{index}'
        for operand, threshold in THRESHOLDS.items():
            if not operand.endswith('.bf16'):
                continue
            values = []
            for directory in (native, reference):
                data = (np.fromfile(directory / operand, '<u2').astype(
                    np.uint32) << 16).view(np.float32).astype(np.float64)
                expected = (len(case['words']) * 2 * 5000
                            if operand == 'logits.bf16'
                            else case['audio_tokens'] * 1024)
                assert data.size == expected and np.isfinite(data).all()
                values.append(data)
            a, b = values
            norm = float(np.linalg.norm(b))
            cosine = float(a @ b / (np.linalg.norm(a) * norm))
            relative = float(np.linalg.norm(a - b) / norm)
            checks.append({'case': index, 'operand': operand,
                'cosine': cosine, 'relative_l2': relative,
                'max_abs': float(np.max(np.abs(a - b))),
                'passed': cosine >= threshold['cosine'] and
                          relative <= threshold['relative_l2']})
        a = np.fromfile(native / 'classes.u32', '<u4')
        b = np.fromfile(reference / 'classes.u32', '<u4')
        assert a.shape == b.shape == (len(case['words']) * 2,)
        assert bool((a < 5000).all()) and bool((b < 5000).all())
        checks.append({'case': index, 'operand': 'raw_timestamp_argmax',
            'passed': bool(np.array_equal(a, b)), 'native': a.tolist(),
            'reference': b.tolist(), 'different_slots': int((a != b).sum()),
            'max_delta_ms': int(np.abs(a.astype(int) - b.astype(int)).max()) * 80})
        prior = Path(plan['asr_prior']) / f'case-{index}'
        current = output / 'asr' / f'case-{index}'
        for name in ['encoder-input.bf16', 'encoder.bf16'] + [
                f'logits-{step}.bf16' for step in range(4)]:
            checks.append({'case': index, 'operand': 'asr/' + name,
                           'passed': digest(prior / name) == digest(current / name)})
        before = (prior / 'generated.u32').read_bytes()[:4 * 4]
        after = (current / 'generated.u32').read_bytes()
        checks.append({'case': index, 'operand': 'asr/greedy_prefix',
                       'passed': len(after) == 4 * 4 and before == after})
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    os.fstat(lease)
    assert not os.environ.get('PYTHONOPTIMIZE')
    plan = json.loads(args.plan.read_text())
    assert plan['limits'] == LIMITS and plan['thresholds'] == THRESHOLDS
    for path, expected in plan['identities'].items():
        assert digest(Path(path)) == expected, 'input identity differs: ' + path
    alignment = json.loads(Path(plan['alignment_plan']).read_text())
    asr = json.loads(Path(plan['asr_plan']).read_text())
    assert len(alignment['cases']) == len(asr['cases']) == 2
    assert [c['frames'] for c in alignment['cases']] == [420, 1505]
    assert [c['max_new_tokens'] for c in asr['cases']] == [4, 4]
    args.output.mkdir(exist_ok=False, parents=True)
    started = time.monotonic()
    result = {'status': 'RUNNING', 'limits': LIMITS, 'thresholds': THRESHOLDS,
              'plan_sha256': digest(args.plan), 'defaults_before': defaults(),
              'runs': []}
    env = dict(os.environ, PYTHONPATH=plan['reference_deps'],
               PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
               TRANSFORMERS_OFFLINE='1', OMP_NUM_THREADS='8')
    commands = [
        ('native', [plan['aligner_binary'], plan['aligner_assets'],
                    plan['alignment_plan'], str(args.output / 'native')]),
        ('asr', [plan['asr_binary'], plan['asr_assets'], plan['asr_plan'],
                 str(args.output / 'asr')]),
        ('reference', [plan['reference_python'], '-B',
                       str(Path(__file__).with_name('aligner_reference.py')),
                       '--assets', plan['aligner_assets'], '--official-source',
                       plan['official_source'], '--plan', plan['alignment_plan'],
                       '--output', str(args.output / 'reference')]),
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
                    child = subprocess.run(command, env=env, pass_fds=(lease,),
                        stdout=log, stderr=subprocess.STDOUT,
                        timeout=min(remaining, LIMITS[name + '_seconds']))
                    record['exit'] = child.returncode
                finally:
                    record['seconds'] = time.monotonic() - step
            assert child.returncode == 0, name + ' execution failed'
        result['checks'] = compare(plan, args.output)
        assert all(c['passed'] for c in result['checks']), 'component checks failed'
        result['status'] = 'PASS_COMPONENT_PENDING_LEASE_RECONCILIATION'
    except Exception as error:
        result['status'] = 'FAILED'
        result['error'] = str(error)
    finally:
        result['seconds'] = time.monotonic() - started
        result['defaults_after'] = defaults()
        result['defaults_unchanged'] = result['defaults_after'] == (
            result['defaults_before'])
        if not result['defaults_unchanged']:
            result['status'] = 'FAILED'
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('status', 'seconds')}, indent=2))
    raise SystemExit(0 if result['status'].startswith('PASS_') else 1)


if __name__ == '__main__':
    main()
