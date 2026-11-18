#!/usr/bin/env python3
"""Bounded real incremental synthesis, cancellation and output-budget gate."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

from acceptance import ROOT, CASES, defaults, digest
from utterance_acceptance import asr_fixture, content_check

LIMITS = dict(seconds=600, native_seconds=180, asr_seconds=180,
              spoken_cases=2, cancellation_cases=1, frame_limit_cases=1,
              max_frames_per_spoken_case=160, cancellation_frames=16,
              frame_limit=2, chunk_frames=16, total_advances=75000,
              gpu_talker_loads=1, gpu_codec_loads=1, gpu_asr_loads=1,
              main_llm_loads=0, http_requests=0, cpu_model_loads=0,
              artifact_mib=16, disk_headroom_mib=512)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.environ.get('PYTHONOPTIMIZE')
    assert not any(k.startswith('Q4T_ABLATE_') for k in os.environ)
    plan = json.loads(args.plan.read_text())
    assert plan['schema'] == 'q4t-native-tts-synthesis-v1'
    assert plan['limits'] == LIMITS and plan['cases'] == CASES
    assert defaults() == plan['defaults']
    assert all(digest(ROOT/p) == h for p, h in plan['sources'].items())
    for name in ('binary', 'asr_binary'):
        assert digest(Path(plan[name])) == plan[name+'_sha256']
    assert shutil.disk_usage(ROOT).free >= (
        LIMITS['disk_headroom_mib']+LIMITS['artifact_mib'])*1048576
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', limits=LIMITS, defaults_before=defaults(),
                  plan_sha256=digest(args.plan), runs=[], comparisons=[])
    child_env = dict(os.environ)

    def run(name, command):
        item = dict(name=name, command=command)
        result['runs'].append(item)
        step = time.monotonic()
        try:
            with (args.output/(name+'.log')).open('wb') as log:
                child = subprocess.run(command, cwd=ROOT, env=child_env,
                    pass_fds=(lease,), stdout=log, stderr=subprocess.STDOUT,
                    timeout=min(LIMITS[name+'_seconds'], LIMITS['seconds']-(step-started)))
            item['exit_code'] = child.returncode
            assert child.returncode == 0, name+' failed'
        finally:
            item['seconds'] = time.monotonic()-step

    try:
        native = args.output/'native'
        run('native', [plan['binary'], plan['assets'], str(args.plan.absolute()),
                       str(native.absolute())])
        receipt = json.loads((native/'result.json').read_text())
        result['native'] = receipt
        assert receipt['status'] == 'PASS_SYNTHESIS_PENDING_CONTENT'
        assert receipt['allocated'] == receipt['released'] == 10
        assert receipt['gpu_talker_loads'] == receipt['gpu_codec_loads'] == 1
        assert receipt['asset_workers_reaped'] == receipt['cases'] == 2
        assert receipt['cancellation_and_frame_limit'] is True
        assert 0 < receipt['advances'] <= LIMITS['total_advances']
        for index in range(2):
            path = native/f'case-{index}'
            value = json.loads((path/'result.json').read_text())
            assert value['early_output'] is True and value['eos'] is True
            assert value['at_limit'] is False
            assert len(value['chunks']) >= 2 and sum(value['chunks']) == value['frames']
            assert all(c == 16 for c in value['chunks'][:-1])
            assert 0 < value['chunks'][-1] <= 16
            assert digest(path/'codes.i32') == plan['original_codes_sha256'][index], \
                'incremental composition changed trained greedy codes'
        prepared = asr_fixture(native, Path(plan['asr_assets']), args.output/'asr-input')
        run('asr', [plan['asr_binary'], plan['asr_assets'], str(prepared.absolute()),
                    str((args.output/'asr').absolute())])
        for index, case in enumerate(CASES):
            path = args.output/'asr'/f'case-{index}'
            decoded = json.loads((path/'result.json').read_text())
            assert decoded['eos'] is True and 0 < decoded['generated_tokens'] <= 128
            result['comparisons'].append(dict(case=index, **content_check(
                (path/'text.txt').read_text(), case)))
        assert all(c['passed'] for c in result['comparisons']), 'speech content differs'
        result['status'] = 'PASS_SYNTHESIS_PENDING_LEASE'
    except BaseException as error:
        result['status'] = 'FAIL'
        result['error'] = repr(error)
    finally:
        result['defaults_after'] = defaults()
        result['defaults_unchanged'] = result['defaults_after'] == plan['defaults']
        result['artifact_bytes'] = sum(p.stat().st_size for p in args.output.rglob('*')
                                       if p.is_file())
        result['seconds'] = time.monotonic()-started
        if not result['defaults_unchanged'] or result['artifact_bytes'] > LIMITS['artifact_mib']*1048576:
            result['status'], result['error'] = 'FAIL', 'default/artifact guard failed'
        if shutil.disk_usage(ROOT).free < LIMITS['disk_headroom_mib']*1048576:
            result['status'], result['error'] = 'FAIL', 'disk headroom guard failed'
        (args.output/'result.json').write_text(json.dumps(result, indent=2,
                                                        ensure_ascii=False)+'\n')
    print(json.dumps({k: v for k, v in result.items()
                      if k in ('status', 'error', 'seconds', 'artifact_bytes')}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    raise SystemExit(main())
