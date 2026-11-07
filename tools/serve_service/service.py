"""Prepare, run, inspect and stop one explicit managed serving generation."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_service import package as pkg
from tools.serve_service import supervisor
from tools.serve_service import inspection
import gpu_exclusive as guard


def verify_worker(output):
    controller = pkg.read(output/'controller.json')
    owner = controller['owner']
    pkg.require(os.getppid() == owner['pid'] and
                supervisor.process_identity(owner['pid']) == owner,
                'worker must be a live lease controller child')
    supervisor.arm_parent_death(owner)
    manifest = pkg.verify(controller['package'], controller['manifest_sha256'])
    guard.ROOT = Path(manifest['repository'])
    lock_path = Path(controller['lock_path'])
    pkg.require(lock_path == guard.common_lock(), 'wrong repository lease')
    lease_fd = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    with guard.borrowed_lock(lease_fd, lock_path):
        pass  # Validate ownership without unlocking the inherited description.
    lease = pkg.read(lock_path)
    pkg.require(lease['pid'] == owner['pid'] and lease['state'] == 'held' and
                lease['output'] == str(output/'lease'), 'worker lease mismatch')
    with lock_path.open('r+') as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise RuntimeError('worker lacks exclusive parent lease')
    return controller


def run(package, digest, output):
    manifest = pkg.verify(package, digest)
    repository = Path(manifest['repository'])
    output = pkg.private_directory(output, repository)
    guard.ROOT = repository
    lock_path = guard.common_lock()
    pkg.save(output/'controller.json', dict(version=2,
        owner=supervisor.process_identity(os.getpid()), lock_path=str(lock_path),
        observation_context=inspection.current_context(),
        package=str(Path(package).resolve()), manifest_sha256=digest,
        created_unix=time.time()))
    command = [manifest['python']['path'], '-I', '-B',
        str(Path(package)/'source/tools/serve_service/service.py'),
        '_worker', '--run', str(output)]
    previous = {}
    def interrupt(_sig, _frame):
        if not (output/'stop.request.json').exists():
            pkg.save(output/'stop.request.json', dict(reason='controller_signal',
                                                    unix_seconds=time.time()))
        raise KeyboardInterrupt('managed service stopped')
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, interrupt)
    try:
        options = {}
        if manifest['settings'].get('nvml_policy') == 'degrade':
            from tools.serve_service import telemetry, monitor_control
            options['managed_monitor'] = telemetry.Monitor(output/'lease',
                resume_path=output/monitor_control.REQUEST_NAME,
                stopping=lambda: (output/'stop.request.json').exists())
        rc = guard.run_guard(command, output/'lease', lock_path,
                             log_max_bytes=4*1024**2, inherit_lease=True,
                             **options)
        result = pkg.read(output/'lease/exit.json')
        worker = (pkg.read(output/'status.json')
                  if (output/'status.json').exists() else None)
        manual = (output/'stop.request.json').exists()
        # The generic guard calls an operator interruption ENV_ABORT. Preserve
        # that receipt; the service terminal additionally describes intent.
        clean = (worker is not None and worker.get('cleanup_confirmed') is True
                 and 'cleanup_error' not in result and
                 'monitor_cleanup_error' not in result)
        resource_cleanup = None
        if 'host_resources' in manifest:
            from tools.serve_service import resources
            try:
                resource_cleanup = resources.departed(output)
                clean = clean and resource_cleanup.get('scope_removed', False)
            except Exception as error:
                resource_cleanup = dict(enabled=True, scope_removed=False, error=str(error))
                clean = False
        stopped = normal_stop(manual, clean and worker['state'] == 'STOPPED',
                              result)
        pkg.save(output/'terminal.json', dict(version=1,
            state='STOPPED' if stopped else 'FAILED', manual_stop=manual,
            cleanup_confirmed=clean, guard=result, worker=worker,
            automatic_reactivation=False, finished_unix=time.time(),
            **({'host_resource_cleanup': resource_cleanup} if resource_cleanup is not None else {})))
        return 0 if stopped else rc or 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def normal_stop(manual, clean, guard_result):
    if not manual or not clean:
        return False
    return (guard_result['status'] == 'COMPLETED' or
            (guard_result['status'] == 'COMPLETED_WITH_MONITOR_GAPS' and
             guard_result.get('monitor_policy') in
             ('nvml-service-degraded-v1', 'nvml-service-degraded-v2') and
             guard_result.get('child_returncode') == 0) or
            (guard_result['status'] == 'ENV_ABORT' and
             guard_result.get('reason') ==
             "KeyboardInterrupt('managed service stopped')" and
             guard_result.get('child_returncode') == 0))


def stop(output):
    output = Path(output).resolve(strict=True)
    control = pkg.read(output/'controller.json')
    if (output/'terminal.json').exists():
        return status(output)
    # Persist intent before signalling. A dead controller never authorizes
    # signalling its former PID or clearing a terminal record.
    if not (output/'stop.request.json').exists():
        pkg.save(output/'stop.request.json', dict(reason='explicit_stop',
                                                unix_seconds=time.time()))
    live = supervisor.signal_owner(control['owner'], 0)
    sent = False
    if live and (output/'status.json').exists():
        worker = pkg.read(output/'status.json')['owner']
        try:
            fields = Path(f"/proc/{worker['pid']}/stat").read_text().rsplit(')', 1)[1].split()
            if int(fields[1]) == control['owner']['pid']:
                sent = supervisor.signal_owner(worker, signal.SIGTERM)
        except (FileNotFoundError, ProcessLookupError):
            pass
    # Before the worker exists the durable intent is enough: it checks before
    # any launch. Leave the guard alive to drain and report COMPLETED normally.
    return dict(state='STOP_REQUESTED' if live else 'OWNER_ABSENT',
                signal_sent=sent, cleanup_confirmed=False)


def status(output):
    output = Path(output).resolve(strict=True)
    if (output/'terminal.json').exists():
        return pkg.read(output/'terminal.json')
    control = pkg.read(output/'controller.json')
    try:
        live = supervisor.process_identity(control['owner']['pid']) == control['owner']
    except (FileNotFoundError, ProcessLookupError):
        live = False
    worker = (pkg.read(output/'status.json')
              if (output/'status.json').exists() else None)
    result = dict(state=worker['state'] if live and worker else
                'STARTING' if live else 'OWNER_ABSENT', controller_live=live,
                worker=worker, cleanup_confirmed=False)
    monitor = output/'lease/compute-monitor.json'
    if monitor.exists():
        result['gpu_monitor'] = pkg.read(monitor)
    return result


def unit_text(package, digest, output):
    manifest = pkg.verify(package, digest)
    def quote(value):
        text = str(value)
        pkg.require(not any(c in text for c in '\n\r\x00'), 'invalid unit path')
        return '"'+text.replace('\\', '\\\\').replace('"', '\\"').replace(
            '%', '%%').replace('$', '$$')+'"'
    args = [manifest['python']['path'], '-I', '-B',
        str(Path(package)/'source/tools/serve_service/service.py'), 'run',
        '--package', str(Path(package).resolve()), '--manifest-sha256', digest,
        '--output', str(Path(output).absolute())]
    return ('[Unit]\nDescription=Qwen4 Thor explicit managed serving generation\n'
        '[Service]\nType=exec\nExecStart='+ ' '.join(map(quote, args))+'\n'
        'Restart=no\nKillMode=control-group\nTimeoutStopSec=90\n'
        'NoNewPrivileges=yes\nUMask=0077\nLimitCORE=0\n'
        'UnsetEnvironment=LD_PRELOAD PYTHONPATH PYTHONHOME\n'
        'StandardOutput=journal\nStandardError=journal\n'
        '# Deliberately no Install section or automatic restart.\n')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--binary', type=Path, default=ROOT/'build/q4t')
    p.add_argument('--release', type=Path, default=ROOT/'build/q4t.release.json')
    p.add_argument('--model-dir', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=8080)
    p.add_argument('--operations-port', type=int, default=0)
    p.add_argument('--restart-limit', type=int, default=0)
    p.add_argument('--nvml-policy', choices=('strict', 'degrade'), default='strict',
                   help='managed runtime only; preflight remains strict')
    p.add_argument('--critical-memory-mib', type=int, default=1024)
    p.add_argument('--client-policy', type=Path)
    p.add_argument('--host-memory-high-mib', type=int)
    p.add_argument('--host-memory-max-mib', type=int)
    p.add_argument('--host-tasks-max', type=int)
    for action in ('run', 'unit'):
        p = sub.add_parser(action)
        p.add_argument('--package', type=Path, required=True)
        p.add_argument('--manifest-sha256', required=True)
        p.add_argument('--output', type=Path, required=True)
    for action in ('stop', 'status', 'inspect', 'resume-monitor', '_worker'):
        p = sub.add_parser(action)
        p.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        selected = None
        values = (args.host_memory_high_mib, args.host_memory_max_mib, args.host_tasks_max)
        if any(v is not None for v in values):
            from tools.serve_service import resources
            selected = resources.policy(*values)
        result = pkg.prepare(args.binary, args.release, args.model_dir,
                             args.output, args.port, args.restart_limit,
                             args.critical_memory_mib, args.client_policy, selected,
                             args.operations_port, args.nvml_policy)
    elif args.action == 'run':
        manifest = pkg.verify(args.package, args.manifest_sha256)
        entry = args.package.resolve()/'source/tools/serve_service/service.py'
        if Path(__file__).resolve() != entry:
            environment = {k: v for k, v in os.environ.items()
                           if k not in ('LD_PRELOAD', 'PYTHONPATH', 'PYTHONHOME')
                           and not k.startswith('Q4T_')}
            command = [manifest['python']['path'], '-I', '-B', str(entry),
                'run', '--package', str(args.package.resolve()),
                '--manifest-sha256', args.manifest_sha256,
                '--output', str(args.output.absolute())]
            os.execve(command[0], command, environment)
        return run(args.package.resolve(), args.manifest_sha256, args.output)
    elif args.action == '_worker':
        control = verify_worker(args.run)
        return supervisor.run(control['package'], control['manifest_sha256'],
                              args.run)
    elif args.action == 'unit':
        print(unit_text(args.package, args.manifest_sha256, args.output), end='')
        return 0
    elif args.action == 'inspect':
        result = inspection.inspect(args.run)
        print(json.dumps(result))
        return 0 if result['complete'] else 2
    elif args.action == 'resume-monitor':
        from tools.serve_service import monitor_control
        result = monitor_control.request(args.run)
    else:
        result = (stop if args.action == 'stop' else status)(args.run)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'managed service rejected: {error}', file=sys.stderr)
        raise SystemExit(1)
