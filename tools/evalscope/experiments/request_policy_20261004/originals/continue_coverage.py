"""One-shot original matrix coverage after the existing history controller.

This adapter never reruns history, changes a gate, or starts lifecycle/business
or another candidate. Run only with the independently reviewed self SHA256.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time

R = Path('/home/rm01/models/dev/qwen4-thor/.q4t-work/'
         'offload-request-policy-20261004')
W = R / 'source'
PLAN_SHA = 'dd12d7cf8beaad1d5bf2db1d581ef95282b8da53b5c45fdd62bcc7580b0f3d9d'
AMENDMENT_SHA = '12c1579eba32cc91219bd2d3453cf65fca40bf7fc88022e12d1acab705e6a23e'
ENTRIES = {
    'phase_common.py':
        'adbd4b9aac6e471e4d80dfc987c96698f4202fe3c0c3ac08faaabe78411d5505',
    'continue_inheritance.py':
        '1e3cd916cc7a068d1f017212a287a811a00ff351a410f0b5b839dae4c0b02b84',
    'run_coverage_stage.py':
        'fef00e0fa4689b857ac46af7e3421cc34ed9be41f2f10de6f28f17c6c8f6d074',
    'audit_group.py':
        'b7d1a7a512a9a63a8e6c14feff8da21ef7cfb1ab94456392ce5a4cf5485b5b1d',
    'compare_groups.py':
        'c9e8a72a262c41994dd16610db4f893fbb89963e970f97faf6ff3bd4b873bd45',
    'aggregate_performance.py':
        '9fd70bcbb4ec355a6810a841cf68cce38676208c4c5a7c8d240a05c85a3fa38d',
}
HISTORY_ARM_SECONDS = 11100
HISTORY_FINALIZATION_SECONDS = 1200
OFFLINE_STEP_SECONDS = 1800
MATRIX_OUTER_CLEANUP_SECONDS = 1800
CHILD_INTERRUPT_GRACE_SECONDS = 300
CHILD_TERMINATE_GRACE_SECONDS = 120


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    # Exclusive files are also the one-shot claim. Never resume/overwrite them.
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def finite_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def environment():
    return {**{key: value for key, value in os.environ.items()
               if not key.startswith('Q4T_') and key != 'LD_PRELOAD'},
            'PYTHONDONTWRITEBYTECODE': '1'}


def validate_entries(self_sha):
    require(Path(__file__).resolve() == R / 'continue_coverage.py',
            'unexpected continuation location')
    require(sha(__file__) == self_sha, 'continuation self SHA changed')
    require(sha(R / 'execution-plan.json') == PLAN_SHA, 'plan SHA changed')
    require(sha(R / 'coverage-amendment.json') == AMENDMENT_SHA,
            'coverage amendment SHA changed')
    for name, digest in ENTRIES.items():
        require(sha(R / name) == digest, 'controller SHA changed: ' + name)
    plan = read(R / 'execution-plan.json')
    amendment = read(R / 'coverage-amendment.json')
    require(amendment['status'] == 'FROZEN_ACTIVE' and
            amendment['original_execution_plan_sha256'] == PLAN_SHA and
            amendment['original_phase_plan_sha256'] ==
            plan['phase_plan_sha256'] and
            amendment['samples_thresholds_resources_unchanged'] is True and
            amendment['history_failure_permanently_vetoes_overall_GO'] is True,
            'amendment does not authorize this unchanged coverage')
    for mapping in ('original_files', 'source_sha256'):
        for path, digest in amendment[mapping].items():
            require(sha(path) == digest, 'amendment source changed: ' + path)
    for stage in ('inheritance-off', 'inheritance-on', 'matrix-off', 'matrix-on'):
        require(sha(R / (stage + '-command.json')) ==
                plan['command_sha256'][stage], 'frozen command changed: ' + stage)
    return plan


def bound_decision(path, plan, *, require_pass=False):
    record = read(path)
    require(record['plan_sha256'] == PLAN_SHA and
            record['runtime_binary_sha256'] == plan['runtime_binary_sha256'] and
            record['runtime_source_commit'] == plan['runtime_source_commit'],
            'decision belongs to another plan/runtime: ' + str(path))
    require(type(record['passed']) is bool, 'missing boolean decision')
    if require_pass:
        require(record['passed'] is True, 'group/evidence failure: ' + str(path))
    return record


def wait_history(plan, report, log):
    start_path = R / 'inheritance-off-controller-start.json'
    origin = read(start_path)
    require(finite_time(origin['started_t']) and
            origin['command'] == read(R / 'inheritance-off-command.json'),
            'existing history origin is not the frozen off controller')
    # The previous continuation still owns both history arms. Do not signal it
    # on timeout/interruption: this adapter did not create that process or unit.
    deadline = (origin['started_t'] + 2 * HISTORY_ARM_SECONDS +
                HISTORY_FINALIZATION_SECONDS)
    report['history_wait'] = {
        'origin_path': str(start_path), 'origin_sha256': sha(start_path),
        'origin_started_t': origin['started_t'], 'deadline_unix_seconds': deadline,
        'per_arm_seconds': HISTORY_ARM_SECONDS,
        'finalization_seconds': HISTORY_FINALIZATION_SECONDS,
        'owns_history_processes_or_units': False,
    }
    log.write('Waiting for existing history continuation terminal evidence.\n')
    log.flush()
    path = R / 'inheritance-continuation-exit.json'
    while True:
        try:
            terminal = read(path)
            break
        except (FileNotFoundError, json.JSONDecodeError):
            require(time.time() < deadline,
                    'history deadline expired; no matrix launched')
            time.sleep(5)
    require(terminal['failure'] is None and
            terminal['decision'] in ('PASS_SCREENING', 'NO_GO') and
            terminal['plan_sha256'] == PLAN_SHA and
            terminal['runtime_binary_sha256'] == plan['runtime_binary_sha256'] and
            terminal['runtime_source_commit'] == plan['runtime_source_commit'] and
            terminal['automatic_retry'] is False and
            finite_time(terminal['ended_t']) and
            origin['started_t'] <= terminal['ended_t'] <= time.time(),
            'history continuation failed or has inconsistent identity')
    decision_path = R / 'inheritance-decision.json'
    require(sha(decision_path) == terminal['decision_sha256'],
            'history terminal decision SHA mismatch')
    decision = bound_decision(decision_path, plan)
    require(decision['evidence_contracts_passed'] is True and
            decision['decision'] ==
            ('PASS_SCREENING' if decision['passed'] else 'NO_GO') and
            terminal['decision'] == decision['decision'],
            'history failure is not a valid speed-only NO_GO')
    expected = [('audit_group.py', 'inheritance-off'),
                ('run_stage.py', 'inheritance-on'),
                ('audit_group.py', 'inheritance-on'),
                ('compare_groups.py', 'inheritance')]
    require(len(terminal['commands']) == len(expected),
            'history terminal does not contain the four frozen steps')
    for record, (script, stage) in zip(terminal['commands'], expected):
        command = ['python3', '-B', str(R / script), stage,
                   '--plan-sha256', PLAN_SHA]
        expected_rc = (0 if decision['passed'] else 3) if script == \
            'compare_groups.py' else 0
        require(record['command'] == command and
                record['returncode'] == expected_rc and
                sha(record['log_path']) == record['log_sha256'],
                'history command failed or changed: ' + stage)
    for stage in ('inheritance-off', 'inheritance-on'):
        bound_decision(R / (stage + '-decision.json'), plan, require_pass=True)
    report.update(history_decision=decision['decision'],
                  history_decision_sha256=sha(decision_path),
                  history_failure_present=not decision['passed'],
                  history_continuation_sha256=sha(path))
    log.write('History contracts completed; preserving its speed decision.\n')
    log.flush()


def cleanup_direct_controller(proc, record):
    """Let the child controller clean its separately owned groups and unit.

    Never killpg here: nested phase_common.run creates a separate group for
    the wrapper. SIGKILL of the outer controller could orphan its cleanup.
    """
    if proc is None:
        record['child_controller_reaped'] = True
        return
    for sig, grace in ((signal.SIGINT, CHILD_INTERRUPT_GRACE_SECONDS),
                       (signal.SIGTERM, CHILD_TERMINATE_GRACE_SECONDS)):
        if proc.poll() is not None:
            break
        try:
            proc.send_signal(sig)  # The unreaped child PID is still ours.
            record['signals'].append({'signal': sig.name, 'pid': proc.pid,
                                      'unix_seconds': time.time()})
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    record['child_controller_reaped'] = proc.poll() is not None
    record['returncode'] = proc.returncode
    if not record['child_controller_reaped']:
        record['cleanup_unknown'] = True
        record['cleanup_note'] = (
            'Child controller did not terminate during cleanup grace; no '
            'SIGKILL, process-group broadcast or unowned unit signal was sent. '
            'Stop all later stages and require inspection of this owned PID.')


def run_step(index, script, stage, command, timeout, report, self_sha, log):
    validate_entries(self_sha)
    label = f'coverage-step-{index:02d}-{stage or "aggregate"}'
    if script == 'audit_group.py':
        label += '-audit'
    elif script == 'run_coverage_stage.py':
        label += '-run'
    elif script == 'compare_groups.py':
        label += '-compare'
    paths = {kind: R / (label + suffix) for kind, suffix in
             [('start', '-start.json'), ('exit', '-exit.json'), ('log', '.log')]}
    require(all(not path.exists() for path in paths.values()),
            'step already claimed; automatic retry forbidden: ' + label)
    record = {'started_t': time.time(), 'command': command, 'script': script,
              'entry_sha256': ENTRIES[script], 'stage': stage,
              'timeout_s': timeout, 'returncode': None, 'failure': None,
              'pid': None, 'signals': [], 'child_controller_reaped': False,
              'cleanup_unknown': False, 'automatic_retry': False,
              'log_path': str(paths['log'])}
    save(paths['start'], record)
    log.write('Starting ' + label + '.\n')
    log.flush()
    proc = None
    try:
        with paths['log'].open('x') as child_log:
            proc = subprocess.Popen(command, cwd=W, env=environment(),
                                    stdout=child_log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            record['pid'] = proc.pid
            record['returncode'] = proc.wait(timeout=timeout)
            record['child_controller_reaped'] = True
    except BaseException as error:
        record['failure'] = type(error).__name__ + ': ' + str(error)
        if proc is not None:
            record['cleanup_unknown'] = True
            record['child_cleanup_confirmation'] = (
                'PENDING_REVIEW_OF_CHILD_STAGE_AND_OWNED_UNIT_EVIDENCE')
        raise
    finally:
        old_int, old_term = signal.getsignal(signal.SIGINT), \
            signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if proc is not None and proc.poll() is None:
                try:
                    cleanup_direct_controller(proc, record)
                except BaseException as cleanup_error:
                    record['cleanup_unknown'] = True
                    record['cleanup_failure'] = (
                        type(cleanup_error).__name__ + ': ' + str(cleanup_error))
            if script == 'run_coverage_stage.py' and record['returncode'] != 0:
                record['cleanup_unknown'] = True
                record['child_cleanup_confirmation'] = (
                    'PENDING_REVIEW_OF_CHILD_STAGE_AND_OWNED_UNIT_EVIDENCE')
            if paths['log'].exists():
                record['log_sha256'] = sha(paths['log'])
            record['ended_t'] = time.time()
            save(paths['exit'], record)
            report['commands'].append(record)
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
    require(record['child_controller_reaped'] and not record['cleanup_unknown'],
            'child controller cleanup unknown; stop')
    log.write('Completed ' + label + ', rc=' + str(record['returncode']) + '.\n')
    log.flush()
    return record['returncode']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-sha256', required=True)
    args = parser.parse_args()
    require(len(args.self_sha256) == 64 and
            all(c in '0123456789abcdef' for c in args.self_sha256),
            'explicit reviewed self SHA256 required')
    require(sha(__file__) == args.self_sha256, 'unreviewed continuation source')
    start_path = R / 'coverage-continuation-start.json'
    exit_path = R / 'coverage-continuation-exit.json'
    log_path = R / 'coverage-continuation.log'
    require(not exit_path.exists() and not log_path.exists(),
            'existing continuation artifacts prohibit retry')
    report = {'started_t': time.time(), 'ended_t': None,
              'controller_sha256': args.self_sha256, 'plan_sha256': PLAN_SHA,
              'amendment_sha256': AMENDMENT_SHA, 'entry_sha256': ENTRIES,
              'failure': None, 'decision': 'INCOMPLETE', 'completed': False,
              'automatic_retry': False, 'commands': [],
              'history_failure_permanently_vetoes_overall_GO': True,
              'next_phase_started': False,
              'authorizes_business_lifecycle_or_next_candidate': False,
              'scope': 'Original six-tier matrix coverage and aggregate only'}
    save(start_path, report)

    def interrupted(number, frame):
        raise KeyboardInterrupt('continuation received signal ' + str(number))

    previous_int, previous_term = signal.getsignal(signal.SIGINT), \
        signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        with log_path.open('x') as log:
            plan = validate_entries(args.self_sha256)
            report.update(runtime_binary_sha256=plan['runtime_binary_sha256'],
                          runtime_source_commit=plan['runtime_source_commit'])
            # Claiming this adapter must not adopt or rerun an existing matrix.
            for stage in ('matrix-off', 'matrix-on'):
                for suffix in ('-controller-start.json', '-controller-exit.json',
                               '-stage.json', '-decision.json',
                               '-coverage-admission.json'):
                    require(not (R / (stage + suffix)).exists(),
                            'matrix already has an execution artifact: ' + stage)
                require(not (W / '.q4t-work/evidence' / stage).exists(),
                        'matrix evidence directory already exists: ' + stage)
            require(not (R / 'matrix-decision.json').exists() and
                    not (R / 'performance-decision.json').exists(),
                    'matrix or aggregate already decided')
            wait_history(plan, report, log)
            sequence = [('run_coverage_stage.py', 'matrix-off'),
                        ('audit_group.py', 'matrix-off'),
                        ('run_coverage_stage.py', 'matrix-on'),
                        ('audit_group.py', 'matrix-on'),
                        ('compare_groups.py', 'matrix'),
                        ('aggregate_performance.py', None)]
            for index, (script, stage) in enumerate(sequence, 1):
                command = ['python3', '-B', str(R / script)]
                if stage:
                    command.append(stage)
                command += ['--plan-sha256', PLAN_SHA]
                timeout = OFFLINE_STEP_SECONDS
                if script in ('run_coverage_stage.py', 'aggregate_performance.py'):
                    command += ['--amendment-sha256', AMENDMENT_SHA]
                if script == 'run_coverage_stage.py':
                    frozen_command = read(R / (stage + '-command.json'))
                    budget = int(frozen_command[
                        frozen_command.index('--runner-timeout-s') + 1])
                    require(budget == 21600, 'matrix runner budget changed')
                    timeout = budget + MATRIX_OUTER_CLEANUP_SECONDS
                code = run_step(index, script, stage, command, timeout,
                                report, args.self_sha256, log)
                if script == 'run_coverage_stage.py':
                    require(code == 0, stage + ' execution failed; stop')
                    terminal = read(R / (stage + '-stage.json'))
                    require(terminal['returncode'] == 0 and
                            terminal['failure'] is None and
                            terminal['cleanup_complete'] is True and
                            terminal['amendment_sha256'] == AMENDMENT_SHA,
                            stage + ' lacks clean terminal evidence')
                elif script == 'audit_group.py':
                    require(code == 0, stage + ' evidence audit failed; stop')
                    bound_decision(R / (stage + '-decision.json'), plan,
                                   require_pass=True)
                elif script == 'compare_groups.py':
                    decision = bound_decision(R / 'matrix-decision.json', plan)
                    require(decision['evidence_contracts_passed'] is True and
                            decision['decision'] ==
                            ('PASS_SCREENING' if decision['passed'] else 'NO_GO') and
                            code == (0 if decision['passed'] else 3),
                            'matrix comparison failed beyond valid speed NO_GO')
                    report.update(matrix_decision=decision['decision'],
                                  matrix_decision_sha256=sha(
                                      R / 'matrix-decision.json'))
                else:
                    decision = bound_decision(R / 'performance-decision.json', plan)
                    require(decision['evidence_contracts_passed'] is True and
                            decision['amendment_sha256'] == AMENDMENT_SHA and
                            decision['next_optimization_allowed'] is False and
                            decision['history_failure_permanently_vetoes_overall_GO']
                            is True and decision['decision'] ==
                            ('PASS_ALL_SPEED_GATES' if decision['passed'] else
                             'NO_GO_PERFORMANCE') and
                            code == (0 if decision['passed'] else 3),
                            'aggregate failed beyond valid performance NO_GO')
                    report.update(decision=decision['decision'],
                                  overall_speed_passed=decision['passed'],
                                  aggregate_returncode=code,
                                  final_decision_sha256=sha(
                                      R / 'performance-decision.json'))
            validate_entries(args.self_sha256)
            report['completed'] = True
            log.write('Original coverage completed; no further phase started.\n')
            log.flush()
    except BaseException as error:
        report['failure'] = type(error).__name__ + ': ' + str(error)
        report['decision'] = 'STOP_GROUP_EVIDENCE_OR_CONTROLLER_FAILURE'
        raise
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        report['ended_t'] = time.time()
        if log_path.exists():
            report['log_sha256'] = sha(log_path)
        report['cleanup_unknown'] = any(
            step.get('cleanup_unknown', False) for step in report['commands'])
        save(exit_path, report)
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        print(json.dumps({key: report.get(key) for key in
                          ('decision', 'completed', 'failure', 'cleanup_unknown',
                           'aggregate_returncode')}, ensure_ascii=False), flush=True)
    # A valid NO_GO is a completed experiment, not a controller failure.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
