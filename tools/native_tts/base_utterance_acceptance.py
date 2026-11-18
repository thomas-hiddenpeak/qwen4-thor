#!/usr/bin/env python3
"""Two complete utterances with natural EOS and independent ASR content check."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import time

if __package__:
    from . import base_acceptance as component
    from . import utterance_acceptance as utterance
    from .speaker_acceptance import pair
else:
    import base_acceptance as component
    import utterance_acceptance as utterance
    from speaker_acceptance import pair

ROOT, defaults, digest = component.ROOT, component.defaults, component.digest
LIMITS = dict(utterance.LIMITS, gpu_speaker_loads=1, speaker_stages=94,
              minimum_free_mib=256, artifact_mib=16)
CONTRACT = dict(utterance.CONTRACT, native_sample_condition=True,
                speaker_cosine=0.99999999, speaker_relative_l2=0.0001,
                speaker_max_abs=0.001, registered_voice_api_qualified=False)
DRIVERS = ('base_utterance_replay.cpp', 'base_utterance_acceptance.py',
           'utterance_acceptance.py', 'base_acceptance.py', 'acceptance.py',
           'speaker_acceptance.py')


def validate_plan(plan):
    assert plan['schema'] == 'q4t-native-base-tts-utterance-v1'
    assert plan['limits'] == LIMITS and plan['contract'] == CONTRACT
    assert len(plan['cases']) == 2
    conditions = []
    samples = set()
    for row in plan['cases']:
        condition = {k: v for k, v in row.items() if k not in
            ('reference_pcm', 'reference_pcm_sha256')}
        conditions.append(condition)
        path = Path(row['reference_pcm'])
        assert path.is_file() and 1280 * 4 <= path.stat().st_size <= 1440000 * 4
        assert path.stat().st_size % 4 == 0
        assert digest(path) == row['reference_pcm_sha256']
        samples.add(row['reference_pcm_sha256'])
    component.validate_plan(dict(schema='q4t-native-base-tts-component-fp32-v1',
        limits=component.LIMITS, thresholds=component.THRESHOLDS,
        cases=conditions, eos_qualified=False, audio_qualified=False))
    assert len(samples) == 2


def validate_native(record):
    assert record['status'] == 'PASS_UTTERANCE_PENDING_CONTENT_CHECK'
    assert record['gpu_model_loads'] == 3 and record['cases'] == 2
    assert record['speaker_stages'] == 94
    assert 4 <= record['frames'] < 320
    assert 0 < record['generation_advances'] <= 48000
    assert 0 < record['codec_advances'] <= 4096


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('binary', 'asr-binary', 'assets', 'asr-assets', 'plan', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.environ.get('PYTHONOPTIMIZE')
    assert not any(k.startswith('Q4T_ABLATE_') for k in os.environ)
    plan = json.loads(args.plan.read_text())
    validate_plan(plan)
    assert defaults() == plan['default_before']
    assert digest(args.binary) == plan['binary_sha256']
    assert digest(args.asr_binary) == plan['asr_binary_sha256']
    assert {n: digest(Path(__file__).parent / n) for n in DRIVERS} == plan['driver_sources']
    assert {n: digest(ROOT / n) for n in plan['runtime_sources']} == plan['runtime_sources']
    component_result = json.loads(Path(plan['component_result']).read_text())
    component_lease = json.loads(Path(plan['component_lease']).read_text())
    assert component_result['status'] == 'PASS_COMPONENT_PENDING_LEASE_RECONCILIATION'
    assert len(component_result['comparisons']) == 134
    assert all(row['passed'] for row in component_result['comparisons'])
    assert component_lease['status'] == 'COMPLETED' and component_lease['child_returncode'] == 0
    space = os.statvfs(args.output.parent)
    assert space.f_bavail * space.f_frsize >= 256 * 1024**2
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', limits=LIMITS, contract=CONTRACT,
                  defaults_before=defaults(), runs=[], plan_sha256=digest(args.plan))
    # Capture before CPU feature generation temporarily hides CUDA from Python.
    child_env = dict(os.environ)

    def run(name, command):
        remaining = LIMITS['seconds'] - (time.monotonic() - started)
        assert remaining > 0
        record = dict(name=name, command=command)
        result['runs'].append(record)
        step = time.monotonic()
        with (args.output / (name + '.log')).open('wb') as log:
            try:
                child = subprocess.run(command, cwd=ROOT, env=child_env,
                    pass_fds=(lease,), stdout=log, stderr=subprocess.STDOUT,
                    timeout=min(remaining, LIMITS[name + '_seconds']))
                record['exit_code'] = child.returncode
            finally:
                record['seconds'] = time.monotonic() - step
        assert child.returncode == 0, name + ' execution failed'

    try:
        run('native', [str(args.binary.absolute()), str(args.assets.absolute()),
                      str(args.plan.absolute()), str(args.output.absolute() / 'native')])
        nr = json.loads((args.output / 'native/result.json').read_text())
        validate_native(nr)
        result['speaker_comparisons'] = [dict(case=i, **pair(
            args.output / 'native' / f'case-{i}' / 'speaker.f32',
            Path(row['speaker_embedding']), 2048,
            dict(cosine=CONTRACT['speaker_cosine'],
                 relative_l2=CONTRACT['speaker_relative_l2'],
                 max_abs=CONTRACT['speaker_max_abs'])))
            for i, row in enumerate(plan['cases'])]
        assert all(row['passed'] for row in result['speaker_comparisons'])
        prepared = utterance.asr_fixture(args.output / 'native', args.asr_assets,
                               args.output / 'asr-input')
        run('asr', [str(args.asr_binary.absolute()), str(args.asr_assets.absolute()),
                   str(prepared.absolute()), str(args.output.absolute() / 'asr')])
        checks = []
        result['comparisons'] = checks
        for index, case in enumerate(plan['cases']):
            directory = args.output / 'asr' / f'case-{index}'
            receipt = json.loads((directory / 'result.json').read_text())
            assert receipt['eos'] is True and 0 < receipt['generated_tokens'] <= 128
            checks.append(dict(case=index, **utterance.content_check(
                (directory / 'text.txt').read_text(), case)))
        assert all(c['passed'] for c in checks), 'actual speech content differs'
        result['native'] = nr
        result['status'] = 'PASS_BASE_UTTERANCE_PENDING_LEASE_RECONCILIATION'
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
            ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: result[k] for k in ('status', 'seconds', 'defaults_unchanged')}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    raise SystemExit(main())
