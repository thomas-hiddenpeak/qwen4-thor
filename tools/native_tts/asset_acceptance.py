#!/usr/bin/env python3
"""One complete production asset-loader read; no CUDA or synthesis calls."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

from acceptance import ROOT, defaults, digest

LIMITS = dict(seconds=120, asset_deadline_seconds=60, cases=1,
              full_checkpoint_reads=2, gpu_model_loads=0, http_requests=0,
              artifact_mib=2, disk_headroom_mib=1024)
CASE = 'tts_assets_full_checkpoints_bind_trained_layouts'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.environ.get('PYTHONOPTIMIZE')
    plan = json.loads(args.plan.read_text())
    assert plan['schema'] == 'q4t-native-tts-assets-v1'
    assert plan['limits'] == LIMITS and plan['case'] == CASE
    assert digest(Path(plan['binary'])) == plan['binary_sha256']
    assert defaults() == plan['defaults']
    assert all(digest(ROOT/p) == h for p, h in plan['sources'].items())
    assert shutil.disk_usage(ROOT).free >= (
        LIMITS['disk_headroom_mib'] + LIMITS['artifact_mib'])*1048576
    args.output.mkdir(parents=True, exist_ok=False)
    required = args.output/'required.txt'
    required.write_text(CASE+'\n')
    result = dict(status='RUNNING', limits=LIMITS, defaults_before=defaults(),
                  plan_sha256=digest(args.plan))
    started = time.monotonic()
    try:
        env = dict(os.environ, CUDA_VISIBLE_DEVICES='',
                   Q4T_TTS_ASSETS=plan['assets'],
                   Q4T_TEST_OUTPUT_DIR=str(args.output/'fixtures'))
        with (args.output/'tests.log').open('w') as log:
            child = subprocess.run(
                [plan['binary'], '--required-list', str(required)],
                env=env, cwd=ROOT, pass_fds=(lease,), stdout=log,
                stderr=subprocess.STDOUT, timeout=LIMITS['seconds'])
        result['exit_code'] = child.returncode
        text = (args.output/'tests.log').read_text()
        assert child.returncode == 0 and '[PASS] '+CASE in text
        assert '1 tests, 1 passed, 0 failed, 0 skipped' in text
        assert defaults() == plan['defaults']
        assert shutil.disk_usage(ROOT).free >= LIMITS['disk_headroom_mib']*1048576
        result['status'] = 'PASS_ASSET_LOADER_PENDING_LEASE'
    except BaseException as exc:
        result['status'] = 'FAIL'
        result['error'] = repr(exc)
        raise
    finally:
        result['seconds'] = time.monotonic()-started
        result['defaults_after'] = defaults()
        (args.output/'result.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps({k: v for k, v in result.items()
                          if not k.startswith('defaults_')}))


if __name__ == '__main__':
    main()
