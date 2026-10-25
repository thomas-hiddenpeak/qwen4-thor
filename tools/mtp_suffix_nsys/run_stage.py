"""Bounded known-work controls, then conditional external-stop model profiles."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import signal
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    'nsys_execution_reused', ROOT / 'tools/mtp_suffix_execution/run_stage.py')
old = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(old)
legacy, save = old.legacy, old.save
sys.path.insert(0, str(HERE))
import fixture_audit
import interactive
import automatic

VARIANTS = [dict(name='direct-stop', wrapper=False, external=False),
            dict(name='wrapper-stop', wrapper=True, external=False),
            dict(name='direct-external', wrapper=False, external=True),
            dict(name='wrapper-external', wrapper=True, external=True)]
INTERACTIVE_VARIANTS = [
    dict(name='direct-interactive', wrapper=False, external=True,
         lifecycle='interactive'),
    dict(name='wrapper-interactive', wrapper=True, external=True,
         lifecycle='interactive')]
OPTIONS = ['--trace=cuda,nvtx', '--sample=none', '--cpuctxsw=none',
           '--cuda-trace-scope=process-tree', '--capture-range=cudaProfilerApi',
           '--capture-range-end=none', '--flush-on-cudaprofilerstop=true',
           '--kill=none', '--wait=all']
MODEL_STEPS = [('nsys8-off', False, 8170, 1), ('nsys8-on', True, 8170, 1),
               ('nsys200-on', True, 204781, 2), ('nsys200-off', False, 204781, 2)]
FIXTURE_CONTRACT = dict(iterations=32, kernels=32, h2d=32, d2h=32,
    nvtx_ranges=33, memset_apis=32, memset_bytes_even=512, memset_bytes_odd=16384,
    unknown_gpu_memory_coverage_blocks_model=True)
WRAPPER_WARNINGS = frozenset((
    'Not all NVTX events might have been collected.',
    'No NVTX events collected. Does the process use NVTX?',
    'CUDA profiling might have not been started correctly.',
    'No CUDA events collected. Does the process use CUDA?'))
_original_clean_env = legacy.clean_env
_original_stop_target = old.stop_profile_target
_original_export = old.export_profile
_original_diagnostics = old.audit_profile_diagnostics
_original_telemetry_sample = old.telemetry.sample
_original_stop_group = legacy.stop_group
_original_require_monitor = legacy.require_monitor
_failure_monitor = {}


def clean_env():
    """Only the frozen fixture config may enable NVLOG in new launches."""
    return {k: v for k, v in _original_clean_env().items()
            if not k.startswith(('NVLOG_', 'NSYS_')) and
            k not in ('QUADD_CUDA_CONFIG', 'CUDA_INJECTION64_PATH',
                      'NVTX_INJECTION64_PATH', 'CUPTI_INJECTION64_PATH')}


def validate(plan):
    fix = plan.get('monitor_fix', {})
    if fix.get('classification') == 'bounded_owned_exit_pending' and fix.get(
            'exit_pending') != dict(ttl_seconds=5, capacity=16, poll_timeout_ms=0):
        raise ValueError('frozen exit-pending monitor contract changed')
    controlled = 'interactive_contract' in plan['nsys']
    stage_seconds, fixture_seconds = (4800, 600) if controlled else (5400, 1200)
    variants = INTERACTIVE_VARIANTS if controlled else VARIANTS
    if controlled and (plan['nsys']['interactive_contract'] not in
            (interactive.contract(), automatic.contract()) or
            set(plan.get('fixture_reuse', {})) != {'direct-stop', 'wrapper-stop'} or
            plan.get('fixture_new_captures') != 2 or
            plan.get('fixture_reused_local_controls') != 2 or
            plan.get('fixture_candidate') != 'wrapper-interactive'):
        raise ValueError('interactive lifecycle or historical-control references changed')
    if ((plan['http_requests'], plan['http_loads'], plan['stage_timeout_seconds'],
         plan['direct_contract_timeout_seconds']) != (6, 4, stage_seconds, fixture_seconds) or
            plan['fixture_variants'] != variants or
            plan['fixture_timeout_seconds'] != 300 or
            plan['fixture_nvlog_limit_bytes'] != 64 * 1024 ** 2 or
            plan['fixture_contract'] != FIXTURE_CONTRACT or
            plan['nsys']['profile_options'] != OPTIONS or
            any(plan[k] != 0 for k in ('retries', 'warmups',
                'observation_build_limit', 'runtime_optimization_candidates'))):
        raise ValueError('frozen collector scope/options/budget changed')
    steps = plan['steps']
    if len(steps) != 5:
        raise ValueError('expected one collector contract and four model groups')
    first = steps[0]
    if (first['name'], first['kind'], first['measurement_role'],
            first['http_requests'], first['http_loads'], first['timeout'],
            first['execution_diagnostic'], first['cases']) != (
                'fixture-controls', 'contract', 'collector_contract', 0, 0,
                fixture_seconds, False, []):
        raise ValueError('collector contract must be the first step')
    for step, (name, suffix, tokens, requests) in zip(steps[1:], MODEL_STEPS):
        if (step['name'], step['head'], step['suffix'], step['measurement_role'],
            step['execution_diagnostic'], step['profile_input_tokens'],
            step['kind'], step['http_requests'], step['http_loads'], step['timeout']) != (
                name, 'subset', suffix, 'nsys_profile', True, tokens,
                'timing', requests, 1, 900):
            raise ValueError('frozen model profile order/configuration changed')
        expected = [('context-8192', 8170)]
        if requests == 2:
            expected.append(('context-204800', 204781))
        if [(c['id'], c['input_tokens']) for c in step['cases']] != expected or any(
                c['repeats'] != 1 or c['output_tokens'] != 256 or
                c['role'] != 'development_diagnostic' for c in step['cases']):
            raise ValueError('frozen profile request/predecessor changed')
    if sum(s['http_requests'] for s in steps) != 6:
        raise ValueError('HTTP budget mismatch')


def global_pid(value):
    return (int(value) >> 24) & 0xffffff


def pidfd_exited(fd, timeout_ms):
    """Only a bound pidfd's exit readiness can prove process termination."""
    poller = select.poll()
    accepted = select.POLLIN | select.POLLHUP
    allowed = accepted | getattr(select, 'POLLRDNORM', 0)
    poller.register(fd, allowed)
    events = poller.poll(timeout_ms)
    if not events:
        return False
    if (len(events) != 1 or events[0][0] != fd or
            events[0][1] & ~allowed or not events[0][1] & accepted):
        raise RuntimeError('invalid owned pidfd exit readiness: ' +
                           repr(dict(fd=fd, events=events)))
    return True


