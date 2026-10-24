"""Bounded known-work controls, then conditional external-stop model profiles."""
import argparse
import copy
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

VARIANTS = [dict(name='direct-stop', wrapper=False, external=False),
            dict(name='wrapper-stop', wrapper=True, external=False),
            dict(name='direct-external', wrapper=False, external=True),
            dict(name='wrapper-external', wrapper=True, external=True)]
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
_original_custom = old.custom_step
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
    if ((plan['http_requests'], plan['http_loads'], plan['stage_timeout_seconds'],
         plan['direct_contract_timeout_seconds']) != (6, 4, 5400, 1200) or
            plan['fixture_variants'] != VARIANTS or
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
                1200, False, []):
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
    return 'q4t-suffix-' + str(os.getpid()) + '-' + name


def session_stop(plan, output, name, *, timeout=45, check=None):
    """Stop exactly our named session; command success is not target exit."""
    if not re.fullmatch(r'q4t-suffix-[0-9]+-[a-z0-9-]+', name):
        raise ValueError('refusing non-owned or malformed session name')
    command = [plan['nsys']['path'], 'stop', '--session=' + name]
    save(output / 'session-stop-command.json', command)
    child, failure = None, None
    started = time.monotonic()
    try:
        with (output / 'session-stop.log').open('x') as stream:
            child = subprocess.Popen(command, cwd=ROOT, env=clean_env(),
                                     stdout=stream, stderr=subprocess.STDOUT)
            while child.poll() is None:
                if check:
                    check()
                if time.monotonic() - started > timeout:
                    raise TimeoutError('owned Nsight session stop timed out')
                time.sleep(.05)
            if child.returncode != 0:
                raise RuntimeError('owned Nsight session stop failed')
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
        save(output / 'session-stop-exit.json', dict(
            session=name, returncode=child.returncode if child else None,
            failure=failure, started_monotonic=started, finished_monotonic=time.monotonic(),
            scope='Control command only; target exit and trace integrity audited separately.'))


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
            if launcher.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('actual-target capture end did not arrive before stop')
            time.sleep(.05)
        save(output / 'external-stop-boundary.json', dict(
            receipt=receipt, observed_monotonic=time.monotonic(),
            session=plan['_active_session'], target_pid=identity['pid']))
        session_stop(plan, output, plan['_active_session'], timeout=90)
    except BaseException as error:
        control_error = error
    finally:
        # Always retain the original pidfd-only target cleanup, including when
        # receipt/stop failed. Never replace q4t returncode with launcher zero.
        target = _original_stop_target(plan, output, launcher)
    if control_error is not None:
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


def pin_fixture(plan, output, launcher_pid, receipt_pid, wrapper):
    identity = old.proc_identity(receipt_pid)
    if identity['state'] in ('Z', 'X') or Path(f'/proc/{receipt_pid}/exe').resolve() != Path(plan['fixture_binary']).resolve():
        raise RuntimeError('fixture executable/liveness mismatch')
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
        identity.update(wrapper_pid=declared['wrapper_pid'])
    else:
        identity.update(wrapper_pid=None)
    fd = os.pidfd_open(receipt_pid)
    # Recheck after acquiring pidfd; a recycled PID must never receive a signal.
    try:
        if old.proc_identity(receipt_pid)['start_ticks'] != identity['start_ticks']:
            raise RuntimeError('fixture identity changed while pinning')
        old.save_atomic(output / 'fixture-owned-identity.json', identity)
    except BaseException:
        os.close(fd)
        raise
    return identity, fd


