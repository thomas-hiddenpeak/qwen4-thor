"""Wait for the existing off run, then follow the frozen one-shot gates."""
import json
import subprocess
import time

from phase_common import R, W, environment, frozen, read, save, sha

PLAN_SHA = 'dd12d7cf8beaad1d5bf2db1d581ef95282b8da53b5c45fdd62bcc7580b0f3d9d'
plan = frozen(PLAN_SHA)
started = time.time()
assert (R / 'inheritance-off-controller-start.json').is_file()
assert not (R / 'inheritance-on-controller-start.json').exists()
save(R / 'inheritance-continuation-start.json', {
    'started_t': started, 'controller_sha256': sha(__file__),
    'plan_sha256': PLAN_SHA, 'automatic_retry': False,
    'scope': 'wait existing off -> audit -> one on -> audit -> frozen compare',
    'full_matrix_autostart': False})
commands = []
report = {'started_t': started, 'failure': None, 'decision': 'INCOMPLETE',
          'automatic_retry': False, 'full_matrix_autostart': False}
try:
    completed = R / 'inheritance-off-stage.json'
    deadline = read(R / 'inheritance-off-controller-start.json')['started_t'] + 11100
    while True:
        assert time.time() < deadline, 'existing off stage did not produce terminal evidence'
        try:
            off = read(completed)
            break
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(5)
    assert off['returncode'] == 0 and off['failure'] is None, 'off failed; no on run'
    sequence = [('audit_group.py', 'inheritance-off'),
                ('run_stage.py', 'inheritance-on'),
                ('audit_group.py', 'inheritance-on'),
                ('compare_groups.py', 'inheritance')]
    for script, stage in sequence:
        command = ['python3', '-B', str(R / script), stage,
                   '--plan-sha256', PLAN_SHA]
        log = R / (stage + '-' + script.removesuffix('.py') + '-continuation.log')
        begin = time.time()
        with log.open('x') as handle:
            code = subprocess.run(command, cwd=W, env=environment(), stdout=handle,
                                  stderr=subprocess.STDOUT).returncode
        commands.append({'command': command, 'started_t': begin,
                         'ended_t': time.time(), 'returncode': code,
                         'log_path': str(log), 'log_sha256': sha(log)})
        if script == 'compare_groups.py':
            decision = read(R / 'inheritance-decision.json')
            assert code == (0 if decision['passed'] else 3)
            report['decision'] = decision['decision']
            report['decision_sha256'] = sha(R / 'inheritance-decision.json')
        else:
            assert code == 0, script + ' ' + stage + ' failed; stop'
except BaseException as error:
    report['failure'] = type(error).__name__ + ': ' + str(error)
    report['decision'] = 'STOP_GROUP_OR_EVIDENCE_FAILURE'
    raise
finally:
    report.update(ended_t=time.time(), commands=commands,
        runtime_binary_sha256=plan['runtime_binary_sha256'],
        runtime_source_commit=plan['runtime_source_commit'], plan_sha256=PLAN_SHA)
    save(R / 'inheritance-continuation-exit.json', report)
    print(report, flush=True)