def sample_with_failure_context(output, stop, failures, heartbeat):
    _failure_monitor.update(output=output, stop=stop, failures=failures,
                            heartbeat=heartbeat, returned=False)
    try:
        return _original_telemetry_sample(output, stop, failures, heartbeat)
    finally:
        _failure_monitor['returned'] = True


def require_monitor_with_pending(sampler, failures, heartbeat, maximum_age):
    """Do not advance a step while previously seen exits remain unresolved.

    The sampler continues normally. The caller only waits for its full ledger;
    this adds no inference-stream synchronization or additional sampling.
    """
    deadline = time.monotonic() + maximum_age
    while True:
        _original_require_monitor(sampler, failures, heartbeat, maximum_age)
        if heartbeat.get('exit_pending_count', 0) == 0:
            return
        if time.monotonic() >= deadline:
            raise legacy.EnvironmentAbort('owned exits unresolved at monitor barrier')
        time.sleep(.05)


def stop_group_with_final_flush(child):
    """Keep monitoring the failed owned-step cleanup before legacy joins it."""
    receipt = dict(child_pid=child.pid, complete=False,
        scope=('Post-owned-step-PGID cleanup only. Detached Nsight descendants are '
               'handled and audited separately by the outer guard; this is not '
               'a post-all-descendant-cleanup barrier. Normal success uses the '
               'original final barrier.'))
    try:
        _original_stop_group(child)
        receipt['cleanup_returned'] = True
        boundary = time.monotonic()
        receipt['boundary_monotonic'] = boundary
        deadline = boundary + 5
        while True:
            if (_failure_monitor.get('failures') or _failure_monitor.get('returned') or
                    not _failure_monitor.get('heartbeat') or
                    _failure_monitor['stop'].is_set()):
                raise legacy.EnvironmentAbort('failure cleanup monitor unavailable/failed: ' +
                                              repr(_failure_monitor.get('failures')))
            heartbeat = dict(_failure_monitor['heartbeat'])
            flushed = heartbeat.get('last_sample', 0)
            duration = heartbeat.get('last_cycle_duration_seconds')
            if (duration is not None and flushed - duration >= boundary and
                    heartbeat.get('exit_pending_count', 0) == 0):
                receipt.update(complete=True, sample_started_monotonic=flushed-duration,
                               flushed_monotonic=flushed)
                return
            if time.monotonic() >= deadline:
                raise legacy.EnvironmentAbort('no fresh monitor flush after failed-step cleanup')
            time.sleep(.05)
    except BaseException as error:
        receipt['failure'] = repr(error)
        raise
    finally:
        receipt['monitor_failures'] = list(_failure_monitor.get('failures', []))
        if _failure_monitor.get('output') is not None:
            save(Path(_failure_monitor['output']).parent / 'failure-monitor-barrier.json', receipt)


def process_trace_counts(db, tables, pid):
    counts = {}
    for table in sorted(tables):
        if table != 'NVTX_EVENTS' and not table.startswith('CUPTI_ACTIVITY_KIND_'):
            continue
        columns = {r[1] for r in db.execute('PRAGMA table_info("' + table + '")')}
        column = 'globalTid' if 'globalTid' in columns else 'globalPid' if 'globalPid' in columns else None
        if column:
            counts[table] = db.execute('SELECT COUNT(*) FROM "' + table +
                '" WHERE ((' + column + ' >> 24) & 16777215)=?', (pid,)).fetchone()[0]
    return counts


def role_diagnostics(db, tables, output, target_pid, wrapper_pid=None):
    result = _original_diagnostics(db, tables, output)
    # SQLite enum zero is named UnknownLevel in the installed exporter.
    # Never silently treat an unfamiliar severity spelling as informational.
    for row in result['messages']:
        if row['severity_name'] not in ('Info', 'Warning', 'Error') and row not in result['errors']:
            result['errors'].append(row)
        if (row['severity_name'] == 'Warning' and
                row not in result['warnings_require_review'] and row not in result['errors']):
            # The inherited exact auxiliary-UM exception must not hide a
            # Warning: only the independently bound empty-wrapper rules below
            # may classify warnings as irrelevant to the actual target.
            result['warnings_require_review'].append(row)
    result['actual_target_pid'] = target_pid
    result['wrapper_pid'] = wrapper_pid
    result['wrapper_empty_process_warnings'] = []
    result['wrapper_trace_counts'] = {}
    if not isinstance(target_pid, int) or not 0 < target_pid < 2 ** 24:
        result['errors'].append(dict(text='invalid actual target PID'))
    if wrapper_pid is not None:
        if not isinstance(wrapper_pid, int) or not 0 < wrapper_pid < 2 ** 24 or wrapper_pid == target_pid:
            result['errors'].append(dict(text='invalid wrapper PID identity'))
        else:
            counts = process_trace_counts(db, tables, wrapper_pid)
            result['wrapper_trace_counts'] = counts
            if not counts or any(counts.values()):
                result['errors'].append(dict(text='wrapper has trace events or cannot prove empty process'))
            else:
                retained = []
                seen = set()
                for row in result['warnings_require_review']:
                    if (row.get('globalPid') is not None and global_pid(row['globalPid']) == wrapper_pid and
                            row['text'].strip() in WRAPPER_WARNINGS and row['text'].strip() not in seen):
                        seen.add(row['text'].strip())
                        result['wrapper_empty_process_warnings'].append(row)
                    else:
                        retained.append(row)
                result['warnings_require_review'] = retained
    result['valid'] = not (result['errors'] or result['warnings_require_review'] or
                           result['profiler_log_findings'])
    result['scope'] = ('Only exact four empty non-CUDA wrapper warnings are classified. '
                       'All actual-target and unknown-process warnings remain blocking.')
    return result


def audit_profile_diagnostics(db, tables, output):
    identity = json.loads((output / 'target-identity.json').read_text())
    return role_diagnostics(db, tables, output, identity['pid'], identity['wrapper_pid'])