def fixture_capture(plan, variant, plan_path, parent_output, group_deadline):
    name = variant['name']
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
    command = [plan['nsys']['path'], 'profile', *options,
               '--session-new=' + session, '--env-var=NVLOG_CONFIG_FILE=' + str(config),
               '--output=' + str(output / 'trace'), *target]
    save(output / 'profile-command.json', command)
    save(output / 'variant.json', dict(variant, session=session, config_sha256=legacy.sha(config)))
    launcher, fd, identity, failure = None, None, None, None
    cleanup_errors = []
    clean, result, forced = False, None, False

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('fixed fixture budget expired: ' + name)
        paths = list(parent_output.glob('*/nvlog.log'))
        sizes = {str(p): p.stat().st_size for p in paths}
        if any(v > plan['fixture_nvlog_limit_bytes'] for v in sizes.values()) or sum(sizes.values()) > 4 * plan['fixture_nvlog_limit_bytes']:
            raise RuntimeError('frozen NVLOG size budget exceeded')

    try:
        with (output / 'server.log').open('x') as stream:
            launcher = subprocess.Popen(command, cwd=ROOT, env=env,
                stdout=stream, stderr=subprocess.STDOUT)
            save(output / 'launcher-identity.json', old.proc_identity(launcher.pid))
            while True:
                check()
                if launcher.poll() is not None:
                    raise RuntimeError('fixture launcher exited before ready')
                if fixture_rows(output / 'server.log', 'fixture-error'):
                    raise RuntimeError('fixture error before ready')
                configs = fixture_rows(output / 'server.log', 'fixture-config')
                if len(configs) > 1:
                    raise RuntimeError('duplicate fixture identity receipt')
                if configs and identity is None and (not variant['wrapper'] or
                        (output / 'fixture-target-identity.json').is_file()):
                    identity, fd = pin_fixture(plan, output, launcher.pid,
                                               configs[0]['pid'], variant['wrapper'])
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
                session_stop(plan, output, session, timeout=min(45, deadline-time.monotonic()), check=check)
            # No CUDA cleanup until the explicit session stop has completed.
            (output / 'release').touch(exist_ok=False)
            while launcher.poll() is None:
                check()
                time.sleep(.05)
            if not pidfd_exited(fd, 0):
                raise RuntimeError('fixture remains live after launcher exit')
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
                    owned = {launcher.pid}
                    for _ in range(64):
                        expanded = owned | {pid for pid, row in records.items()
                                            if row.get('ppid') in owned}
                        if expanded == owned:
                            break
                        owned = expanded
                    for pid in owned - {launcher.pid}:
                        try:
                            if Path(f'/proc/{pid}/exe').resolve() == Path(plan['fixture_binary']).resolve():
                                identity, fd = pin_fixture(plan, output, launcher.pid, pid, variant['wrapper'])
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
        save(output / 'exit.json', dict(complete=clean, failure=failure,
            launcher=launcher.returncode if launcher else None, cleanup_errors=cleanup_errors,
            forced_termination=forced,
            started_monotonic=started, finished_monotonic=time.monotonic(),
            performance_acceptance=False, actual_fixture_pid=identity['pid'] if identity else None))
        if cleanup_errors:
            raise legacy.EnvironmentAbort('fixture owned cleanup failed: ' + repr(cleanup_errors))


def collector_gate(results):
    if [row['name'] for row in results] != [v['name'] for v in VARIANTS]:
        return False
    chosen = results[-1]
    return (all(r['known_work_complete'] and r['negative_controls_passed'] for r in results)
            and chosen['diagnostics_valid'] and chosen['terminal_evidence_present']
            and chosen['gpu_memory_coverage_complete'])


def fixture_controls(plan, plan_path):
    legacy.require_guard()
    output = Path(plan['output']) / 'fixture-controls'
    output.mkdir()
    results, failure = [], None
    started = time.monotonic()
    try:
        for variant in plan['fixture_variants']:
            results.append(fixture_capture(plan, variant, plan_path, output, started + 1200))
            save(output / 'results.json', results)
        passed = collector_gate(results)
        save(output / 'gate.json', dict(passed=passed, candidate='wrapper-external',
            plan_sha256=legacy.sha(plan_path), results=results,
            scope='Known work and selected target diagnostics only; every model trace reaudited.'))
        if not passed:
            raise RuntimeError('fixed wrapper-external collector gate did not clear')
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
            completed_variants=len(results), cleanup_errors=child_cleanup,
            started_monotonic=started, finished_monotonic=time.monotonic(),
            measurement_role='collector_contract', performance_acceptance=False))


def custom_step(plan, name, plan_path):
    if name == 'fixture-controls':
        return fixture_controls(plan, plan_path)
    gate = json.loads((Path(plan['output']) / 'fixture-controls/gate.json').read_text())
    if (gate.get('passed') is not True or gate.get('candidate') != 'wrapper-external' or
            gate.get('plan_sha256') != legacy.sha(plan_path)):
        raise RuntimeError('conditional model phase has no same-plan collector gate')
    local = copy.deepcopy(plan)
    local['_active_session'] = session_name(name)
    local['nsys']['profile_options'].append('--session-new=' + local['_active_session'])
    return _original_custom(local, name, plan_path)


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
