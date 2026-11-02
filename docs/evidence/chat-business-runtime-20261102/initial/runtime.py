"""Run only remaining frozen steps; reuse sealed, identity-matched CPU checks."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT = Path('/home/rm01/models/dev/qwen4-thor')
OUT = ROOT / '.q4t-work/chat-business-runtime-20261102'
PLAN_PATH = ROOT / '.q4t-work/chat-business-plan-20261102.json'
CPU = ROOT / 'docs/evidence/chat-business-acceptance-20261102'
sys.path[:0] = [str(ROOT / 'tools/response_protocol'),
                str(ROOT / 'tools/verify/startup_delivery')]
import acceptance
from deploy import require_guard

def reuse(plan):
    acceptance.validate_plan(plan)
    seals = json.loads((CPU / 'sealed-files.json').read_text())
    for name, expected in seals.items():
        acceptance.require(acceptance.digest(CPU / name) == expected,
                           'CPU evidence changed: ' + name)
    acceptance.require(acceptance.digest(PLAN_PATH) ==
                       acceptance.digest(CPU / 'frozen-plan.json'), 'CPU input identity differs')
    result = json.loads((CPU / 'cpu-01/result.json').read_text())
    acceptance.require(result['status'] == 'PASS' and
        [r['name'] for r in result['steps']] ==
            ['host', 'client', 'recovery', 'orchestration', 'template'] and
        all(r['returncode'] == 0 for r in result['steps']), 'CPU reuse not established')
    return {name: acceptance.digest(CPU / name) for name in
            ['sealed-files.json', 'frozen-plan.json', 'cpu-01/result.json']}

def main():
    acceptance.install_interrupt_handlers()
    require_guard(OUT)
    plan = json.loads(PLAN_PATH.read_text())
    receipt = reuse(plan)
    acceptance.save(OUT / 'reused-cpu.json', dict(directory=str(CPU), sha256=receipt))
    commands = acceptance.commands(plan, OUT)[5:]
    acceptance.save(OUT / 'runtime-plan.json', dict(original_plan=str(PLAN_PATH),
        original_plan_sha256=acceptance.digest(PLAN_PATH), script_sha256=acceptance.digest(__file__),
        steps=commands, limits=plan['limits'], started=datetime.datetime.now().isoformat()))
    deadline = time.monotonic() + plan['limits']['stage_seconds']
    state = dict(status='FAIL', steps=[], reused_cpu=True, deployed=False,
                 performance_acceptance=False)
    try:
        for name, limit, command in commands:
            reuse(plan)
            require_guard(OUT)
            remaining = deadline - time.monotonic()
            acceptance.require(remaining > 0, 'stage deadline')
            env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_')
                   and k not in ('LD_PRELOAD', 'PYTHONPATH', 'PYTHONHOME')}
            temp = OUT / (name + '-temporary')
            temp.mkdir()
            env.update(Q4T_TEST_OUTPUT_DIR=str(temp), TMPDIR=str(temp),
                       PYTHONDONTWRITEBYTECODE='1')
            record = dict(name=name, argv=command, timeout=min(limit, remaining))
            acceptance.save(OUT / (name + '-command.json'), record)
            print('START ' + name, flush=True)
            started = time.monotonic()
            try:
                if name == 'faults':
                    state['status'] = 'ENV_ABORT'
                with (OUT / (name + '.log')).open('x') as log:
                    run = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                        stderr=subprocess.STDOUT, timeout=record['timeout'])
                record['returncode'] = run.returncode
                if name == 'faults':
                    fault = json.loads((OUT / 'faults/result.json').read_text())
                    if fault.get('status') in ('PASS', 'FAIL'):
                        state['status'] = 'FAIL'
                    acceptance.require(fault.get('status') == 'PASS', 'fault scenarios failed')
                acceptance.require(run.returncode == 0, 'step failed: ' + name)
            finally:
                record['elapsed_seconds'] = time.monotonic() - started
                state['steps'].append(record)
                acceptance.save(OUT / (name + '-exit.json'), record)
            print('PASS ' + name, flush=True)
        reuse(plan)
        state['status'] = 'CHECKS_PASSED_PENDING_GUARD_EXIT'
    except BaseException as error:
        state['error'] = repr(error)
        raise
    finally:
        state['finished'] = datetime.datetime.now().isoformat()
        acceptance.save(OUT / 'worker-result.json', state)

if __name__ == '__main__':
    main()