def session_name(name):
    return interactive.make_name(name)


def automatic_collection(plan):
    return plan['nsys'].get('interactive_contract') == automatic.contract()


def session_class(plan):
    return automatic.Session if automatic_collection(plan) else interactive.Session


def finish_interactive(session, errors):
    """Close control ownership after the independently owned target is stopped."""
    receipt = dict(session=session.name, shutdown=None, adopted=None, errors=[],
        scope='Only this session and proven adopted identities; outer guard audits all descendants.')
    if session.can_shutdown and not session.shutdown_attempted:
        try:
            receipt['shutdown'] = session.shutdown()
        except BaseException as error:
            receipt['errors'].append('session shutdown: ' + repr(error))
    try:
        receipt['adopted'] = session.reap_adopted(timeout=5)
        if receipt['adopted']['remaining'] or receipt['adopted']['not_children']:
            receipt['errors'].append('proven session identities remain after shutdown')
    except BaseException as error:
        receipt['errors'].append('adopted identity reap: ' + repr(error))
    try:
        session.close()
    except BaseException as error:
        receipt['errors'].append('subreaper scope close: ' + repr(error))
    errors.extend(receipt['errors'])
    save(session.output / 'interactive-final-cleanup.json', receipt)


def bind_model_target(plan, output, session, *, cleanup=False):
    target = json.loads((output / 'target-identity.json').read_text())
    actual = old.proc_identity(target['pid'])
    if (actual['start_ticks'] != target['start_ticks'] or
            actual['state'] in ('Z', 'X') or actual['ppid'] != target['wrapper_pid'] or
            Path(f'/proc/{target["pid"]}/exe').resolve() != Path(plan['candidate']).resolve()):
        raise RuntimeError('interactive target identity differs from observation binary/wrapper')
    ownership = session.prove_descendant(target['pid'])
    wrapper = session.prove_descendant(target['wrapper_pid'])
    if ownership['target']['start_ticks'] != actual['start_ticks']:
        raise RuntimeError('interactive target changed during owner binding')
    receipt = 'target-cleanup-identity.json' if cleanup else 'target-owned-identity.json'
    save(output / receipt, dict(target=actual,
        wrapper=wrapper['target'], ownership=ownership,
        candidate=plan['candidate'], scope='Stable step-owner ancestry, exact executable and wrapper.'))
    return target


def session_stop(plan, output, name, *, timeout=45, check=None):
    """Complete exactly the preselected lifecycle; no stop-error fallback."""
    if not re.fullmatch(r'q4t-suffix-[0-9]+-[a-z0-9-]+', name):
        raise ValueError('refusing non-owned or malformed session name')
    session = plan.get('_interactive_session')
    if (session is None or session.name != name or
            Path(session.output).resolve() != Path(output).resolve()):
        raise RuntimeError('external stop requires the bound interactive session')
    if check:
        check()
    if automatic_collection(plan):
        result = session.await_report(timeout=timeout, check=check)
        # The target is still held alive. This cancels no active collection:
        # report-ready has already acknowledged its automatic completion.
        if check:
            check()
        session.shutdown()
        return result
    return session.stop(timeout=timeout, check=check)


def capture_end_receipt(text, target_pid):
    captures = [dict(re.findall(r'(\w+)=(\S+)', line)) for line in text.splitlines()
                if line.startswith('[exec-capture]')]
    if ([r.get('event') for r in captures] != ['begin', 'end'] or
            any(r.get('pid') != str(target_pid) or r.get('error') != '0' for r in captures)):
        raise RuntimeError('successful actual-target capture end receipt missing')
    return captures[-1]


def stop_profile_target(plan, output, launcher):
    control_error, target = None, None
    try:
        identity = json.loads((output / 'target-identity.json').read_text())
        deadline = time.monotonic() + 5
        while True:
            raw = (output / 'server.log').read_text()
            if re.search(r'^\[exec-capture\] event=end ', raw, re.M):
                receipt = capture_end_receipt(raw, identity['pid'])
                break
            launcher_code = launcher.poll()
            if ((launcher_code is not None and
                 (not automatic_collection(plan) or launcher_code != 0)) or
                    time.monotonic() >= deadline):
                raise RuntimeError('actual-target capture end did not arrive before stop')
            time.sleep(.05)
        save(output / 'external-stop-boundary.json', dict(
            receipt=receipt, observed_monotonic=time.monotonic(),
            session=plan['_active_session'], target_pid=identity['pid']))
        def target_alive():
            actual = old.proc_identity(identity['pid'])
            if (actual['start_ticks'] != identity['start_ticks'] or
                    actual['state'] in ('Z', 'X')):
                raise RuntimeError('model target exited before report completion')
        session_stop(plan, output, plan['_active_session'], timeout=45,
                     check=target_alive)
    except BaseException as error:
        control_error = error
    finally:
        # Even startup/capture failures need a fresh stable-owner proof before
        # the inherited pidfd signal path may consume a target identity file.
        # An unproved target is left to the outer owned-descendant guard.
        session = plan.get('_interactive_session')
        if session is not None:
            try:
                bind_model_target(plan, output, session, cleanup=True)
            except BaseException as error:
                save(output / 'target-cleanup-authorization.json', dict(
                    authorized=False, failure=repr(error),
                    control_failure=repr(control_error) if control_error else None,
                    scope='No target signal authorized; outer owned-process guard required.'))
                raise RuntimeError('interactive target cleanup ownership unproved') from error
        target = _original_stop_target(plan, output, launcher)
    if control_error is not None:
        control_error.actual_target_exit = target
        raise control_error
    return target


