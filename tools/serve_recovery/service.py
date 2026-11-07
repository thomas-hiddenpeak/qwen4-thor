"""Unified recovery service entrypoint; no systemd, shell loop or auto install."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_gateway import gateway as ingress
from tools.serve_recovery import bundle, child, control, journal, ledger, store
from tools.serve_service import package as engine


DIAGNOSTIC_BYTES = 64 * 1024
DIAGNOSTIC_SEALS = fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL


def diagnostic_buffer():
    """Private, fixed-capacity RAM for fatal control-tree diagnostics.

    Healthy control loops emit structured IPC state, not stdio logging. Model
    output uses observation_io separately. A full buffer rejects further fatal
    traceback writes without growing RAM or touching a filesystem sink.
    """
    fd = os.memfd_create('q4t-service-diagnostics', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.ftruncate(fd, DIAGNOSTIC_BYTES)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, DIAGNOSTIC_SEALS)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_diagnostics(status):
    """Explicit private CLI only; never expose tracebacks on public health."""
    pid, start = status['guardian_pid'], status['guardian_start_ticks']
    ledger.require(journal.process_start(pid) == start, 'stale diagnostic owner')
    path = Path(f'/proc/{pid}/fd/2')
    ledger.require(os.readlink(path) == '/memfd:q4t-service-diagnostics (deleted)',
                   'diagnostic buffer unavailable for this service version')
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        ledger.require(os.fstat(fd).st_size == DIAGNOSTIC_BYTES and
            fcntl.fcntl(fd, fcntl.F_GET_SEALS) == DIAGNOSTIC_SEALS and
            journal.process_start(pid) == start, 'invalid diagnostic buffer')
        raw = os.pread(fd, DIAGNOSTIC_BYTES, 0).rstrip(b'\0')
        return dict(retained_bytes=len(raw), capacity=DIAGNOSTIC_BYTES,
            possibly_truncated=len(raw) == DIAGNOSTIC_BYTES,
            text=raw.decode('utf-8', errors='replace'), persistence='guardian_lifetime')
    finally:
        os.close(fd)


def private_directory(path, repository):
    path = Path(path).absolute()
    ledger.require(path.resolve() == path and any(path.is_relative_to(Path(repository)/name)
        for name in ('build', '.q4t-work')), 'state outside repository work/build')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    return path


def initialize(package, digest, state):
    manifest = bundle.verify(package, digest)
    state = private_directory(state, manifest['repository'])
    service_id = str(uuid.uuid4())
    with store.Store(state, service_id, initialize=True):
        pass
    descriptor = dict(version=1, service_id=service_id,
        package=str(Path(package).resolve()), manifest_sha256=digest)
    engine.save(state/'service.json', descriptor)
    (state/'runs').mkdir(mode=0o700)
    return dict(state=str(state), service_id=service_id, initialized=True,
                desired='STOPPED', automatic_start=False)


def descriptor(state, package, digest):
    state = Path(state).absolute()
    info = state.lstat()
    ledger.require(state.resolve() == state and stat.S_ISDIR(info.st_mode) and
        info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
        'private canonical service state required')
    value = ingress.parse(ingress.read_file(state/'service.json', private=True))
    ledger.keys(value, ('version', 'service_id', 'package', 'manifest_sha256'))
    ledger.require(type(value['version']) is int and value['version'] == 1 and
        ledger.identifier(value['service_id']) and
        value['package'] == str(Path(package).resolve()) and value['manifest_sha256'] == digest,
        'service identity or deployment changed')
    return value


def acquire_lease(path):
    borrowed = os.environ.get('Q4T_MANAGED_LEASE_FD')
    if borrowed is not None:
        fd = os.dup(int(borrowed))
        try:
            child.verify_lease(fd, path)
        except BaseException:
            os.close(fd)
            raise
        return fd
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        ledger.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                       and info.st_nlink == 1, 'invalid shared lease file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child.verify_lease(fd, path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def launch(package, digest, state, *, daemon=False, timeout=360):
    manifest = bundle.verify(package, digest)
    desc = descriptor(state, package, digest)
    package, state = Path(package).resolve(), Path(state).resolve()
    lease = acquire_lease(manifest['lock_path'])
    parent, endpoint = (socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                        if daemon else (None, None))
    process = None
    diagnostics = None
    interrupted = False
    previous = {}
    def stop(_signal, _frame):
        nonlocal interrupted
        interrupted = True
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)  # This unreaped direct child only.
    try:
        run = state/'runs'/str(uuid.uuid4())
        run.mkdir(mode=0o700)
        controller = dict(version=1, package=str(package), manifest_sha256=digest,
                          directory=str(run))
        gateway = bundle.gateway_config_for(package, manifest, run)
        engine.save(run/'controller.json', controller)
        engine.save(run/'gateway.json', gateway)
        python = manifest['python']['path']
        config = dict(version=1, directory=str(state), service_id=desc['service_id'],
            python=python, journal=str(package/'source/tools/serve_recovery/journal.py'),
            controller=[python, '-I', '-B', str(package/'source/tools/serve_recovery/controller.py'),
                        '--config', str(run/'controller.json')],
            gateway=[python, '-I', '-B', str(package/'source/tools/serve_recovery'/
                     ('native_ingress.py' if manifest.get('ingress_mode') == 'native'
                      else 'gateway.py')),
                     '--config', str(run/'gateway.json')], quota_enforced=manifest['quota_enforced'],
            lease_path=manifest['lock_path'], lease_fd=lease, resource_mode='process',
            require_hard_memory_limit=False, term_ms=30000, kill_ms=5000,
            heartbeat_ms=15000, critical_memory_mib=1024,
            launch=dict(mode='daemon' if daemon else 'foreground', parent_pid=os.getpid(),
                parent_start_ticks=journal.process_start(os.getpid()),
                startup_fd=endpoint.fileno() if daemon else 0, timeout_ms=int(timeout*1000)))
        engine.save(run/'guardian.json', config)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, stop)
        # Keep fatal diagnostics in fixed RAM; ordinary disk failures must not
        # block Guardian or its inherited stderr. Runtime state uses private IPC.
        try:
            diagnostics = diagnostic_buffer()
        except OSError:
            # A failed optional memory sink cannot prevent core startup.
            diagnostics = None
        diagnostic_stdio = diagnostics if diagnostics is not None else subprocess.DEVNULL
        process = subprocess.Popen([str(package/'bin/q4t-guardian'), '--config',
            str(run/'guardian.json'), '--activate'], stdin=subprocess.DEVNULL,
            stdout=diagnostic_stdio, stderr=diagnostic_stdio,
            start_new_session=True,
            pass_fds=(lease, endpoint.fileno()) if daemon else (lease,))
        if endpoint is not None:
            endpoint.close()
            endpoint = None
        if daemon:
            parent.settimeout(timeout+2)
            ready = journal.receive(parent)
            ledger.require(type(ready) is dict and ready.get('guardian_pid') == process.pid and
                ready.get('service_id') == desc['service_id'] and ready.get('business_ready') is True and
                ready.get('daemon_detached') is False, 'service did not become ready')
            journal.send(parent, dict(version=1, service_id=desc['service_id'], op='detach'))
            result = journal.receive(parent)
            ledger.require(type(result) is dict and result.get('daemon_detached') is True and
                result.get('guardian_pid') == process.pid, 'daemon commit was not acknowledged')
            return dict(result, run=str(run))
        while process.poll() is None:
            time.sleep(0.1)
        return dict(guardian_exit_code=process.returncode, interrupted=interrupted, run=str(run))
    except BaseException:
        if parent is not None:
            parent.close()  # A failed startup handshake requests durable stop.
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                # Do not SIGKILL Root or unlock under unreaped/D-state children.
                # Root keeps the original lease and reports DRAINING/UNKNOWN.
                pass
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if parent is not None:
            parent.close()
        if endpoint is not None:
            endpoint.close()
        if diagnostics is not None:
            os.close(diagnostics)
        os.close(lease)  # No LOCK_UN on a shared open-file description.


def main(argv=None, *, package=None, digest=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    def timeout_seconds(raw):
        value = int(raw)
        if not 1 <= value <= 600:
            raise argparse.ArgumentTypeError('timeout must be 1..600 seconds')
        return value
    prepare = commands.add_parser('prepare')
    for name in ('binary', 'release', 'model', 'guardian', 'cuda-probe',
                 'policy', 'backend-policy', 'native-receipt', 'output'):
        prepare.add_argument('--'+name, type=Path, required=True)
    prepare.add_argument('--ingress-mode', choices=('native', 'nginx'), default='native')
    prepare.add_argument('--native-ingress', type=Path)
    prepare.add_argument('--nginx', type=Path)
    prepare.add_argument('--certificate', type=Path)
    prepare.add_argument('--key', type=Path)
    for name in ('init', 'run', 'start', 'status', 'diagnostics', 'stop'):
        command = commands.add_parser(name)
        command.add_argument('--state', type=Path, required=True)
        if package is None:
            command.add_argument('--package', type=Path, required=True)
            command.add_argument('--manifest-sha256', required=True)
        if name == 'start':
            command.add_argument('--daemon', action='store_true', required=True)
            command.add_argument('--timeout', type=timeout_seconds, default=360)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'prepare':
            result = bundle.prepare(args.binary, args.release, args.model, args.guardian,
                args.cuda_probe, args.nginx, args.policy, args.backend_policy, args.output,
                native_receipt=args.native_receipt, certificate=args.certificate, key=args.key,
                native_ingress=args.native_ingress, ingress_mode=args.ingress_mode)
        else:
            package = package or args.package
            digest = digest or args.manifest_sha256
            if args.operation == 'init':
                result = initialize(package, digest, args.state)
            elif args.operation in ('run', 'start'):
                result = launch(package, digest, args.state, daemon=args.operation == 'start',
                                timeout=getattr(args, 'timeout', 360))
            else:
                desc = descriptor(args.state, package, digest)
                try:
                    result = control.request(desc['service_id'],
                        'status' if args.operation == 'diagnostics' else args.operation, timeout=45
                                             if args.operation == 'stop' else 3)
                    if args.operation == 'diagnostics':
                        result = dict(service_id=desc['service_id'],
                            diagnostics=read_diagnostics(result))
                except OSError:
                    result = dict(service_id=desc['service_id'], state='UNKNOWN',
                                  business_ready=False, live_control=False)
        print(json.dumps(result, ensure_ascii=False))
        if args.operation == 'stop' and not result.get('cleanup_confirmed'):
            return 1
        return int(result.get('guardian_exit_code', 0) != 0 or result.get('state') == 'UNKNOWN')
    except (OSError, ValueError, RuntimeError) as error:
        # Fixed error class only: do not print private config or API credentials.
        print(json.dumps(dict(status='FAILED', category=type(error).__name__)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
