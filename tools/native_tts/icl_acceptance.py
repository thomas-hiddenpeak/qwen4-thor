#!/usr/bin/env python3
"""Fixed-reference ICL prompt qualification; encoder/EOS/audio remain separate."""
import argparse
import fcntl
import math
import json
import os
from pathlib import Path
import stat
import subprocess
import struct
import time

if __package__:
    from . import acceptance as core
else:
    import acceptance as core

ROOT, THRESHOLDS = core.ROOT, core.THRESHOLDS
digest, defaults, operands = core.digest, core.defaults, core.operands
compare_pair, compare_codes = core.compare_pair, core.compare_codes
LIMITS = dict(core.LIMITS, minimum_free_mib=256, artifact_mib=16)
CASES = [{k: v for k, v in row.items() if k != 'voice'} for row in core.CASES]


REFERENCE_TEXT = 'The blue train arrives at noon.'
REFERENCE_SAMPLES = 55693


def read_reference(row):
    path = Path(row['reference_codes'])
    assert path.is_absolute() and path.is_file()
    assert path.stat().st_size == ((REFERENCE_SAMPLES + 1919) // 1920) * 16 * 4
    assert digest(path) == row['reference_codes_sha256']
    values = struct.unpack('<' + 'i' * (path.stat().st_size // 4), path.read_bytes())
    assert all(0 <= code < 2048 for code in values)
    return [list(values[i:i + 16]) for i in range(0, len(values), 16)]


def validate_plan(plan):
    assert plan['schema'] == 'q4t-native-icl-tts-component-fp32-v1'
    assert plan['limits'] == LIMITS and plan['thresholds'] == THRESHOLDS
    assert len(plan['cases']) == 2
    private = ('speaker_embedding', 'speaker_embedding_sha256',
               'reference_codes', 'reference_codes_sha256', 'reference_text',
               'reference_samples')
    for expected, case in zip(CASES, plan['cases']):
        assert {k: v for k, v in case.items() if k not in private} == expected
        path = Path(case['speaker_embedding'])
        assert path.is_absolute() and path.is_file() and path.stat().st_size == 8192
        assert digest(path) == case['speaker_embedding_sha256']
        values = struct.unpack('<2048f', path.read_bytes())
        assert all(math.isfinite(x) for x in values) and any(values)
        assert case['reference_text'] == REFERENCE_TEXT
        assert type(case['reference_samples']) is int
        assert case['reference_samples'] == REFERENCE_SAMPLES
        read_reference(case)
    # Isolate two prompt alignment modes from any change of reference speaker.
    assert all(plan['cases'][0][k] == plan['cases'][1][k] for k in private)
    assert plan['eos_qualified'] is False and plan['audio_qualified'] is False
    assert plan['encoder_qualified'] is False


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
    assert rr['icl_inputs_applied'] == 2 and rr['base_reference_reused'] is True
    assert rr['forwards'] == {'talker': 4, 'predictor': 60}
    assert 0 < rr['seconds'] < LIMITS['reference_seconds']
    for index in range(2):
        native = output / 'native' / f'case-{index}'
        reference = output / 'reference' / f'case-{index}'
        record = json.loads((native / 'result.json').read_text())
        assert record['frames'] == 2 and record['groups'] == 16
        assert record['at_limit'] is True and record['eos'] is False
        assert 0 < record['workspace_bytes'] <= 512 << 20
        assert record['reference_frames'] == 30
        names = operands(record)
        assert len(names) == 66
        for directory in (native, reference):
            assert {p.name for p in directory.glob('*.f32')} == {n[0] for n in names}
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
    assert defaults() == plan['defaults']
    assert all(digest(Path(p)) == h for p, h in plan['sources'].items())
    space = os.statvfs(args.output.parent)
    assert space.f_bavail * space.f_frsize >= 256 * 1024**2
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', limits=LIMITS, thresholds=THRESHOLDS,
                  defaults_before=defaults(), binary_sha256=digest(args.binary),
                  plan_sha256=digest(args.plan), runs=[])
    source_names = ('icl_replay.cpp', 'icl_reference.py', 'icl_acceptance.py',
                    'base_reference.py', 'acceptance.py')
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
                       str(ROOT / 'tools/native_tts/icl_reference.py'),
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
        result['status'] = 'PASS_ICL_COMPONENT_PENDING_LEASE_RECONCILIATION'
    except Exception as error:
        result['status'], result['error'] = 'FAILED', str(error)
    finally:
        result['defaults_after'] = defaults()
        result['defaults_unchanged'] = result['defaults_after'] == result['defaults_before']
        if not result['defaults_unchanged']:
            result['status'], result['error'] = 'FAILED', 'protected default changed'
        result['seconds'] = time.monotonic() - started
        result['artifact_bytes'] = sum(p.stat().st_size for p in args.output.rglob('*') if p.is_file())
        if result['artifact_bytes'] > 16 * 1024**2:
            result.update(status='FAILED', error='artifact storage budget exceeded')
        result['artifacts'] = {str(p.relative_to(args.output)): digest(p)
            for p in sorted(args.output.rglob('*')) if p.is_file()}
        (args.output / 'result.json').write_text(json.dumps(result,
            ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('status', 'seconds', 'defaults_unchanged')}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    raise SystemExit(main())