def export_database(plan, output, deadline=None, check=None):
    report, database = output / 'trace.nsys-rep', output / 'trace.sqlite'
    if not report.is_file() or report.stat().st_size == 0 or database.exists():
        raise RuntimeError('missing trace report or existing export destination')
    command = [plan['nsys']['path'], 'export', '--type=sqlite',
               '--output=' + str(database), str(report)]
    save(output / 'export-command.json', command)
    child, failure = None, None
    limit = time.monotonic() + plan['nsys']['export_timeout_seconds']
    if deadline is not None:
        limit = min(limit, deadline)
    try:
        with (output / 'export.log').open('x') as stream:
            child = subprocess.Popen(command, cwd=ROOT, env=clean_env(), stdout=stream,
                                     stderr=subprocess.STDOUT)
            while child.poll() is None:
                if check:
                    check()
                if time.monotonic() >= limit:
                    raise TimeoutError('frozen SQLite export timeout')
                time.sleep(.05)
            if child.returncode or not database.is_file():
                raise RuntimeError('SQLite export failed')
    except BaseException as error:
        failure = repr(error)
        raise
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        save(output / 'export-exit.json', dict(returncode=child.returncode if child else None,
                                              failure=failure))
    return database


def export_profile(plan, output):
    # Original exporter now invokes the role-aware diagnostic function above.
    _original_export(plan, output)
    identity = json.loads((output / 'target-identity.json').read_text())
    import analyze_gpu
    try:
        result = analyze_gpu.audit_database(output / 'trace.sqlite', identity['pid'],
            server_text=(output / 'server.log').read_text(),
            export_text=(output / 'export.log').read_text(),
            scratch_dir=output / 'audit-scratch', mode='model',
            wrapper_pid=identity['wrapper_pid'])
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        result = dict(trace_integrity='FAIL', analysis_error=repr(error),
                      performance_acceptance=False)
    save(output / 'reverse-coverage-audit.json', result)
    if result['trace_integrity'] != 'PASS':
        raise RuntimeError('model trace reverse coverage/diagnostic audit failed')
    evidence = json.loads((output / 'trace-evidence.json').read_text())
    evidence.update(reverse_coverage_sha256=legacy.sha(output / 'reverse-coverage-audit.json'),
                    complete_capture_requires_offline_audit=False,
                    stage_environment_requires_final_audit=True)
    save(output / 'trace-evidence.json', evidence)


def fixture_rows(path, marker):
    result = []
    if path.exists():
        raw = path.read_text()
        lines = raw.splitlines()
        if raw and not raw.endswith('\n'):
            lines = lines[:-1]  # A live stdio write can be between bytes.
        for line in lines:
            if line.startswith('[' + marker + '] '):
                result.append(json.loads(line.split('] ', 1)[1]))
    return result


def fixture_ready(path):
    rows = fixture_rows(path, 'fixture-ready')
    if len(rows) > 1:
        raise RuntimeError('duplicate fixture ready receipt')
    if not rows:
        return None
    row = rows[0]
    if (row.get('passed') is not True or row.get('completed_steps') != 32 or
            any(row.get(k) != 0 for k in ('profiler_start_error',
                'profiler_stop_error', 'final_sync_error'))):
        raise RuntimeError('fixture ready reported failed computation or stop')
    return row


def nvlog_evidence(path, target_pid):
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError('fixture NVLOG file missing/empty')
    pid_pattern = re.compile(r'\bpid=\s*' + str(target_pid) + r'\s')
    target = [line for line in path.read_text(errors='replace').splitlines()
              if pid_pattern.search(line)]
    if not target:
        raise RuntimeError('fixture NVLOG has no bound actual-target PID records')
    flags = [int(match.group(1)) for line in target
             if (match := re.search(r'Flushing activity records with flag:\s*(\d+)', line))]
    loss = re.compile(r'(?:CUPTI dropped|Number of incomplete CUPTI events dropped:|'
                      r'Could not parse|Couldn.t allocate CUPTI buffers)\s*[1-9]\d*', re.I)
    failures = [line for line in target if loss.search(line)]
    done = [line for line in target if 'ReportCUDADone: DONE.' in line]
    nvtx_terminal = [line for line in target if re.search(
        r'NVTXFinish|NVTX profiling (?:stopped|finished)|ReportNVTX.*DONE', line)]
    return dict(path=str(path), sha256=legacy.sha(path), bytes=path.stat().st_size,
        target_pid=target_pid, target_log_lines=len(target), flush_flags=flags,
        cuda_done_records=done, explicit_loss_findings=failures,
        nvtx_terminal_records=nvtx_terminal,
        terminal_evidence_present=bool(flags and done),
        dropped_count=None, missing_count_is_not_zero=True,
        scope='NVLOG producer records; no inferred zero drop count or warning waiver.')


