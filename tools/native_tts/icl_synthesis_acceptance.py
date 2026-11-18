#!/usr/bin/env python3
"""Fixed-reference ICL audio/lifecycle gate; registration encoder stays separate."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import time

if __package__:
    from . import icl_acceptance as component
    from .utterance_acceptance import asr_fixture, content_check
else:
    import icl_acceptance as component
    from utterance_acceptance import asr_fixture, content_check

ROOT, defaults, digest = component.ROOT, component.defaults, component.digest
LIMITS = dict(seconds=1200, native_seconds=600, asr_seconds=600,
              spoken_cases=2, cancellation_cases=1, frame_limit_cases=2,
              max_frames_per_spoken_case=160, cancellation_frames=2,
              streaming_frame_limit=3, deferred_frame_limit=2, chunk_frames=16,
              total_advances=100000, gpu_talker_loads=1, gpu_codec_loads=1,
              gpu_asr_loads=1, gpu_reference_encoder_loads=0,
              main_llm_loads=0, http_requests=0, cpu_model_loads=0,
              artifact_mib=16, minimum_free_mib=192)
CONTRACT = dict(sampling='checkpoint-default-seed0', reference_frames=30,
                exact_normalized_transcription=True, native_sample_rate=24000,
                natural_eos=True, reference_pcm_excluded=True,
                registered_voice_api_qualified=False,
                encoder_gate_unchanged=True, listening_qualified=False)
DRIVERS = ('icl_synthesis_replay.cpp', 'icl_synthesis_acceptance.py',
           'icl_acceptance.py', 'utterance_acceptance.py', 'acceptance.py')


def validate_plan(plan):
    assert plan['schema'] == 'q4t-native-icl-synthesis-v1'
    assert plan['limits'] == LIMITS and plan['contract'] == CONTRACT
    component.validate_plan(dict(schema='q4t-native-icl-tts-component-fp32-v1',
        limits=component.LIMITS, thresholds=component.THRESHOLDS,
        cases=plan['cases'], eos_qualified=False, audio_qualified=False,
        encoder_qualified=False))


def validate_native(receipt):
    assert receipt['status'] == 'PASS_ICL_SYNTHESIS_PENDING_CONTENT'
    assert receipt['allocated'] == receipt['released'] == 12
    assert receipt['gpu_talker_loads'] == receipt['gpu_codec_loads'] == 1
    assert receipt['asset_workers_reaped'] == receipt['cases'] == 2
    assert receipt['cancellation_and_two_frame_limits'] is True
    assert 0 < receipt['advances'] <= LIMITS['total_advances']
    assert receipt['sampling_policy'] == CONTRACT['sampling']
    assert receipt['encoder_loads'] == 0


def validate_chunks(value, index):
    assert type(index) is int and index in (0, 1)
    assert value['early_output'] is (index == 0)
    assert value['deferred'] is (index == 1)
    assert value['eos'] is True and value['at_limit'] is False
    assert value['reference_frames'] == 30
    assert type(value['frames']) is int and 2 < value['frames'] < 160
    assert value['samples'] == value['frames'] * 1920
    assert value['sample_rate'] == 24000
    chunks = value['chunks']
    assert type(chunks) is list and len(chunks) >= 2
    assert all(type(c) is int and 0 < c <= 16 for c in chunks)
    assert sum(chunks) == value['frames'] and chunks[0] == 2
    assert all(c == 16 for c in chunks[1:-1])


def validate_component(plan):
    previous = json.loads(Path(plan['prompt_component_plan']).read_text())
    component.validate_plan(previous)
    assert previous['cases'] == plan['cases']
    result = json.loads(Path(plan['prompt_component_result']).read_text())
    assert result['status'] == 'PASS_ICL_COMPONENT_PENDING_LEASE_RECONCILIATION'
    assert len(result['comparisons']) == 134
    assert all(row['passed'] is True for row in result['comparisons'])
    assert result['plan_sha256'] == digest(Path(plan['prompt_component_plan']))
    lease = json.loads(Path(plan['prompt_component_lease']).read_text())
    assert lease['status'] == 'COMPLETED' and lease['child_returncode'] == 0
    # An additional isolated CMake target doesn't change the already checked
    # model arithmetic. Every runtime source and header must remain identical.
    for name, expected in previous['sources'].items():
        relative = Path(name).relative_to(ROOT)
        if relative.parts[0] in ('src', 'include'):
            assert digest(Path(name)) == expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.environ.get('PYTHONOPTIMIZE')
    assert not any(k.startswith('Q4T_ABLATE_') for k in os.environ)
    plan = json.loads(args.plan.read_text())
    validate_plan(plan)
    validate_component(plan)
    assert defaults() == plan['defaults']
    assert all(digest(ROOT / p) == h for p, h in plan['sources'].items())
    assert {n: digest(Path(__file__).parent / n) for n in DRIVERS} == plan['drivers']
    for name in ('binary', 'asr_binary'):
        assert digest(Path(plan[name])) == plan[name + '_sha256']
    space = os.statvfs(ROOT)
    assert space.f_bavail * space.f_frsize >= LIMITS['minimum_free_mib'] * 1048576
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', limits=LIMITS, contract=CONTRACT,
                  defaults_before=defaults(), plan_sha256=digest(args.plan),
                  runs=[], comparisons=[])
    child_env = dict(os.environ)

    def run(name, command):
        item = dict(name=name, command=command)
        result['runs'].append(item)
        step = time.monotonic()
        remaining = LIMITS['seconds'] - (step - started)
        assert remaining > 0
        try:
            with (args.output / (name + '.log')).open('wb') as log:
                child = subprocess.run(command, cwd=ROOT, env=child_env,
                    pass_fds=(lease,), stdout=log, stderr=subprocess.STDOUT,
                    timeout=min(LIMITS[name + '_seconds'], remaining))
            item['exit_code'] = child.returncode
            assert child.returncode == 0, name + ' failed'
        finally:
            item['seconds'] = time.monotonic() - step

    try:
        native = args.output / 'native'
        run('native', [plan['binary'], plan['assets'], str(args.plan.absolute()),
                       str(native.absolute())])
        result['native'] = json.loads((native / 'result.json').read_text())
        validate_native(result['native'])
        for index in range(2):
            value = json.loads((native / f'case-{index}/result.json').read_text())
            validate_chunks(value, index)
        # Full generated PCM, no target text/prompt, trim or tempo normalization.
        prepared = asr_fixture(native, Path(plan['asr_assets']), args.output / 'asr-input')
        run('asr', [plan['asr_binary'], plan['asr_assets'], str(prepared.absolute()),
                    str((args.output / 'asr').absolute())])
        for index, case in enumerate(plan['cases']):
            path = args.output / 'asr' / f'case-{index}'
            decoded = json.loads((path / 'result.json').read_text())
            assert decoded['eos'] is True and 0 < decoded['generated_tokens'] <= 128
            result['comparisons'].append(dict(case=index, **content_check(
                (path / 'text.txt').read_text(), case)))
        assert all(c['passed'] for c in result['comparisons']), 'speech content differs'
        result['status'] = 'PASS_FIXED_REFERENCE_ICL_AUDIO_PENDING_LEASE'
    except Exception as error:
        result['status'], result['error'] = 'FAIL', repr(error)
    finally:
        result['defaults_after'] = defaults()
        result['defaults_unchanged'] = result['defaults_after'] == plan['defaults']
        result['artifact_bytes'] = sum(p.stat().st_size for p in args.output.rglob('*')
                                       if p.is_file())
        result['seconds'] = time.monotonic() - started
        if not result['defaults_unchanged'] or result['artifact_bytes'] > 16 * 1048576:
            result['status'], result['error'] = 'FAIL', 'default/artifact guard failed'
        result['artifacts'] = {str(p.relative_to(args.output)): digest(p)
            for p in sorted(args.output.rglob('*')) if p.is_file()}
        (args.output / 'result.json').write_text(json.dumps(result, indent=2,
                                                        ensure_ascii=False) + '\n')
    print(json.dumps({k: v for k, v in result.items()
                      if k in ('status', 'error', 'seconds', 'artifact_bytes')}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    raise SystemExit(main())
