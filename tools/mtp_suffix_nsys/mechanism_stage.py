"""One fixed parameter diagnosis under the repository GPU lease; no benchmark."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


runner = module('run_stage')
sync = runner.synchronous


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def rows(path, tag):
    if not path.exists():
        return []
    if path.stat().st_size > 64 * 1024 * 1024:
        raise RuntimeError('bounded probe log exceeded')
    result = []
    for line in path.read_text().splitlines(keepends=True):
        if line.startswith('[' + tag + '] ') and line.endswith('\n'):
            result.append(json.loads(line[len(tag) + 3:]))
    return result


def verify_lease(plan):
    path = Path(plan['lock_path'])
    with path.open('r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
            raise RuntimeError('full stage requires an existing exclusive guard lease')
        record = json.loads(lock.read())
    if record['pid'] != os.getppid() or record['state'] != 'held':
        raise RuntimeError('lease owner must be the direct parent guard')


def verify_files(plan):
    for path, expected in plan['files'].items():
        if sha(path) != expected:
            raise RuntimeError('frozen identity changed: ' + path)
    tool = plan['tool_dependency']
    manifest = json.loads(Path(tool['manifest']).read_text())
    for relative, row in manifest.items():
        if sha(Path(tool['package_root']) / relative) != row['sha256']:
            raise RuntimeError('pinned collector dependency changed: ' + relative)


def loaded_libraries(plan, pid):
    """Observe the held actual target's relevant mapped libraries, by content."""
    tool = plan['tool_dependency']
    package = Path(tool['package_root'])
    manifest = json.loads(Path(tool['manifest']).read_text())
    selected = {}
    for line in Path(f'/proc/{pid}/maps').read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or not fields[5].startswith('/'):
            continue
        path = Path(fields[5])
        if not path.name.startswith(('libcuda', 'libcublas', 'libcupti')):
            continue
        path = path.resolve()
        expected = plan['files'].get(str(path))
        if path.is_relative_to(package):
            expected = manifest[str(path.relative_to(package))]['sha256']
        digest = sha(path)
        if expected != digest:
            raise RuntimeError('actual mapped CUDA dependency was not frozen: '+str(path))
        selected[str(path)] = digest
    if not any(Path(p).name.startswith('libcublasLt') for p in selected):
        raise RuntimeError('production cuBLASLt mapping missing from held target')
    return selected


def reused_cupti_result(plan):
    """Use one independently reanalyzed immutable capture, never sample again."""
    reused = plan['reused_cupti']
    for field in ('raw', 'analysis', 'proof'):
        path = reused[field]
        if sha(path) != reused[field + '_sha256'] or plan['files'].get(path) != sha(path):
            raise ValueError('reused CUPTI ' + field + ' identity differs')
    proof = json.loads(Path(reused['proof']).read_text())
    result = json.loads(Path(reused['analysis']).read_text())
    analyzer = str(Path(__file__).with_name('mechanism_analyze.py'))
    if (proof.get('analysis_sha256') != reused['analysis_sha256'] or
            proof.get('raw_sha256') != reused['raw_sha256'] or
            proof.get('analyzer_sha256') != sha(analyzer) or
            proof.get('binary_sha256') != plan['binary_sha256'] or
            proof.get('runs') != 1 or proof.get('elapsed_seconds', 61) > 60):
        raise ValueError('reused CUPTI analysis provenance differs')
    if (result.get('status') != 'VALID_AND_REPRODUCED' or
            result.get('eligible_nsys') is not True or
            result.get('local_mechanism_valid') is not True or result.get('errors') or
            result.get('source', {}).get('sha256') != reused['raw_sha256'] or
            result.get('source', {}).get('path') != reused['raw'] or
            result.get('model_qualification') is not False or
            result.get('performance_acceptance') is not False):
        raise ValueError('reused CUPTI capture is not a valid local mechanism')
    return result


