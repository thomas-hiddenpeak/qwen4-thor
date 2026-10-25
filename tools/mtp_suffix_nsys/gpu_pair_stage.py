"""Four bounded real-HTTP captures; recorded GPU comparison, not acceptance."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LOG_LIMIT = 256 * 1024 ** 2
SQLITE_LIMIT = 8 * 1024 ** 3
CANDIDATE_SHA = 'f0ff0fe8e11791d561bc49f4dc42762415d4ff000836bbd8c38548723906c914'


def module(name):
    spec = importlib.util.spec_from_file_location('pair_' + name, HERE / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


runner = module('run_stage')
old, legacy, sync = runner.old, runner.legacy, runner.synchronous
save = legacy.save


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate(plan):
    if tuple(plan.get(k) for k in ('http_requests', 'http_loads',
            'stage_timeout_seconds', 'direct_contract_timeout_seconds',
            'fixture_runs', 'analysis_per_capture_seconds',
            'source_log_limit_bytes', 'sqlite_limit_bytes')) != (
            6, 4, 4200, 0, 0, 120, LOG_LIMIT, SQLITE_LIMIT):
        raise ValueError('fixed four-capture budget changed')
    if any(plan[k] != 0 for k in ('retries', 'warmups', 'observation_build_limit',
                                  'runtime_optimization_candidates')):
        raise ValueError('extra work is outside this stage')
    if (plan['resource_sample_max_age_seconds'] != 5 or
            plan.get('monitor_fix', {}).get('exit_pending') != dict(
                ttl_seconds=5, capacity=16, poll_timeout_ms=0)):
        raise ValueError('fixed monitor contract changed')
    if (plan['nsys']['interactive_contract'] != sync.contract() or
            plan['nsys']['export_timeout_seconds'] != 120 or
            plan['candidate_sha256'] != CANDIDATE_SHA or
            plan['files'].get(plan['candidate']) != CANDIDATE_SHA or
            plan['tool_dependency']['path'] != plan['nsys']['path'] or
            plan['nsys']['path'] not in plan['files']):
        raise ValueError('unchanged synchronous/tool/runtime identity required')
    for key in ('candidate', 'model', 'artifact', 'output'):
        if not Path(plan[key]).is_absolute():
            raise ValueError('absolute path required: ' + key)
    references = plan.get('qualification_reuse', {})
    if not references or any(not Path(p).is_absolute() or plan['files'].get(p) != h
                             for p, h in references.items()):
        raise ValueError('reuse anchors must be explicitly frozen; no historical promotion')
    if not isinstance(plan.get('nvlog_config'), str) or '{log}' not in plan['nvlog_config']:
        raise ValueError('fixed per-capture NVLOG configuration required')
    steps = plan['steps']
    if len(steps) != 4:
        raise ValueError('exactly four model captures required')
    for step, (name, suffix, tokens, requests) in zip(steps, runner.MODEL_STEPS):
        if tuple(step.get(k) for k in ('name', 'head', 'suffix', 'measurement_role',
                'execution_diagnostic', 'profile_input_tokens', 'kind',
                'http_requests', 'http_loads', 'timeout')) != (
                name, 'subset', suffix, 'nsys_profile', True, tokens,
                'timing', requests, 1, 900):
            raise ValueError('capture order/configuration changed')
        expected = [('context-8192', 8170)]
        if requests == 2:
            expected.append(('context-204800', 204781))
        if [(c['id'], c['input_tokens']) for c in step['cases']] != expected:
            raise ValueError('fixed request/predecessor changed')
        for case in step['cases']:
            if (case['repeats'] != 1 or case['output_tokens'] != 256 or
                    case['role'] != 'development_diagnostic' or
                    not case['input_min'] <= case['input_tokens'] <= case['input_max'] or
                    not Path(case['path']).is_absolute() or case['path'] not in plan['files'] or
                    any(not re.fullmatch('[0-9a-f]{64}', case[key]) for key in
                        ('prompt_sha256', 'expected_output_sha256'))):
                raise ValueError('frozen HTTP contract changed')
        command = step['command']
        if (step.get('env') or len(command) != 7 or
                command[1:4] != ['-B', str(Path(__file__).resolve()), '--plan'] or
                not Path(command[4]).is_absolute() or
                command[5:] != ['--custom-step', name]):
            raise ValueError('capture must use this validated stage entry')


def clean_env():
    return {k: v for k, v in runner.clean_env().items()
            if not k.startswith(('CUPTI_', 'Q4T_')) and k != 'LD_PRELOAD'}


def check_files(output):
    for name, maximum in (('server.log', LOG_LIMIT), ('nvlog.log', LOG_LIMIT),
                          ('trace.sqlite', SQLITE_LIMIT), ('export.log', LOG_LIMIT),
                          ('analysis.log', LOG_LIMIT)):
        path = output / name
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise RuntimeError('bounded regular output required: ' + name)


def require_live(identity, fd):
    current = old.proc_identity(identity['pid'])
    if (current['start_ticks'] != identity['start_ticks'] or
            current['state'] in ('Z', 'X') or runner.pidfd_exited(fd, 0)):
        raise RuntimeError('bound actual target exited or changed')


def wait_ready(plan, output, session, launcher, check):
    """A normal Nsight launch-control exit is not the target/wrapper exit."""
    identity, fd = None, None
    try:
        while True:
            check()
            code = launcher.poll()
            if code is not None and code != 0:
                raise RuntimeError('Nsight launch control failed during startup')
            if identity is None and (output / 'target-identity.json').is_file():
                identity = runner.bind_model_target(plan, output, session)
                fd = os.pidfd_open(identity['pid'])
            if identity is not None:
                require_live(identity, fd)
            if 'serving on port' in (output / 'server.log').read_text():
                if identity is None:
                    raise RuntimeError('server readiness lacks owned target identity')
                return identity, fd
            time.sleep(.1)
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise


def loaded_libraries(plan, pid):
    tool = plan['tool_dependency']
    package = Path(tool['package_root']).resolve()
    manifest = json.loads(Path(tool['manifest']).read_text())
    selected = {}
    for line in Path(f'/proc/{pid}/maps').read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or not fields[5].startswith('/'):
            continue
        path = Path(fields[5]).resolve()
        if not path.name.startswith(('libcuda', 'libcublas', 'libcupti')):
            continue
        expected = plan['files'].get(str(path))
        if path.is_relative_to(package):
            expected = manifest[str(path.relative_to(package))]['sha256']
        digest = sha(path)
        if expected != digest:
            raise RuntimeError('unfrozen actual CUDA/Nsight mapping: ' + str(path))
        selected[str(path)] = digest
    if not all(any(Path(p).name.startswith(prefix) for p in selected)
               for prefix in ('libcuda.so', 'libcudart', 'libcublasLt')):
        raise RuntimeError('actual CUDA/cuBLAS mapping incomplete')
    return selected


def snapshot_logs(output, target_pid):
    # A fresh isolated module object; the historical module and its contract
    # remain unchanged. Only this stage's frozen byte bound is different.
    snapshot = module('nvlog_snapshot')
    snapshot.MAX_BYTES = LOG_LIMIT
    receipt = snapshot.snapshot(output / 'nvlog.log', output / 'nvlog-snapshot.log',
                                max_bytes=LOG_LIMIT)
    receipt['stage_adapter'] = dict(module_sha256=sha(HERE / 'nvlog_snapshot.py'),
        adapter_sha256=sha(Path(__file__)), max_bytes=LOG_LIMIT,
        historical_default_max_bytes=64 * 1024 ** 2, isolated_module=True)
    save(output / 'nvlog-snapshot.json', receipt)
    result = snapshot.reconcile(receipt, output / 'nvlog.log', target_pid,
                                max_bytes=LOG_LIMIT)
    result['stage_adapter'] = receipt['stage_adapter']
    save(output / 'nvlog-reconciliation.json', result)
    return receipt


def require_qualified(result):
    if (result.get('qualification_passed') is not True or result.get('errors') != [] or
            result.get('performance_acceptance') is not False):
        raise RuntimeError('capture lacks bounded recorded-GPU qualification')


def analyze_capture(plan, output, identity, deadline, check):
    command = [sys.executable, '-B', str(HERE / 'recorded_gpu.py'),
        '--output', str(output), '--target-pid', str(identity['pid']),
        '--wrapper-pid', str(identity['wrapper_pid']), '--scratch-dir', str(output / 'audit-scratch'),
        '--nvlog-path', str(output / 'nvlog.log'),
        '--nvlog-snapshot-receipt', str(output / 'nvlog-snapshot.json')]
    save(output / 'analysis-command.json', command)
    child, failure, forced = None, None, False
    cleanup_errors = []
    limit = min(deadline, time.monotonic() + plan['analysis_per_capture_seconds'])
    try:
        with (output / 'analysis.log').open('x') as stream:
            child = subprocess.Popen(command, cwd=ROOT, env=clean_env(), stdout=stream,
                                     stderr=subprocess.STDOUT)
            while child.poll() is None:
                check()
                if time.monotonic() >= limit:
                    raise TimeoutError('bounded capture analysis exceeded 120 seconds')
                time.sleep(.05)
            if child.returncode:
                raise RuntimeError('recorded GPU analysis rejected capture')
        result = json.loads((output / 'recorded-gpu-audit.json').read_text())
        require_qualified(result)
        if not (output / 'strict-audit.json').is_file():
            raise RuntimeError('original strict audit was not retained')
        return result
    except BaseException as error:
        failure = repr(error)
        raise
    finally:
        if child is not None and child.poll() is None:
            forced = True
            try:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            except BaseException as error:
                cleanup_errors.append(repr(error))
        save(output / 'analysis-exit.json', dict(returncode=child.returncode if child else None,
            failure=failure, forced=forced, cleanup_errors=cleanup_errors))
        if forced or cleanup_errors:
            raise legacy.EnvironmentAbort('owned analysis cleanup forced or failed')


def custom_step(plan, name, plan_path):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    output = Path(plan['output']) / name
    output.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic() + step['timeout']
    config = output / 'nvlog.config'
    config.write_text(plan['nvlog_config'].format(log=str(output / 'nvlog.log')))
    env = clean_env() | dict(Q4T_EXEC_DIAGNOSTIC='1', Q4T_EXEC_NVTX='1',
        Q4T_EXEC_PROFILE_INPUT=str(step['profile_input_tokens']), NVLOG_CONFIG_FILE=str(config))
    command = old.server_command(plan, step)
    wrapper = [sys.executable, '-B', str(Path(__file__).resolve()),
               '--plan', str(plan_path), '--target-wrapper', name]
    save(output / 'server-command.json', dict(argv=command,
        environment={k: v for k, v in env.items() if k.startswith(('Q4T_', 'NVLOG_'))},
        measurement_role='nsys_profile', diagnostic_only=True,
        performance_acceptance_sample=False, performance_acceptance=False))
    save(output / 'step.json', step)

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('frozen 900-second capture budget exhausted')
        check_files(output)

    session = sync.Session(plan['nsys']['path'], output, sync.make_name(name),
                           cwd=ROOT, env=env, deadline=deadline, check=check)
    local = dict(plan, _active_session=session.name, _interactive_session=session,
                 _profile_input_tokens=step['profile_input_tokens'])
    launcher, identity, fd, target_exit = None, None, None, None
    http_passed, failure, execution, clean = False, None, None, False
    cleanup_errors = []
    try:
        try:
            with (output / 'server.log').open('x') as stream:
                try:
                    old.http.event(output, 'server_before_start')
                    session.start()
                    launcher = session.launch(wrapper, stream, envvars={k: v for k, v in env.items()
                        if k.startswith(('Q4T_', 'NVLOG_'))})
                    old.http.event(output, 'profile_launcher_started', pid=launcher.pid)
                    identity, fd = wait_ready(plan, output, session, launcher, check)
                    old.http.event(output, 'server_started', pid=identity['pid'],
                        start_ticks=identity['start_ticks'], launcher_pid=launcher.pid)
                    capacity = old.http.capacity_evidence((output / 'server.log').read_text(), 208896)
                    save(output / 'capacity.json', capacity)
                    if not capacity['matches_requested']:
                        raise RuntimeError('effective capacity mismatch')
                    old.http.event(output, 'server_ready', pid=identity['pid'], capacity=capacity)
                    results = []
                    for case in step['cases']:
                        check(); require_live(identity, fd)
                        rows = old.admission.run_cases(plan, dict(step, cases=[case]),
                            output, launcher, clean_env(), capacity)
                        results.extend(rows)
                        save(output / 'results.json', results)
                        if (len(rows) != 1 or rows[0]['outputs'] != [case['expected_output_sha256']] or
                                len(rows[0]['responses']) != 1 or any(r['actual_output'] != 256 or
                                r['finish'] != ['length'] for r in rows[0]['responses'])):
                            raise RuntimeError('fixed HTTP output/length differs')
                    while True:
                        check(); require_live(identity, fd)
                        raw = (output / 'server.log').read_text()
                        ended = re.findall(r'^\[q4t\]\[request\] .*\bevent=end\b', raw, re.M)
                        if '[exec-capture] event=end ' in raw and len(ended) == step['http_requests']:
                            break
                        time.sleep(.05)
                    runner.capture_end_receipt(raw, identity['pid'], step['profile_input_tokens'])
                    if '[layer-timing]' in raw or '[mtp-timing]' in raw:
                        raise RuntimeError('legacy intrusive timing appeared')
                    save(output / 'loaded-libraries.json', loaded_libraries(plan, identity['pid']))
                    save(output / 'output-reference-check.json', dict(passed=True,
                        arbitrary_input_equivalence=False, performance_acceptance=False))
                    http_passed = True
                finally:
                    if launcher is not None:
                        try:
                            target_exit = runner.stop_profile_target(local, output, launcher)
                        except BaseException as error:
                            target_exit = getattr(error, 'actual_target_exit', None)
                            cleanup_errors.append(repr(error))
                    try:
                        runner.finish_interactive(session, cleanup_errors)
                    except BaseException as error:
                        cleanup_errors.append('session final cleanup: ' + repr(error))
                    if fd is not None:
                        try:
                            if not runner.pidfd_exited(fd, 0):
                                cleanup_errors.append('actual target exit not confirmed by held pidfd')
                        except BaseException as error:
                            cleanup_errors.append('actual target exit observation: ' + repr(error))
                        finally:
                            try:
                                os.close(fd)
                            except BaseException as error:
                                cleanup_errors.append('owned pidfd close: ' + repr(error))
                    try:
                        stream.flush()
                    except BaseException as error:
                        cleanup_errors.append('target log flush: ' + repr(error))
            execution = old.audit_execution(output, step, identity['pid'] if identity else None)
            actual = target_exit['returncode'] if target_exit else None
            shutdown = output / 'profile-shutdown.json'
            if shutdown.exists() and json.loads(shutdown.read_text()).get('forced_target_kill'):
                cleanup_errors.append('actual target required forced kill')
            clean = (http_passed and actual == 0 and launcher is not None and
                     launcher.returncode == 0 and not cleanup_errors and execution['complete'])
            if not clean:
                raise RuntimeError('HTTP, independent target exit or CPU capture incomplete')
        finally:
            save(output / 'exit.json', dict(server=target_exit['returncode'] if target_exit else None,
                launcher=launcher.returncode if launcher else None,
                http_output_checks_passed=clean, cleanup_errors=cleanup_errors,
                execution_records_complete=bool(execution and execution['complete']),
                trace_exports_complete=False, diagnostic_only=True,
                performance_acceptance_sample=False, performance_acceptance=False,
                collection_lifecycle=sync.contract()['mode']))
            if cleanup_errors:
                raise legacy.EnvironmentAbort('owned model/session cleanup failed')
        work = module('pair_work_contract').validate_work(output, step, identity['pid'])
        save(output / 'work-contract.json', work)
        if work.get('status') != 'VALID' or work.get('errors'):
            raise RuntimeError('fixed target-work contract failed')
        check()
        runner.export_database(plan, output, deadline, check)
        check()
        snapshot_logs(output, identity['pid'])
        analyze_capture(plan, output, identity, deadline, check)
        check()
        result = json.loads((output / 'exit.json').read_text())
        result.update(trace_exports_complete=True, recorded_gpu_qualification=True,
                      full_trace_integrity=False)
        save(output / 'exit.json', result)
    except BaseException as error:
        failure = repr(error)
        exit_path = output / 'exit.json'
        if exit_path.exists():
            result = json.loads(exit_path.read_text())
            result['failure'] = failure
            export = output / 'export-exit.json'
            forced_export = export.exists() and json.loads(export.read_text()).get('returncode', 0) in (
                -signal.SIGTERM, -signal.SIGKILL)
            if isinstance(error, legacy.EnvironmentAbort) or forced_export:
                result['cleanup_errors'] = list(result.get('cleanup_errors', [])) + [failure]
            save(exit_path, result)
            if forced_export:
                raise legacy.EnvironmentAbort('owned export cleanup forced') from error
        raise


def install():
    legacy.clean_env = clean_env
    old.telemetry.sample = runner.sample_with_failure_context
    legacy.stop_group = runner.stop_group_with_final_flush
    legacy.require_monitor = runner.require_monitor_with_pending
    old.validate, old.custom_step = validate, custom_step
    old.admission.audit_server = module('pair_work_contract').audit_server
    old.__file__ = str(Path(__file__).resolve())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--custom-step')
    parser.add_argument('--target-wrapper')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    validate(plan)
    if args.custom_step and args.target_wrapper:
        parser.error('choose exactly one child mode')
    for step in plan['steps']:
        if Path(step['command'][4]).resolve() != args.plan.resolve():
            raise ValueError('step points at a different frozen plan')
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('owned GPU-pair stage interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    install()
    old.main()


if __name__ == '__main__':
    main()
