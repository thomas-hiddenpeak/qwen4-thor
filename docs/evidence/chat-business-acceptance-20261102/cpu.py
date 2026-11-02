"""Only the frozen, GPU-free CPU checks after ENV_ABORT/LEASE_BUSY."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT = Path('/home/rm01/models/dev/qwen4-thor')
sys.path.insert(0, str(ROOT / 'tools/response_protocol'))
import acceptance
OUT = ROOT / '.q4t-work/chat-business-acceptance-20261102/cpu-01'
plan_path = ROOT / '.q4t-work/chat-business-plan-20261102.json'
plan = json.loads(plan_path.read_text())
acceptance.validate_plan(plan)
OUT.mkdir(mode=0o700)
commands = acceptance.commands(plan, OUT)[:5]
acceptance.save(OUT / 'plan.json', dict(plan_sha256=acceptance.digest(plan_path),
    plan_path=str(plan_path), steps=commands, deadline_seconds=560,
    runtime_validation=False, reason='GPU lease busy; independent CPU checks only'))
result = dict(status='FAIL', steps=[], started=datetime.datetime.now().isoformat(),
              runtime_validation=False, performance_acceptance=False, deployed=False)
deadline = time.monotonic() + 560
try:
    for name, limit, command in commands:
        acceptance.validate_plan(plan)
        temp = OUT / (name + '-temporary')
        temp.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_')
               and k not in ('LD_PRELOAD', 'PYTHONPATH', 'PYTHONHOME')}
        env.update(Q4T_TEST_OUTPUT_DIR=str(temp), TMPDIR=str(temp),
                   PYTHONDONTWRITEBYTECODE='1', CUDA_VISIBLE_DEVICES='')
        record = dict(name=name, argv=command, timeout=min(limit, deadline-time.monotonic()))
        acceptance.save(OUT / (name + '-command.json'), record)
        start = time.monotonic()
        with (OUT / (name + '.log')).open('x') as log:
            run = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, timeout=record['timeout'])
        record.update(returncode=run.returncode, seconds=time.monotonic()-start)
        acceptance.save(OUT / (name + '-exit.json'), record)
        result['steps'].append(record)
        print(json.dumps(record), flush=True)
        acceptance.require(run.returncode == 0, 'failed step: ' + name)
    acceptance.validate_plan(plan)
    result['status'] = 'PASS'
except BaseException as error:
    result['error'] = repr(error)
    raise
finally:
    result['finished'] = datetime.datetime.now().isoformat()
    acceptance.save(OUT / 'result.json', result)
