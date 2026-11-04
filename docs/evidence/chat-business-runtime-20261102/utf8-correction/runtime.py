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
OUT = ROOT / '.q4t-work/chat-business-runtime-utf8-20261102'
PRIOR = ROOT / '.q4t-work/chat-business-runtime-20261102'
PLAN_PATH = ROOT / '.q4t-work/chat-business-plan-utf8-20261102.json'
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
    old = json.loads((CPU / 'frozen-plan.json').read_text())
    acceptance.require(plan['artifacts'] == old['artifacts'] and
        plan['model_inputs'] == old['model_inputs'] and
        plan['sources'].keys() == old['sources'].keys(), 'reused identity differs')
    changed = {p for p in plan['sources'] if plan['sources'][p] != old['sources'][p]}
    acceptance.require(changed == {'tools/response_protocol/verify.py',
        'tools/response_protocol/test_acceptance.py'}, 'unexpected source change')
    prior = json.loads((OUT / 'prior-evidence.json').read_text())
    for name, expected in prior.items():
        acceptance.require(acceptance.digest(PRIOR / name) == expected,
                           'prior evidence changed: ' + name)
    fault = json.loads((PRIOR / 'faults/result.json').read_text())
    guard = json.loads((PRIOR / 'lease/exit.json').read_text())
    acceptance.require(fault['status'] == 'PASS' and len(fault['completed']) == 8
        and fault['observed_fixture_processes'] == 12 and guard.get('finished')
        and guard['status'] == 'COMMAND_FAILED' and not guard.get('cleanup_error'),
        'fault reuse or first cleanup unconfirmed')
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
    all_commands = acceptance.commands(plan, OUT)
    commands = [all_commands[3], all_commands[6]]
    acceptance.save(OUT / 'runtime-plan.json', dict(original_plan=str(PLAN_PATH),
        original_plan_sha256=acceptance.digest(PLAN_PATH), script_sha256=acceptance.digest(__file__),
        steps=commands, limits=plan['limits'], started=datetime.datetime.now().isoformat()))
    first_event = json.loads((PRIOR / 'lease/events.jsonl').read_text().splitlines()[0])
    deadline = first_event['monotonic'] + plan['limits']['stage_seconds']
    acceptance.save(OUT / 'correction-budget.json', dict(
        reason='One pre-send UTF-8 driver failure; no extra quality samples',
        prior_attempts=1, prior_wire_generations=0, prior_starts=1,
        cumulative_attempts_max=24, cumulative_starts_max=4,
        cumulative_capability_gets_max=7, deadline_monotonic=deadline,
        reused_cpu_groups=['host', 'client', 'recovery', 'template'],
        rerun_group='orchestration plus one new loopback UTF-8 regression',
        reused_real_fault_cases=8))
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