def capture(plan, mode, output, whole_deadline):
    output.mkdir()
    log = output / 'server.log'
    deadline = min(whole_deadline, time.monotonic() + (120 if mode == 'cupti' else 300))
    child = control = identity = fd = None
    errors = []
    forced = False
    ready = None
    env = runner.clean_env()
    # No external preloads or diagnostic ablations may affect this fixed probe.
    env = {k: v for k, v in env.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    command = [plan['binary'], '--mode', mode, '--release', str(output / 'release')]
    save(output / 'probe-command.json', command)
    config = output / 'nvlog.config'
    if mode == 'nsys':
        config.write_text(plan['nvlog_config'].format(log=str(output / 'nvlog.log')))
        env['NVLOG_CONFIG_FILE'] = str(config)

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('fixed probe collection deadline exceeded')
        for path in (log, output / 'nvlog.log'):
            if path.exists() and path.stat().st_size > 64 * 1024 * 1024:
                raise RuntimeError('bounded log capacity exceeded')

    try:
        with log.open('x') as stream:
            if mode == 'nsys':
                control = sync.Session(plan['tool_dependency']['path'], output,
                    sync.make_name('fixed-memset'), cwd=ROOT, env=env,
                    deadline=deadline, check=check)
                control.start()
                child = control.launch(command, stream,
                    envvars={'NVLOG_CONFIG_FILE': str(config)})
            else:
                child = subprocess.Popen(command, cwd=ROOT, env=env,
                    stdout=stream, stderr=subprocess.STDOUT)
            save(output / 'launcher.json', sync.manual.proc_identity(child.pid))
            while True:
                check()
                if rows(log, 'mechanism-error'):
                    raise RuntimeError('probe reported an error')
                configs = rows(log, 'mechanism-config')
                if len(configs) > 1:
                    raise RuntimeError('duplicate probe config')
                if configs and identity is None:
                    identity, fd = runner.pin_fixture(
                        {'fixture_binary': plan['binary']}, output, child.pid,
                        configs[0]['pid'], False, session=control)
                    if mode == 'cupti' and identity['pid'] != child.pid:
                        raise RuntimeError('direct CUPTI child identity differs')
                if fd is not None and runner.pidfd_exited(fd, 0):
                    raise RuntimeError('actual target exited before successful receipt')
                ready_rows = rows(log, 'mechanism-ready')
                if len(ready_rows) > 1:
                    raise RuntimeError('duplicate probe ready')
                if ready_rows:
                    if identity is None:
                        raise RuntimeError('ready lacks prior owned identity')
                    ready = ready_rows[0]
                    receipt = dict(pid=identity['pid'], start_ticks=identity['start_ticks'],
                        profiler_stop_error=ready.get('profiler_stop_error'),
                        source='mechanism-probe', raw=ready)
                    sync._target_receipt(receipt)
                    save(output / 'ready-boundary.json', receipt)
                    save(output / 'loaded-libraries.json', loaded_libraries(plan, identity['pid']))
                    break
                code = child.poll()
                if code is not None and (mode == 'cupti' or code != 0):
                    raise RuntimeError('launcher exited before successful receipt')
                time.sleep(.05)
            if control is not None:
                control.await_report(receipt, check=check)
                control.shutdown()
            (output / 'release').touch(exist_ok=False)
            while child.poll() is None or not runner.pidfd_exited(fd, 0):
                check()
                time.sleep(.05)
            exits = rows(log, 'mechanism-exit')
            if (child.returncode != 0 or len(exits) != 1 or
                    exits[0].get('pid') != identity['pid'] or
                    exits[0].get('released') is not True or exits[0].get('returncode') != 0):
                raise RuntimeError('target and launcher must finish normally')
        if control is not None:
            runner.finish_interactive(control, errors)
            if errors:
                raise runner.legacy.EnvironmentAbort('session cleanup failed')
        if mode == 'nsys':
            export_plan = dict(nsys=dict(path=plan['tool_dependency']['path'],
                interactive_contract=sync.contract(), export_timeout_seconds=120))
            runner.export_database(export_plan, output, deadline, check)
            snapshot = module('nvlog_snapshot')
            saved = snapshot.snapshot(output/'nvlog.log', output/'nvlog-snapshot.log')
            save(output/'nvlog-snapshot.json', saved)
        return dict(mode=mode, pid=identity['pid'], ready=ready,
                    normal_target_exit=True, launcher_returncode=child.returncode)
    finally:
        def cleanup(label, operation):
            try:
                return operation()
            except BaseException as error:
                errors.append(label + ': ' + repr(error))
                return None

        live_target = False
        if fd is not None:
            observed = cleanup('target exit readiness', lambda: runner.pidfd_exited(fd, 0))
            live_target = observed is not True
        live_child = child is not None and cleanup('launcher poll', child.poll) is None
        if live_target or live_child:
            forced = True
            # Only a held pidfd or the exact Popen child may receive a signal.
            def signal_target(sig):
                try:
                    signal.pidfd_send_signal(fd, sig)
                except ProcessLookupError:
                    pass
            if live_target:
                cleanup('owned target TERM', lambda: signal_target(signal.SIGTERM))
            if live_child:
                cleanup('owned launcher TERM', child.terminate)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cleanup('owned launcher KILL', child.kill)
                    cleanup('owned launcher reap', lambda: child.wait(timeout=5))
                except BaseException as error:
                    errors.append('owned launcher wait: ' + repr(error))
            if live_target and cleanup('owned target TERM wait',
                    lambda: runner.pidfd_exited(fd, 5000)) is not True:
                cleanup('owned target KILL', lambda: signal_target(signal.SIGKILL))
                if cleanup('owned target KILL wait',
                           lambda: runner.pidfd_exited(fd, 5000)) is not True:
                    errors.append('owned actual target remained alive or exit unknown')
        if control is not None and not control._closed:
            cleanup('session final cleanup', lambda: runner.finish_interactive(control, errors))
        if fd is not None:
            cleanup('pidfd close', lambda: os.close(fd))
        try:
            save(output / 'cleanup.json', dict(forced=forced, errors=errors,
                status='ENV_ABORT' if forced or errors else 'NORMAL', identity=identity))
        except BaseException as error:
            raise runner.legacy.EnvironmentAbort('cleanup receipt could not be preserved') from error
        if forced or errors:
            raise runner.legacy.EnvironmentAbort('owned probe cleanup failed or forced')


def run(plan_path):
    plan = json.loads(plan_path.read_text())
    scope = json.loads(Path(plan['scope']).read_text())
    output = Path(plan['output'])
    output.mkdir()
    started = time.monotonic()
    state = dict(schema=1, plan_sha256=sha(plan_path), status='RUNNING',
        captures=[], new_model_loads=0, new_http=0, performance_acceptance=False,
        raw_manifest_scope='Driver-boundary observation; live NVLOG may append during daemon cleanup. Preserve this manifest and independently seal/reconcile after the outer guard returns.',
        errors=[], started_unix=time.time())
    deadline = min(started+600, started+scope['created_unix']+1800-time.time())
    def terminated(*_):
        raise runner.legacy.EnvironmentAbort('guard or external termination')
    signal.signal(signal.SIGTERM, terminated)
    signal.signal(signal.SIGINT, terminated)
    try:
        verify_lease(plan)
        verify_files(plan)
        if time.monotonic() >= deadline:
            raise TimeoutError('phase work budget expired before capture')
        analyze = module('mechanism_analyze')
        if 'reused_cupti' in plan:
            if scope.get('cupti_only_processes') != 0 or scope.get('build_attempts') != 0:
                raise ValueError('reuse scope must forbid new CUPTI capture and build')
            result = reused_cupti_result(plan)
            state['reused_cupti'] = plan['reused_cupti']
        else:
            state['captures'].append(capture(plan, 'cupti', output/'cupti', deadline))
            result = analyze.analyze_cupti(output/'cupti/server.log')
            save(output/'cupti/analysis.json', result)
        if result.get('eligible_nsys') is not True:
            state['status'] = result['status']
            return 1
        state['captures'].append(capture(plan, 'nsys', output/'nsys', deadline))
        comparison = analyze.analyze_nsys(output/'nsys/trace.sqlite',
                                         output/'nsys/server.log', result)
        save(output/'nsys/analysis.json', comparison)
        state['analysis_status'] = comparison['status']
        state['analysis_errors'] = comparison.get('errors', [])
        snapshot = module('nvlog_snapshot')
        receipt = json.loads((output/'nsys/nvlog-snapshot.json').read_text())
        save(output/'nsys/nvlog-reconciliation.json', snapshot.reconcile(
            receipt, output/'nsys/nvlog.log', state['captures'][-1]['pid']))
        verify_files(plan)
        if comparison['status'] == 'INVALID':
            state['status'] = 'INVALID_MECHANISM_EVIDENCE'
            return 1
        if comparison['status'] not in ('LOCAL_MECHANISM_OBSERVED', 'NOT_REPRODUCED'):
            raise ValueError('unexpected mechanism analysis status')
        state['status'] = 'BOUNDED_MECHANISM_COMPLETE'
        return 0
    except BaseException as error:
        state['status'] = 'ENV_ABORT' if isinstance(error, runner.legacy.EnvironmentAbort) else 'STOPPED'
        state['errors'].append(repr(error))
        return 1
    finally:
        for path in output.glob('*/cleanup.json'):
            if json.loads(path.read_text())['status'] == 'ENV_ABORT':
                state['status'] = 'ENV_ABORT'
        state['elapsed_seconds'] = time.monotonic()-started
        state['finished_unix'] = time.time()
        save(output/'state.json', state)
        save(output/'files.json', {str(p.relative_to(output)): sha(p)
             for p in sorted(output.rglob('*')) if p.is_file()})
        print(json.dumps(state, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.plan.resolve()))