def fixture_wrapper(plan, name):
    legacy.require_guard()
    variant = next(v for v in plan['fixture_variants'] if v['name'] == name)
    if not variant['wrapper']:
        raise ValueError('fixture wrapper forbidden for direct variant')
    output = Path(plan['output']) / 'fixture-controls' / name
    command = [plan['fixture_binary'], '--release', str(output / 'release')]
    child = None
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('owned fixture wrapper interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        child = subprocess.Popen(command, cwd=ROOT)
        identity = old.proc_identity(child.pid)
        old.save_atomic(output / 'fixture-target-identity.json', dict(identity,
            wrapper_pid=os.getpid(), wrapper_start_ticks=old.proc_identity(os.getpid())['start_ticks'],
            executable=plan['fixture_binary'], command=command))
        child.wait()
    finally:
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            old.save_atomic(output / 'fixture-target-exit.json', dict(pid=child.pid,
                wrapper_pid=os.getpid(), returncode=child.returncode,
                finished_monotonic=time.monotonic()))
    return 0 if child is not None and child.returncode == 0 else 1


def pin_fixture(plan, output, launcher_pid, receipt_pid, wrapper, session=None):
    identity = old.proc_identity(receipt_pid)
    if identity['state'] in ('Z', 'X') or Path(f'/proc/{receipt_pid}/exe').resolve() != Path(plan['fixture_binary']).resolve():
        raise RuntimeError('fixture executable/liveness mismatch')
    ownership = None
    if session is not None:
        ownership = session.prove_descendant(receipt_pid)
        if ownership['target']['start_ticks'] != identity['start_ticks']:
            raise RuntimeError('interactive fixture ownership identity changed')
    else:
        ancestor, belongs = receipt_pid, False
        for _ in range(64):
            if ancestor == launcher_pid:
                belongs = True
                break
            if ancestor <= 1:
                break
            ancestor = old.proc_identity(ancestor)['ppid']
        if not belongs:
            raise RuntimeError('fixture PID is not our launcher descendant')
    if wrapper:
        declared = json.loads((output / 'fixture-target-identity.json').read_text())
        if declared['pid'] != receipt_pid or declared['start_ticks'] != identity['start_ticks']:
            raise RuntimeError('wrapper fixture identity mismatch')
        if session is not None:
            actual_wrapper = session.prove_descendant(declared['wrapper_pid'])['target']
            if (identity['ppid'] != declared['wrapper_pid'] or
                    actual_wrapper['start_ticks'] != declared['wrapper_start_ticks']):
                raise RuntimeError('interactive fixture wrapper identity mismatch')
        identity.update(wrapper_pid=declared['wrapper_pid'])
    else:
        identity.update(wrapper_pid=None)
    fd = os.pidfd_open(receipt_pid)
    # Recheck after acquiring pidfd; a recycled PID must never receive a signal.
    try:
        if old.proc_identity(receipt_pid)['start_ticks'] != identity['start_ticks']:
            raise RuntimeError('fixture identity changed while pinning')
        if ownership is not None:
            identity['interactive_ownership'] = ownership
        old.save_atomic(output / 'fixture-owned-identity.json', identity)
    except BaseException:
        os.close(fd)
        raise
    return identity, fd


def fixture_capture(plan, variant, plan_path, parent_output, group_deadline):
    name = variant['name']
    controlled = variant.get('lifecycle') == 'interactive'
    if variant['external'] and not controlled:
        raise ValueError('external collection requires interactive start/launch')
    output = parent_output / name
    output.mkdir()
    started = time.monotonic()
    deadline = min(group_deadline, started + plan['fixture_timeout_seconds'])
    session = session_name(name)
    config = output / 'nvlog.config'
    config.write_text(plan['fixture_log_config'].format(log=str(output / 'nvlog.log')))
    env = clean_env() | {'NVLOG_CONFIG_FILE': str(config)}
    options = [value.replace('--capture-range-end=none', '--capture-range-end=' +
               ('none' if variant['external'] else 'stop')) for value in OPTIONS]
    target = ([sys.executable, '-B', str(Path(__file__).resolve()), '--plan', str(plan_path),
               '--fixture-wrapper', name] if variant['wrapper'] else
              [plan['fixture_binary'], '--release', str(output / 'release')])
    command = None
    if not controlled:
        command = [plan['nsys']['path'], 'profile', *options,
                   '--session-new=' + session, '--env-var=NVLOG_CONFIG_FILE=' + str(config),
                   '--output=' + str(output / 'trace'), *target]
        save(output / 'profile-command.json', command)
    save(output / 'variant.json', dict(variant, session=session, config_sha256=legacy.sha(config)))
    launcher, fd, identity, failure = None, None, None, None
    control, control_closed = None, False
    cleanup_errors = []
    clean, result, forced = False, None, False

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('fixed fixture budget expired: ' + name)
        paths = list(parent_output.glob('*/nvlog.log'))
        sizes = {str(p): p.stat().st_size for p in paths}
        if (any(v > plan['fixture_nvlog_limit_bytes'] for v in sizes.values()) or
                sum(sizes.values()) > len(plan['fixture_variants']) * plan['fixture_nvlog_limit_bytes']):
            raise RuntimeError('frozen NVLOG size budget exceeded')

    try:
        with (output / 'server.log').open('x') as stream:
            if controlled:
                control = session_class(plan)(plan['nsys']['path'], output, session,
                    cwd=ROOT, env=env, deadline=deadline, check=check)
                control.start()
                launcher = control.launch(target, stream,
                    envvars={'NVLOG_CONFIG_FILE': str(config)})
            else:
                launcher = subprocess.Popen(command, cwd=ROOT, env=env,
                    stdout=stream, stderr=subprocess.STDOUT)
            save(output / 'launcher-identity.json', old.proc_identity(launcher.pid))
            while True:
                check()
                launcher_code = launcher.poll()
                if (launcher_code is not None and
                        (not automatic_collection(plan) or launcher_code != 0)):
                    raise RuntimeError('fixture launcher exited before ready')
                if fixture_rows(output / 'server.log', 'fixture-error'):
                    raise RuntimeError('fixture error before ready')
                configs = fixture_rows(output / 'server.log', 'fixture-config')
                if len(configs) > 1:
                    raise RuntimeError('duplicate fixture identity receipt')
                if configs and identity is None and (not variant['wrapper'] or
                        (output / 'fixture-target-identity.json').is_file()):
                    identity, fd = pin_fixture(plan, output, launcher.pid,
                        configs[0]['pid'], variant['wrapper'], session=control)
                if fd is not None and pidfd_exited(fd, 0):
                    raise RuntimeError('actual fixture exited before ready')
                ready = fixture_ready(output / 'server.log')
                if ready is not None:
                    if identity is None and variant['wrapper']:
                        time.sleep(.05)
                        continue
                    if identity is None or ready['pid'] != identity['pid']:
                        raise RuntimeError('fixture config/ready identity mismatch')
                    save(output / 'ready-boundary.json', dict(receipt=ready,
                        observed_monotonic=time.monotonic(), session=session))
                    break
                time.sleep(.05)
            if variant['external']:
                def target_alive():
                    check()
                    if pidfd_exited(fd, 0):
                        raise RuntimeError('fixture exited before report completion')
                session_stop(dict(plan, _interactive_session=control), output, session,
                    timeout=min(45, deadline-time.monotonic()), check=target_alive)
            # No CUDA cleanup until the selected collection has completed.
            (output / 'release').touch(exist_ok=False)
            # Auto completion may end launch control before the held target.
            # Observe both independently after releasing actual fixture work.
            while launcher.poll() is None or not pidfd_exited(fd, 0):
                check()
                time.sleep(.05)
            exits = fixture_rows(output / 'server.log', 'fixture-exit')
            if (launcher.returncode != 0 or len(exits) != 1 or
                    exits[0].get('pid') != identity['pid'] or exits[0].get('released') is not True or
                    exits[0].get('returncode') != 0):
                raise RuntimeError('fixture target/launcher did not finish normally')
            if variant['wrapper']:
                target_exit = json.loads((output / 'fixture-target-exit.json').read_text())
                if (target_exit['pid'] != identity['pid'] or target_exit['wrapper_pid'] != identity['wrapper_pid'] or
                        target_exit['returncode'] != 0):
                    raise RuntimeError('actual wrapped fixture wait status failed')
            else:
                save(output / 'fixture-target-exit.json', dict(
                    pid=identity['pid'], wrapper_pid=None, receipt_returncode=exits[0]['returncode'],
                    exit_observed_via_pidfd=True, launcher_returncode=launcher.returncode,
                    scope='Direct fixture own main-return receipt plus observed exit; not waitpid status.'))
        if control is not None:
            finish_interactive(control, cleanup_errors)
            control_closed = True
            if cleanup_errors:
                raise legacy.EnvironmentAbort('interactive fixture session cleanup failed')
        check()
        database = export_database(plan, output, deadline, check)
        internal = nvlog_evidence(output / 'nvlog.log', identity['pid'])
        save(output / 'nvlog-evidence.json', internal)
        if internal['explicit_loss_findings']:
            raise RuntimeError('fixture NVLOG reports explicit dropped/incomplete records')
        result = fixture_audit.audit(database, output / 'server.log', identity['pid'])
        save(output / 'known-work-audit.json', result)
        if not result['fixture_contract_passed']:
            raise RuntimeError('known fixture work/diagnostic error failed')
        negatives = fixture_audit.negative_copies(database, output / 'server.log',
                                                 output / 'negative-copies', identity['pid'])
        save(output / 'negative-controls.json', negatives)
        with fixture_audit.connect(database) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            diagnostics = role_diagnostics(db, tables, output, identity['pid'], identity['wrapper_pid'])
        save(output / 'profile-diagnostics.json', diagnostics)
        check()
        clean = True
        return dict(name=name, known_work_complete=True,
            negative_controls_passed=all(v['expected_rejected'] for v in negatives['derivatives'].values()),
            gpu_memory_coverage_complete=result['gpu_memory_coverage_complete'],
            memory_coverage=result['memory_coverage'],
            diagnostics_valid=diagnostics['valid'], actual_target_pid=identity['pid'],
            terminal_evidence_present=internal['terminal_evidence_present'],
            wrapper_pid=identity['wrapper_pid'], output=str(output),
            raw_hashes={p.name: legacy.sha(p) for p in [database, output / 'server.log',
                output / 'known-work-audit.json', output / 'profile-diagnostics.json']})
    except BaseException as error:
        failure = repr(error)
        raise
    finally:
        # Failures release only this fixture, then terminate only pinned owned
        # process handles. Whole-stage guard remains responsible for descendants.
        launcher_live = launcher is not None and launcher.poll() is None
        target_live = False
        if fd is not None:
            try:
                target_live = not pidfd_exited(fd, 0)
            except BaseException as error:
                cleanup_errors.append('target exit probe: ' + repr(error))
                target_live = True
        if launcher_live or target_live:
            forced = True
            try:
                (output / 'release').touch(exist_ok=True)
            except BaseException as error:
                cleanup_errors.append('release receipt: ' + repr(error))
            if fd is None and launcher_live:
                try:
                    # Initialization may fail/hang before the first receipt.
                    # Discover only descendants of our still-owned launcher,
                    # then pin the exact fixture executable before signalling.
                    records = legacy.gpu_exclusive.processes()
                    owned = {control.owner['pid'] if control is not None else launcher.pid}
                    for _ in range(64):
                        expanded = owned | {pid for pid, row in records.items()
                                            if row.get('ppid') in owned}
                        if expanded == owned:
                            break
                        owned = expanded
                    for pid in owned - {control.owner['pid'] if control is not None else launcher.pid}:
                        try:
                            if Path(f'/proc/{pid}/exe').resolve() == Path(plan['fixture_binary']).resolve():
                                identity, fd = pin_fixture(plan, output, launcher.pid, pid,
                                    variant['wrapper'], session=control)
                                break
                        except (FileNotFoundError, ProcessLookupError):
                            continue
                except BaseException as error:
                    cleanup_errors.append('owned target discovery: ' + repr(error))
            if fd is not None:
                try:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except BaseException as error:
                    cleanup_errors.append('target terminate: ' + repr(error))
            # Identity/discovery errors above must never skip the Popen child.
            if launcher is not None and launcher.poll() is None:
                try:
                    launcher.terminate()
                    try:
                        launcher.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        launcher.kill()
                        launcher.wait(timeout=5)
                except BaseException as error:
                    cleanup_errors.append('launcher terminate: ' + repr(error))
            if fd is not None:
                try:
                    if not pidfd_exited(fd, 1000):
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                        if not pidfd_exited(fd, 5000):
                            raise RuntimeError('owned pinned fixture survived cleanup')
                except BaseException as error:
                    cleanup_errors.append('target reap observation: ' + repr(error))
        if fd is not None:
            try:
                os.close(fd)
            except OSError as error:
                cleanup_errors.append('target handle close: ' + repr(error))
        if control is not None and not control_closed:
            finish_interactive(control, cleanup_errors)
        save(output / 'exit.json', dict(complete=clean, failure=failure,
            launcher=launcher.returncode if launcher else None, cleanup_errors=cleanup_errors,
            forced_termination=forced,
            started_monotonic=started, finished_monotonic=time.monotonic(),
            performance_acceptance=False, actual_fixture_pid=identity['pid'] if identity else None))
        if cleanup_errors:
            raise legacy.EnvironmentAbort('fixture owned cleanup failed: ' + repr(cleanup_errors))


def reused_control_evidence(plan):
    """Bind historical local contracts without promoting their failed stage."""
    references = plan['fixture_reuse']
    if set(references) != {'direct-stop', 'wrapper-stop'}:
        raise ValueError('expected the two historical ordinary-stop references')
    controls = {}
    for name in ('direct-stop', 'wrapper-stop'):
        ref = references[name]
        source_plan = Path(ref['source_plan'])
        output = Path(ref['output'])
        seal_path = Path(ref['source_raw_seal'])
        if (legacy.sha(source_plan) != ref['source_plan_sha256'] or
                legacy.sha(seal_path) != ref['source_raw_seal_sha256'] or
                ref['source_stage_validity'] != 'ENV_ABORT' or
                ref['scope'] != 'historical_partial_known_work_only; no new sample or diagnostics pass'):
            raise RuntimeError('historical control source identity/scope changed')
        parent = json.loads(source_plan.read_text())
        source_run = Path(parent['output'])
        if (output != source_run / 'fixture-controls' / name or
                seal_path != source_run / 'files.json' or
                parent['fixture_binary_sha256'] != plan['fixture_binary_sha256'] or
                parent['fixture_contract'] != plan['fixture_contract']):
            raise RuntimeError('historical fixture computation contract differs')
        sealed = json.loads(seal_path.read_text())
        expected = dict(ref['raw_hashes'])
        if set(expected) != {'trace.sqlite', 'server.log', 'known-work-audit.json',
                            'profile-diagnostics.json'}:
            raise RuntimeError('historical raw identity inventory changed')
        if (expected['known-work-audit.json'] != ref['known_work_audit_sha256'] or
                expected['profile-diagnostics.json'] != ref['profile_diagnostics_sha256']):
            raise RuntimeError('conflicting historical raw identity references')
        expected.update({'known-work-audit.json': ref['known_work_audit_sha256'],
            'negative-controls.json': ref['negative_controls_sha256'],
            'exit.json': ref['exit_sha256'],
            'profile-diagnostics.json': ref['profile_diagnostics_sha256']})
        for relative, digest in expected.items():
            path = output / relative
            if (legacy.sha(path) != digest or
                    sealed.get(str(path.relative_to(source_run))) != digest):
                raise RuntimeError('historical control raw dependency changed: ' + relative)
        audit = json.loads((output / 'known-work-audit.json').read_text())
        negatives = json.loads((output / 'negative-controls.json').read_text())
        exit_row = json.loads((output / 'exit.json').read_text())
        diagnostics = json.loads((output / 'profile-diagnostics.json').read_text())
        if (audit.get('known_work_complete') is not True or
                audit.get('fixture_contract_passed') is not True or audit['errors'] or
                audit['input_hashes'] != dict(database=expected['trace.sqlite'],
                                             receipt=expected['server.log']) or
                negatives['original_sha256'] != expected['trace.sqlite'] or
                set(negatives['derivatives']) != {'drop-launch-kernel-pair', 'drop-last-nvtx'} or
                exit_row.get('complete') is not True or exit_row['cleanup_errors'] or
                exit_row['forced_termination'] or exit_row['failure'] is not None or
                exit_row['actual_fixture_pid'] != audit['expected_pid'] or
                diagnostics['actual_target_pid'] != audit['expected_pid']):
            raise RuntimeError('historical local fixture/negative/exit contract failed')
        for item in negatives['derivatives'].values():
            path = Path(item['path'])
            if (not path.is_relative_to(output / 'negative-copies') or
                    item['expected_rejected'] is not True or
                    legacy.sha(path) != item['sha256'] or
                    sealed.get(str(path.relative_to(source_run))) != item['sha256']):
                raise RuntimeError('historical negative copy identity/verdict changed')
        controls[name] = dict(reference=ref, checks_passed=True,
            known_work_complete=True, negative_controls_passed=True,
            diagnostics_valid=diagnostics['valid'],
            warnings_require_review=diagnostics['warnings_require_review'],
            source_stage_validity='ENV_ABORT', new_sample=False)
    return dict(controls=controls, scope='Historical local contracts only; '
        'not pooled with new captures, no environment or diagnostics promotion.')


def collector_gate(results, reused=None):
    variants = INTERACTIVE_VARIANTS if reused is not None else VARIANTS
    if [row['name'] for row in results] != [v['name'] for v in variants]:
        return False
    if reused is not None and (set(reused.get('controls', {})) != {'direct-stop', 'wrapper-stop'} or
            any(row.get('checks_passed') is not True or row.get('new_sample') is not False or
                row.get('source_stage_validity') != 'ENV_ABORT'
                for row in reused['controls'].values())):
        return False
    chosen = results[-1]
    return (all(r['known_work_complete'] and r['negative_controls_passed'] for r in results)
            and chosen['diagnostics_valid'] and chosen['terminal_evidence_present']
            and chosen['gpu_memory_coverage_complete'])


def fixture_controls(plan, plan_path):
    legacy.require_guard()
    output = Path(plan['output']) / 'fixture-controls'
    output.mkdir()
    results, failure, reused = [], None, None
    started = time.monotonic()
    try:
        if 'interactive_contract' in plan['nsys']:
            reused = reused_control_evidence(plan)
            save(output / 'reused-controls.json', reused)
        for variant in plan['fixture_variants']:
            results.append(fixture_capture(plan, variant, plan_path, output,
                started + plan['direct_contract_timeout_seconds']))
            save(output / 'results.json', results)
        passed = collector_gate(results, reused)
        candidate = 'wrapper-interactive' if reused is not None else 'wrapper-external'
        save(output / 'gate.json', dict(passed=passed, candidate=candidate,
            plan_sha256=legacy.sha(plan_path), results=results,
            new_captures=len(results), reused_local_controls=len(reused['controls']) if reused else 0,
            reused_controls_sha256=legacy.sha(output / 'reused-controls.json') if reused else None,
            scope='Known work and selected target diagnostics only; every model trace reaudited.'))
        if not passed:
            raise RuntimeError('fixed collector gate did not clear')
    except BaseException as error:
        failure = repr(error)
        raise
    finally:
        child_cleanup = []
        for variant in plan['fixture_variants']:
            path = output / variant['name'] / 'exit.json'
            if path.is_file():
                row = json.loads(path.read_text())
                if row.get('cleanup_errors') or row.get('forced_termination'):
                    child_cleanup.append(dict(variant=variant['name'],
                        errors=row.get('cleanup_errors', []),
                        forced_termination=row.get('forced_termination', False)))
        save(output / 'exit.json', dict(complete=failure is None, failure=failure,
            completed_variants=len(results), new_captures=len(results),
            reused_local_controls=len(reused['controls']) if reused else 0,
            cleanup_errors=child_cleanup,
            started_monotonic=started, finished_monotonic=time.monotonic(),
            measurement_role='collector_contract', performance_acceptance=False))


def interactive_model_step(plan, name, plan_path):
    """Keep the inherited HTTP/target contracts with interactive collection."""
    legacy.require_guard()
    step = next(row for row in plan['steps'] if row['name'] == name)
    if step['measurement_role'] != 'nsys_profile' or not step['execution_diagnostic']:
        raise ValueError('interactive model step requires frozen execution diagnostics')
    output = Path(plan['output']) / name
    output.mkdir(parents=True, exist_ok=False)
    env = clean_env() | {'Q4T_EXEC_DIAGNOSTIC': '1', 'Q4T_EXEC_NVTX': '1',
                        'Q4T_EXEC_PROFILE_INPUT': str(step['profile_input_tokens'])}
    command = old.server_command(plan, step)
    target_command = [sys.executable, '-B', str(Path(__file__).resolve()),
                      '--plan', str(plan_path), '--target-wrapper', name]
    save(output / 'server-command.json', dict(argv=command,
        environment={k:v for k,v in env.items() if k.startswith('Q4T_')},
        measurement_role=step['measurement_role'], diagnostic_only=True,
        performance_acceptance_sample=False, performance_acceptance=False))
    save(output / 'step.json', step)
    deadline = time.monotonic() + step['timeout']

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('frozen interactive model group budget expired')

    session = session_class(plan)(plan['nsys']['path'], output, session_name(name),
        cwd=ROOT, env=env, deadline=deadline, check=check)
    local = dict(plan, _active_session=session.name, _interactive_session=session)
    passed, failure, server, server_pid, target_exit = False, None, None, None, None
    cleanup_errors = []
    with (output / 'server.log').open('x') as log:
        try:
            old.http.event(output, 'server_before_start')
            session.start()
            server = session.launch(target_command, log,
                envvars={k:v for k,v in env.items() if k.startswith('Q4T_')})
            old.http.event(output, 'profile_launcher_started', pid=server.pid)
            while True:
                check()
                if server.poll() is not None:
                    raise RuntimeError('server launcher exited during startup')
                if 'serving on port' in (output / 'server.log').read_text():
                    target = bind_model_target(plan, output, session)
                    server_pid = target['pid']
                    old.http.event(output, 'server_started', pid=server_pid,
                        start_ticks=target['start_ticks'], launcher_pid=server.pid)
                    break
                time.sleep(1)
            capacity = old.http.capacity_evidence((output / 'server.log').read_text(), 208896)
            save(output / 'capacity.json', capacity)
            old.http.event(output, 'server_ready', pid=server_pid, capacity=capacity)
            if not capacity['matches_requested']:
                raise RuntimeError('effective capacity mismatch')
            results = []
            for case in step['cases']:
                check()
                batch = old.admission.run_cases(
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
            save(output / 'output-reference-check.json', dict(passed=True,
                reference='old 6a binary same-head full output hashes', arbitrary_input_equivalence=False))
            raw_log = (output / 'server.log').read_text()
            if '[layer-timing]' in raw_log or '[mtp-timing]' in raw_log:
                raise RuntimeError('legacy intrusive timing leaked into execution-observation stage')
            check()
            passed = True
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            if server is not None:
                old.http.event(output, 'server_before_shutdown', pid=server_pid,
                               launcher_pid=server.pid)
                try:
                    target_exit = stop_profile_target(local, output, server)
                except BaseException as error:
                    target_exit = getattr(error, 'actual_target_exit', None)
                    cleanup_errors.append(repr(error))
                old.http.event(output,
                    'server_after_shutdown' if not cleanup_errors else 'server_shutdown_failed',
                    pid=server_pid, server_returncode=target_exit['returncode'] if target_exit else None,
                    launcher_returncode=server.returncode, errors=list(cleanup_errors))
            finish_interactive(session, cleanup_errors)
            log.flush()
            execution = old.audit_execution(output, step, server_pid)
            actual_code = target_exit['returncode'] if target_exit else None
            clean = (passed and server is not None and server.returncode == 0 and
                     actual_code == 0 and not cleanup_errors and execution['complete'])
            save(output / 'exit.json', dict(server=actual_code,
                http_output_checks_passed=clean, failure=failure,
                cleanup_errors=cleanup_errors, execution_records_complete=execution['complete'],
                launcher=server.returncode if server else None, trace_exports_complete=False,
                diagnostic_only=True, performance_acceptance_sample=False,
                performance_acceptance=False,
                collection_lifecycle=plan['nsys']['interactive_contract']['mode']))
    if not clean:
        raise RuntimeError('interactive diagnostic group failed')
    export_profile(plan, output)
    result = json.loads((output / 'exit.json').read_text())
    result['trace_exports_complete'] = True
    save(output / 'exit.json', result)


def custom_step(plan, name, plan_path):
    if name == 'fixture-controls':
        return fixture_controls(plan, plan_path)
    gate = json.loads((Path(plan['output']) / 'fixture-controls/gate.json').read_text())
    controlled = 'interactive_contract' in plan['nsys']
    candidate = 'wrapper-interactive' if controlled else 'wrapper-external'
    if (gate.get('passed') is not True or gate.get('candidate') != candidate or
            gate.get('plan_sha256') != legacy.sha(plan_path)):
        raise RuntimeError('conditional model phase has no same-plan collector gate')
    if controlled:
        reused_path = Path(plan['output']) / 'fixture-controls/reused-controls.json'
        reused = json.loads(reused_path.read_text())
        if (gate.get('new_captures') != 2 or gate.get('reused_local_controls') != 2 or
                gate.get('reused_controls_sha256') != legacy.sha(reused_path) or
                not collector_gate(gate.get('results', []), reused)):
            raise RuntimeError('interactive collector evidence is incomplete or changed')
        return interactive_model_step(plan, name, plan_path)
    raise ValueError('model external collection requires the interactive lifecycle')


def main():
    legacy.clean_env = clean_env
    old.telemetry.sample = sample_with_failure_context
    legacy.stop_group = stop_group_with_final_flush
    legacy.require_monitor = require_monitor_with_pending
    old.validate = validate
    old.custom_step = custom_step
    old.stop_profile_target = stop_profile_target
    old.audit_profile_diagnostics = audit_profile_diagnostics
    old.export_profile = export_profile
    old.__file__ = str(Path(__file__).resolve())
    if '--fixture-wrapper' in sys.argv:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--plan', required=True, type=Path)
        parser.add_argument('--fixture-wrapper', required=True)
        args = parser.parse_args()
        plan = json.loads(args.plan.read_text())
        validate(plan)
        return fixture_wrapper(plan, args.fixture_wrapper)
    old.main()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
