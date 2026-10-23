"""One bounded execution-observation stage under a shared whole-stage lease."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/mtp_suffix_zero_hit'))
import telemetry

SPEC = importlib.util.spec_from_file_location(
    'suffix_admission_reused', ROOT / 'tools/mtp_suffix_admission/run_stage.py')
admission = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admission)
legacy = admission.legacy
http = admission.http_runner
save = legacy.save


sys.path.insert(0, str(Path(__file__).resolve().parent))
from spec import validate


def server_command(plan, step):
    command = [plan['candidate'], 'serve', '--model-dir', plan['model'],
               '--port', str(plan['port']), '--max-seq', '1', '--max-prefill',
               '8192', '--max-len', '208896', '--max-tokens', '256',
               '--mtp', '--mtp-k', '3']
    if step['head'] == 'subset':
        command += ['--mtp-subset-head', plan['artifact']]
    if step['suffix']:
        command += ['--mtp-suffix']
    return command


def proc_identity(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return dict(pid=pid, start_ticks=int(fields[19]), ppid=int(fields[1]),
                pgid=int(fields[2]), state=fields[0])


def save_atomic(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    save(temporary, value)
    temporary.replace(path)


def target_wrapper(plan, name):
    """Keep the actual profiled q4t's return code separate from Nsight's code."""
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    if step['measurement_role'] != 'nsys_profile':
        raise ValueError('target wrapper only belongs to frozen Nsight steps')
    output = Path(plan['output']) / name
    child = None

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('owned target wrapper interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    try:
        # Preserve Nsight's injection environment across exec. All user Q4T
        # settings were cleared before launching Nsight by custom_step.
        child = subprocess.Popen(server_command(plan, step), cwd=ROOT)
        identity = proc_identity(child.pid)
        save_atomic(output / 'target-identity.json', dict(identity,
            wrapper_pid=os.getpid(), candidate=plan['candidate'],
            command=server_command(plan, step)))
        (output / 'server.pid').write_text(str(child.pid) + '\n')
        child.wait()
    finally:
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
            save_atomic(output / 'target-exit.json', dict(
                pid=child.pid, returncode=child.returncode,
                wrapper_pid=os.getpid(), finished_monotonic=time.monotonic()))
    return 0 if child is not None and child.returncode == 0 else 1


def stop_profile_target(plan, output, launcher):
    """Signal only the pinned actual q4t; let Nsight finish its trace naturally."""
    identity_path = output / 'target-identity.json'
    if not identity_path.is_file():
        if launcher.poll() is None:
            raise RuntimeError('profile target identity missing; outer owner cleanup required')
        return None
    identity = json.loads(identity_path.read_text())
    pid, start = identity['pid'], identity['start_ticks']
    forced = False
    fd = None
    try:
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            pass
        if fd is not None:
            try:
                actual = proc_identity(pid)
            except (FileNotFoundError, ProcessLookupError):
                actual = None
            if actual is None:
                os.close(fd)
                fd = None
            elif actual['start_ticks'] != start:
                raise RuntimeError('profile target PID identity changed; refusing signal')
            if actual is not None and actual['state'] not in ('Z', 'X'):
                executable = Path(f'/proc/{pid}/exe').resolve()
                if executable != Path(plan['candidate']).resolve():
                    raise RuntimeError('profile target executable changed; refusing signal')
                try:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if (output / 'target-exit.json').is_file():
                        break
                    time.sleep(.05)
                else:
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    forced = True
    finally:
        if fd is not None:
            os.close(fd)
    launcher.wait(timeout=plan['nsys']['launcher_exit_timeout_seconds'])
    target = json.loads((output / 'target-exit.json').read_text())
    if target['pid'] != pid or target['wrapper_pid'] != identity['wrapper_pid']:
        raise RuntimeError('profile target exit identity mismatch')
    save(output / 'profile-shutdown.json', dict(target=target,
        launcher_returncode=launcher.returncode, forced_target_kill=forced,
        scope='Actual q4t pidfd signal only; launcher allowed to finish trace.'))
    return target


def audit_execution(output, step, server_pid):
    """Audit raw host records even on failure, without treating partial as valid."""
    errors, batches, spans, flushes, captures = [], {}, {}, {}, []
    raw = (output / 'server.log').read_text()
    try:
        exec_lines = [line for line in raw.splitlines() if line.startswith('[exec-')]
        if not step['execution_diagnostic']:
            if exec_lines:
                raise ValueError('normal service emitted execution diagnostics')
        else:
            if not exec_lines:
                raise ValueError('observed service emitted no execution diagnostics')
            for number, line in enumerate(raw.splitlines(), 1):
                if not line.startswith('[exec-'):
                    continue
                marker = line.split(']', 1)[0] + ']'
                row = dict(re.findall(r'(\w+)=(\S+)', line), line=number)
                if marker == '[exec-capture]':
                    captures.append(row)
                    continue
                key = int(row['batch'])
                if marker == '[exec-batch]':
                    if key in batches:
                        raise ValueError('duplicate execution batch')
                    batches[key] = row
                elif marker == '[exec-span]':
                    spans.setdefault(key, []).append(row)
                elif marker == '[exec-flush]':
                    if key in flushes:
                        raise ValueError('duplicate execution flush')
                    flushes[key] = row
                else:
                    raise ValueError('unrecognized execution record: ' + marker)
            if not batches or batches.keys() != spans.keys() or batches.keys() != flushes.keys():
                raise ValueError('execution batch/span/flush coverage is incomplete')
            for key, batch in batches.items():
                rows, flush = spans[key], flushes[key]
                if (int(batch['pid']) != server_pid or batch['complete'] != '1' or
                        batch['overflow'] != '0' or batch['clock_error'] != '0' or
                        int(batch['capacity']) != 4096 or
                        not 0 < int(batch['records']) == len(rows) <= 4096 or
                        [int(row['id']) for row in rows] != list(range(len(rows))) or
                        flush['clock_error'] != '0' or int(flush['wall_ns']) < 0 or
                        int(flush['cpu_ns']) < 0 or flush['line'] <= rows[-1]['line']):
                    raise ValueError('invalid/incomplete execution batch or flush')
                for row in rows:
                    index, parent = int(row['id']), int(row['parent'])
                    if (row['closed'] != '1' or row['pid'] != batch['pid'] or
                            row['tid'] != batch['tid'] or not -1 <= parent < index or
                            row['line'] <= batch['line']):
                        raise ValueError('execution span identity/tree is incomplete')
                    for clock in ('wall', 'cpu'):
                        begin, end = int(row[clock + '_begin_ns']), int(row[clock + '_end_ns'])
                        if begin < 0 or end < begin:
                            raise ValueError('execution clock is invalid/non-monotonic')
                        if parent >= 0 and not (
                                int(rows[parent][clock + '_begin_ns']) <= begin <= end <=
                                int(rows[parent][clock + '_end_ns'])):
                            raise ValueError('nested execution span escapes parent interval')
            admitted = [dict(re.findall(r'(\w+)=(\S+)', line))
                        for line in raw.splitlines() if 'event=admitted ' in line]
            expected = [case['input_tokens'] for case in step['cases']
                        for _ in range(case['repeats'])]
            if len(admitted) != len(expected):
                raise ValueError('execution request coverage is incomplete')
            for request, tokens in zip(admitted, expected):
                request_rows = [row for key, batch in batches.items()
                                if batch['label'] == request['id'] for row in spans[key]]
                phases = {row['phase'] for row in request_rows}
                required = {'handler_prefill', 'handler_mtp_init', 'prompt_trunk_free',
                            'target_begin_sequence', 'prompt_draft_extend_call',
                            'prefill_final_d2h_submit', 'prefill_final_readback_wait_commit'}
                if not required <= phases:
                    raise ValueError('missing required request prefill/init/free spans')
                calls = sorted((int(row['p']), int(row['t'])) for row in request_rows
                               if row['phase'] == 'target_prefill_call')
                position = 0
                for start, count in calls:
                    if start != position or not 0 < count <= 8192:
                        raise ValueError('prefill chunk coverage is incomplete')
                    position += count
                if position != tokens:
                    raise ValueError('prefill chunks do not cover the actual prompt')
        if step['measurement_role'] == 'nsys_profile':
            if ([row['event'] for row in captures] != ['begin', 'end'] or
                    any(int(row['pid']) != server_pid or row['error'] != '0' or
                        int(row['input_tokens']) != step['profile_input_tokens']
                        for row in captures)):
                raise ValueError('capture markers are incomplete or refer to another target')
        elif captures:
            raise ValueError('capture unexpectedly enabled outside the frozen profile branch')
    except (KeyError, ValueError, TypeError) as error:
        errors.append(repr(error))
    result = dict(complete=not errors, errors=errors, actual_server_pid=server_pid,
        execution_diagnostic=step['execution_diagnostic'], batches=len(batches),
        spans=sum(map(len, spans.values())), flushes=len(flushes), captures=captures,
        scope='Raw completeness only; CPU non-running wall is not GPU time. Nested spans must not be added.')
    save(output / 'execution-audit.json', result)
    return result


def audit_profile_diagnostics(db, tables, output):
    """Do not let complete-looking tables hide lost CUDA/NVTX trace records."""
    required = {'DIAGNOSTIC_EVENT', 'ENUM_DIAGNOSTIC_SEVERITY_LEVEL'}
    if not required <= tables:
        raise RuntimeError('missing Nsight diagnostic/severity tables')
    levels = {row['id']: row['name'] for row in
              db.execute('SELECT * FROM ENUM_DIAGNOSTIC_SEVERITY_LEVEL')}
    unsupported_um = re.compile(
        r'^CUDA device \d+: Unified Memory trace is not supported by the current '
        r'driver version or configuration\.$')
    loss = re.compile(r'\b(dropped|lost|overflow|truncat\w*|incomplete)\b', re.I)
    messages, errors, warnings, auxiliary, findings = [], [], [], [], []
    for source in db.execute('SELECT * FROM DIAGNOSTIC_EVENT'):
        row = dict(source)
        row['severity_name'] = levels.get(row['severity'], 'UNKNOWN')
        messages.append(row)
        message = row['text'].strip()
        if unsupported_um.fullmatch(message):
            auxiliary.append(row)
        elif loss.search(message) or row['severity_name'] in ('Error', 'UNKNOWN'):
            errors.append(row)
        elif row['severity_name'] == 'Warning':
            warnings.append(row)
    for name in ('server.log', 'export.log'):
        for number, line in enumerate((output / name).read_text().splitlines(), 1):
            # Application counters such as suffix incomplete=0 are not Nsight
            # diagnostic messages. Nsight [Warning]/[Error] lines remain visible.
            if (line.startswith(('[exec-', '[q4t]', 'MTP [')) or
                    re.match(r'^\[(?:suffix|mtp|startup|request)(?:\]|-)', line)):
                continue
            if loss.search(line) or re.search(r'\b(FATAL|ERROR):', line):
                findings.append(dict(file=name, line=number, text=line))
    return dict(messages=messages, errors=errors, warnings_require_review=warnings,
        unavailable_auxiliary_features=auxiliary, profiler_log_findings=findings,
        valid=not errors and not warnings and not findings)


def export_profile(plan, output):
    report, database = output / 'trace.nsys-rep', output / 'trace.sqlite'
    if not report.is_file() or report.stat().st_size == 0 or database.exists():
        raise RuntimeError('missing trace report or unexpected export destination')
    command = [plan['nsys']['path'], 'export', '--type=sqlite',
               '--output=' + str(database), str(report)]
    save(output / 'export-command.json', command)
    with (output / 'export.log').open('x') as stream:
        result = subprocess.run(command, cwd=ROOT, stdout=stream,
            stderr=subprocess.STDOUT, check=False,
            timeout=plan['nsys']['export_timeout_seconds'])
    save(output / 'export-exit.json', dict(returncode=result.returncode))
    if result.returncode != 0 or not database.is_file():
        raise RuntimeError('frozen Nsight SQLite export failed')
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        for table in ('CUPTI_ACTIVITY_KIND_KERNEL', 'CUPTI_ACTIVITY_KIND_RUNTIME', 'NVTX_EVENTS'):
            if table not in tables:
                raise RuntimeError('required Nsight kernel/API/NVTX table missing: ' + table)
            counts[table] = db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            if counts[table] <= 0:
                raise RuntimeError('empty required Nsight table: ' + table)
        try:
            diagnostics = audit_profile_diagnostics(db, tables, output)
        except (KeyError, ValueError, TypeError, RuntimeError, sqlite3.Error) as error:
            diagnostics = dict(valid=False, errors=[repr(error)], schema_incomplete=True)
        save(output / 'profile-diagnostics.json', diagnostics)
        if not diagnostics['valid']:
            raise RuntimeError('Nsight diagnostics report trace loss/error/warning or missing schema')
    save(output / 'trace-evidence.json', dict(report_sha256=legacy.sha(report),
        sqlite_sha256=legacy.sha(database), tables=sorted(tables), required_counts=counts,
        diagnostics_sha256=legacy.sha(output / 'profile-diagnostics.json'),
        performance_acceptance=False, complete_capture_requires_offline_audit=True))


def custom_step(plan, name, plan_path):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    output = Path(plan['output']) / name
    output.mkdir(parents=True, exist_ok=False)
    env = legacy.clean_env()
    if step['execution_diagnostic']:
        env['Q4T_EXEC_DIAGNOSTIC'] = '1'
    profile = step['measurement_role'] == 'nsys_profile'
    if profile:
        env['Q4T_EXEC_PROFILE_INPUT'] = str(step['profile_input_tokens'])
        env['Q4T_EXEC_NVTX'] = '1'
    command = server_command(plan, step)
    launch = command
    if profile:
        launch = [plan['nsys']['path'], 'profile', *plan['nsys']['profile_options'],
                  '--output=' + str(output / 'trace'), sys.executable, '-B',
                  str(Path(__file__).resolve()), '--plan', str(plan_path),
                  '--target-wrapper', name]
        save(output / 'profile-command.json', launch)
    save(output / 'server-command.json', dict(
        argv=command, environment={k:v for k,v in env.items() if k.startswith('Q4T_')},
        measurement_role=step['measurement_role'],
        diagnostic_only=step['execution_diagnostic'], performance_acceptance_sample=False,
        performance_acceptance=False))
    save(output / 'step.json', step)
    passed, failure, server = False, None, None
    cleanup_errors = []
    target, target_exit, server_pid = None, None, None
    with (output / 'server.log').open('x') as log:
        try:
            http.event(output, 'server_before_start')
            server = subprocess.Popen(launch, cwd=ROOT, env=env, stdout=log,
                                      stderr=subprocess.STDOUT)
            if profile:
                http.event(output, 'profile_launcher_started', pid=server.pid)
            else:
                server_pid = server.pid
                (output / 'server.pid').write_text(str(server_pid) + '\n')
                http.event(output, 'server_started', pid=server_pid)
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError('server exited during startup')
                if 'serving on port' in (output / 'server.log').read_text():
                    if profile:
                        target = json.loads((output / 'target-identity.json').read_text())
                        server_pid = target['pid']
                        actual = proc_identity(server_pid)
                        if (actual['start_ticks'] != target['start_ticks'] or
                                Path(f'/proc/{server_pid}/exe').resolve() != Path(plan['candidate']).resolve()):
                            raise RuntimeError('Nsight target identity differs from observation binary')
                        http.event(output, 'server_started', pid=server_pid,
                                   start_ticks=target['start_ticks'], launcher_pid=server.pid)
                    break
                time.sleep(1)
            else:
                raise TimeoutError('diagnostic startup exceeded 900 seconds')
            capacity = http.capacity_evidence((output / 'server.log').read_text(), 208896)
            save(output / 'capacity.json', capacity)
            http.event(output, 'server_ready', pid=server_pid, capacity=capacity)
            if not capacity['matches_requested']:
                raise RuntimeError('effective capacity mismatch')
            # The existing collector uses evalscope and writes raw HTTP/SSE,
            # SQLite, first-request markers and boundary timestamps.
            results = []
            for case in step['cases']:
                batch = admission.run_cases(
                    plan, dict(step, cases=[case]), output, server, env, capacity)
                results.extend(batch)
                save(output / 'results.json', results)
                result = batch[0]
                if result['outputs'] != [case['expected_output_sha256']] * case['repeats']:
                    raise RuntimeError('diagnostic output differs from frozen reference: ' + case['id'])
                if (len(result['responses']) != case['repeats'] or
                        any(row['actual_output'] != 256 or row['finish'] != ['length']
                            for row in result['responses'])):
                    raise RuntimeError('diagnostic output length/finish changed')
            save(output / 'output-reference-check.json', dict(
                passed=True, reference='old 6a binary same-head full output hashes',
                arbitrary_input_equivalence=False))
            raw_log = (output / 'server.log').read_text()
            if '[layer-timing]' in raw_log or '[mtp-timing]' in raw_log:
                raise RuntimeError('legacy intrusive timing leaked into execution-observation stage')
            passed = True
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            if server is not None:
                http.event(output, 'server_before_shutdown', pid=server_pid,
                           launcher_pid=server.pid if profile else None)
                try:
                    if profile:
                        target_exit = stop_profile_target(plan, output, server)
                    elif server.poll() is None:
                        server.terminate()
                        try:
                            server.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            server.kill()
                            server.wait(timeout=10)
                except BaseException as error:
                    cleanup_errors.append(repr(error))
                actual_code = (target_exit['returncode'] if target_exit else None) if profile else server.returncode
                http.event(output, 'server_after_shutdown' if not cleanup_errors else 'server_shutdown_failed',
                           pid=server_pid, server_returncode=actual_code,
                           launcher_returncode=server.returncode if profile else None,
                           errors=cleanup_errors)
            log.flush()
            execution = audit_execution(output, step, server_pid)
            actual_code = ((target_exit['returncode'] if target_exit else None) if profile else
                           server.returncode if server else None)
            clean = (passed and server is not None and server.returncode == 0 and
                     actual_code == 0 and not cleanup_errors and execution['complete'])
            save(output / 'exit.json', dict(server=actual_code,
                http_output_checks_passed=clean, failure=failure,
                cleanup_errors=cleanup_errors, execution_records_complete=execution['complete'],
                launcher=server.returncode if profile and server else None,
                trace_exports_complete=False if profile else None,
                diagnostic_only=step['execution_diagnostic'], performance_acceptance_sample=False,
                performance_acceptance=False))
    if not clean:
        raise RuntimeError('diagnostic group failed')
    if profile:
        export_profile(plan, output)
        result = json.loads((output / 'exit.json').read_text())
        result['trace_exports_complete'] = True
        save(output / 'exit.json', result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--custom-step')
    parser.add_argument('--target-wrapper')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    validate(plan)
    if args.custom_step and args.target_wrapper:
        parser.error('choose exactly one child mode')
    if args.target_wrapper:
        raise SystemExit(target_wrapper(plan, args.target_wrapper))
    elif args.custom_step:
        custom_step(plan, args.custom_step, args.plan.resolve())
    else:
        legacy.require_guard()
        telemetry.configure_sources(plan['telemetry_sources'])
        monitor = {}
        final_barrier = {}
        original_verify = legacy.verify_files
        verify_calls = 0

        def sample(output, stop, failures, heartbeat):
            monitor.update(failures=failures, heartbeat=heartbeat)
            telemetry.sample(output, stop, failures, heartbeat)

        def verify_with_final_flush(value):
            nonlocal verify_calls
            original_verify(value)
            verify_calls += 1
            if verify_calls != 2:
                return
            # The second verification follows every service shutdown and the
            # driver's child/process cleanup. Keep monitoring until a complete
            # sample has started and flushed after this boundary. Do not end
            # sampling early to avoid the process-exit path being investigated.
            boundary = time.monotonic()
            deadline = boundary + value['resource_sample_max_age_seconds']
            while True:
                if monitor.get('failures'):
                    raise legacy.EnvironmentAbort('final monitor flush failed: ' +
                                                  repr(monitor['failures']))
                heartbeat = dict(monitor.get('heartbeat', {}))
                flushed = heartbeat.get('last_sample', 0)
                sample_started = flushed - heartbeat.get('last_cycle_duration_seconds', 0)
                if ('last_cycle_duration_seconds' in heartbeat and
                        sample_started >= boundary):
                    final_barrier.update(boundary_monotonic=boundary,
                                         sample_started_monotonic=sample_started,
                                         flushed_monotonic=flushed)
                    return
                if time.monotonic() >= deadline:
                    raise legacy.EnvironmentAbort('no final post-shutdown monitor flush')
                time.sleep(.05)

        legacy.sample_memory = sample
        legacy.verify_files = verify_with_final_flush
        legacy.audit_server = admission.audit_server
        try:
            legacy.run_stage(plan, args.plan.resolve())
        finally:
            # The sampler can fail between the legacy driver's last heartbeat
            # check and its join. Preserve its raw state/seal, and separately
            # reject any such final failure before the guard sees a clean exit.
            state_path = Path(plan['output']) / 'state.json'
            if state_path.is_file():
                state = json.loads(state_path.read_text())
                final_errors = []
                summary_path = Path(plan['output']) / 'telemetry-summary.json'
                summary_sha = None
                try:
                    summary = json.loads(summary_path.read_text())
                    summary_sha = legacy.sha(summary_path)
                    if (summary['sampler_status'] != 'STOPPED' or
                            summary['sampler_failure'] is not None or
                            summary['failures_at_exit'] or
                            summary['unflushed_attempts'] != 0 or
                            summary['sample_attempts'] != summary['flushed_samples'] or
                            summary['flushed_samples'] < 1):
                        final_errors.append('final sampler summary is incomplete/failed')
                    if state.get('status') == 'MEASURED_REQUIRES_REVIEW' and (
                            not final_barrier or summary['last_flushed_heartbeat'] -
                            summary['final_successful_cycle_duration_seconds'] <
                            final_barrier['boundary_monotonic']):
                        final_errors.append('final summary lacks post-shutdown flush')
                except (OSError, KeyError, ValueError, TypeError) as error:
                    final_errors.append('final sampler summary: ' + repr(error))
                failed = bool(state.get('monitor_failures') or
                              state.get('cleanup_errors') or
                              state.get('status') == 'ENV_ABORT' or final_errors)
                child_cleanup = []
                for step in plan['steps']:
                    child_exit = Path(plan['output']) / step['name'] / 'exit.json'
                    if child_exit.is_file():
                        details = json.loads(child_exit.read_text())
                        problems = list(details.get('cleanup_errors', []))
                        if details.get('server') in (-signal.SIGKILL, -signal.SIGTERM):
                            problems.append('actual target did not complete graceful shutdown')
                        if step['measurement_role'] == 'nsys_profile':
                            receipt = child_exit.parent / 'profile-shutdown.json'
                            if details.get('server') is None or not receipt.is_file():
                                problems.append('profile target identity/exit receipt missing')
                            elif json.loads(receipt.read_text()).get('forced_target_kill'):
                                problems.append('profile target required forced kill')
                        if problems:
                            child_cleanup.append(dict(step=step['name'],
                                errors=problems))
                failed = failed or bool(child_cleanup)
                save(args.plan.resolve().parent / 'postflight.json', dict(
                    status='ENV_ABORT' if failed else state.get('status'),
                    state_sha256=legacy.sha(state_path),
                    monitor_failures=state.get('monitor_failures', []),
                    cleanup_errors=state.get('cleanup_errors', []),
                    child_cleanup_errors=child_cleanup,
                    final_summary_sha256=summary_sha,
                    final_monitor_barrier=final_barrier,
                    final_monitor_errors=final_errors,
                    note='Final sampler join reviewed; original raw state/seal remain unchanged.'))
                if failed:
                    raise legacy.EnvironmentAbort('final telemetry/cleanup failure')


if __name__ == '__main__':
    main()
