"""Unified recovery service entrypoint; no systemd, shell loop or auto install."""
import argparse
from contextlib import ExitStack
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
from tools.serve_recovery import bundle, child, control, journal, ledger, store, tls_control
from tools.serve_service import package as engine


DIAGNOSTIC_BYTES = 64 * 1024
# Linux UAPI values, also needed by Python builds whose build-time headers did
# not export the names. The kernel still enforces every requested seal.
ADD_SEALS = getattr(fcntl, 'F_ADD_SEALS', 1033)
GET_SEALS = getattr(fcntl, 'F_GET_SEALS', 1034)
DIAGNOSTIC_SEALS = (getattr(fcntl, 'F_SEAL_GROW', 4) |
                    getattr(fcntl, 'F_SEAL_SHRINK', 2) |
                    getattr(fcntl, 'F_SEAL_SEAL', 1))


def diagnostic_buffer():
    """Private, fixed-capacity RAM for fatal control-tree diagnostics.

    Healthy control loops emit structured IPC state, not stdio logging. Model
    output uses observation_io separately. A full buffer rejects further fatal
    traceback writes without growing RAM or touching a filesystem sink.
    """
    fd = os.memfd_create('q4t-service-diagnostics', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.ftruncate(fd, DIAGNOSTIC_BYTES)
        fcntl.fcntl(fd, ADD_SEALS, DIAGNOSTIC_SEALS)
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
            fcntl.fcntl(fd, GET_SEALS) == DIAGNOSTIC_SEALS and
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
    if manifest.get('version', 1) >= 8 and manifest.get('ingress_mode') == 'native':
        from tools.serve_recovery import initialization
        return initialization.finish(state, package, digest, manifest)
    service_id = str(uuid.uuid4())
    with store.Store(state, service_id, initialize=True):
        pass
    descriptor = dict(version=1, service_id=service_id,
        package=str(Path(package).resolve()), manifest_sha256=digest)
    engine.save(state/'service.json', descriptor)
    (state/'runs').mkdir(mode=0o700)
    if manifest['version'] >= 7 and manifest.get('ingress_mode') == 'native':
        from tools.serve_recovery import access_manager
        access_manager.initialize(state, descriptor, manifest)
        if manifest['version'] >= 8:
            from tools.serve_recovery import quota_manager
            quota_manager.initialize(state, descriptor, manifest)
            from tools.serve_recovery import access_store, usage_store
            usage_store.initialize_bound(access_store.binding(state, descriptor['service_id']))
    return dict(state=str(state), service_id=service_id, initialized=True,
                desired='STOPPED', automatic_start=False)


def descriptor(state, package, digest):
    state = Path(state).absolute()
    info = state.lstat()
    ledger.require(state.resolve() == state and stat.S_ISDIR(info.st_mode) and
        info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
        'private canonical service state required')
    value = ingress.parse(ingress.read_file(state/'service.json', private=True))
    from tools.serve_recovery import deployment_policy
    deployment_policy.descriptor(value)
    ledger.require(
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


def controller_configuration(package, digest, state, run):
    # Advisory provenance shares the already required configuration write.
    # Missing observation cannot prevent an otherwise valid model launch.
    try:
        retention_context = dict(
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            parent_pid=os.getpid(), parent_start_ticks=journal.process_start(os.getpid()))
    except (OSError, ValueError, IndexError):
        retention_context = None
    return dict(version=3, package=str(package), manifest_sha256=digest,
                directory=str(run), state_directory=str(state),
                retention_context=retention_context)


def launch(package, digest, state, *, daemon=False, timeout=360):
    # Held until Guardian owns its instance lock (or foreground exit). The
    # descriptor cannot change between validation and the actual startup.
    from tools.serve_recovery import deployment
    manifest = bundle.verify(package, digest)
    with deployment.claim(state, shared=True):
        return launch_owned(package, digest, state, manifest=manifest, daemon=daemon, timeout=timeout)


def launch_owned(package, digest, state, *, manifest, daemon=False, timeout=360):
    desc = descriptor(state, package, digest)
    from tools.serve_recovery import deployment
    deployment.ready(state, desc)
    package, state = Path(package).resolve(), Path(state).resolve()
    lease = acquire_lease(manifest['lock_path'])
    parent = endpoint = None
    process = None
    diagnostics = None
    cold_fds = ()
    interrupted = False
    previous = {}
    def stop(_signal, _frame):
        nonlocal interrupted
        interrupted = True
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)  # This unreaped direct child only.
    try:
        from tools.serve_recovery import cold_admission
        cold_config, cold_fds = cold_admission.configuration(desc['service_id'], digest, lease)
        ledger.require(cold_config is None or manifest['version'] >= 17,
                       'cold device startup requires service profile v17')
        if daemon:
            parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        run = state/'runs'/str(uuid.uuid4())
        run.mkdir(mode=0o700)
        controller = controller_configuration(package, digest, state, run)
        access = None
        if manifest['version'] >= 7 and manifest.get('ingress_mode') == 'native':
            from tools.serve_recovery import access_store
            access = access_store.binding(state, desc['service_id'])
            if manifest['version'] >= 8:controller.update(version=4, limits=access)
            if manifest.get('recovery_profiles') is not None:
                controller.update(version=5, profiles=access)
            # No journal read in this process or silent legacy-key fallback.
            # The isolated reader gates the public credential configuration.
        gateway = bundle.gateway_config_for(package, manifest, run, access=access)
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
        if manifest['version'] >= 8 and manifest.get('ingress_mode') == 'native':
            config.update(version=2, usage_handoff=True)
        from tools.serve_recovery import version_launch
        versions = version_launch.configuration(state,desc,run)
        if versions is not None:
            config.update(version=3,version_recovery=versions)
        if cold_config is not None:
            config.update(version=4, usage_handoff=config.get('usage_handoff', False),
                          version_recovery=versions, cold_admission=cold_config)
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
            pass_fds=(lease, *cold_fds, *((endpoint.fileno(),) if daemon else ())))
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
        for fd in cold_fds: os.close(fd)
        os.close(lease)  # No LOCK_UN on a shared open-file description.


def main(argv=None, *, package=None, digest=None, workspace=None):
    os.umask(0o077)
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['device-domain']:
        from tools.serve_recovery import device_domain
        return device_domain.main(argv[1:])
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
    prepare.add_argument('--device-control', type=Path)
    prepare.add_argument('--nginx', type=Path)
    prepare.add_argument('--certificate', type=Path)
    prepare.add_argument('--key', type=Path)
    prepare.add_argument('--client-ca', type=Path)
    prepare.add_argument('--client-crl', type=Path)
    prepare.add_argument('--served-model-name')
    prepare.add_argument('--model-alias', action='append', default=[])
    prepare.add_argument('--object-store-dir')
    prepare.add_argument('--capacity-config', type=Path)
    prepare.add_argument('--access-program-policy', type=Path)
    prepare.add_argument('--file-input-policy', type=Path)
    prepare.add_argument('--embedding-model-dir', type=Path)
    prepare.add_argument('--recovery-profiles', type=Path)
    prepare.add_argument('--recovery-profile-policy', type=Path)
    for option in engine.APPLICATION_LIMITS:
        prepare.add_argument('--object-'+option.replace('_', '-'), type=int)
    for name in ('check', 'reload', 'init', 'init-resume', 'run', 'start', 'status', 'diagnostics', 'stop',
                 'tls-status', 'tls-reload', 'observe', 'metrics', 'export',
                 'retention-plan', 'retention-archive', 'retention-apply', 'retention-resume',
                 'retention-migrate', 'access-status', 'access-propose', 'access-apply',
                 'access-migrate', 'limits-status', 'limits-propose', 'limits-apply',
                 'limits-migrate', 'usage-status', 'usage-migrate',
                 'deploy', 'rollback', 'deployment-status', 'profiles-status',
                 'version-prepare', 'version-register', 'version-status', 'version-adapt',
                 'publish', 'publish-status', 'publish-resume', 'publish-rollback'):
        command = commands.add_parser(name)
        command.add_argument('--state', type=Path, required=name not in ('check', 'version-adapt'))
        command.add_argument('--package', type=Path, default=package, required=package is None)
        command.add_argument('--manifest-sha256', default=digest, required=digest is None)
        if name == 'reload':
            command.add_argument('--component', choices=('tls', 'access', 'limits'), required=True)
            command.add_argument('--proposal', type=Path)
            command.add_argument('--wait-seconds', type=int, default=3)
        if name == 'retention-plan':
            command.add_argument('--max-age-days', type=int, default=14)
            command.add_argument('--max-runs', type=int, default=100)
            command.add_argument('--max-bytes', type=int, default=1024**3)
            command.add_argument('--plan-output', type=Path)
        if name in ('retention-archive', 'retention-resume', 'retention-migrate'):
            command.add_argument('--run', required=True)
        if name == 'retention-archive':
            command.add_argument('--archive', type=Path, required=True)
        if name == 'retention-apply':
            command.add_argument('--plan', type=Path, required=True)
        if name == 'export':
            command.add_argument('--port', type=int, required=True)
        if name == 'start':
            command.add_argument('--daemon', action='store_true', required=True)
            command.add_argument('--timeout', type=timeout_seconds, default=360)
        if name in ('access-status', 'access-propose', 'limits-status', 'limits-propose'):
            command.add_argument('--request-id')
        if name == 'access-propose':
            command.add_argument('--keys', type=Path, required=True)
            command.add_argument('--proposal', type=Path, required=True)
        if name == 'limits-propose':
            command.add_argument('--limits', type=Path, required=True)
            command.add_argument('--proposal', type=Path, required=True)
        if name in ('access-apply', 'limits-apply'):
            command.add_argument('--proposal', type=Path, required=True)
            command.add_argument('--wait-seconds', type=int, default=3)
        if name in ('deploy', 'rollback'):
            command.add_argument('--expected-manifest-sha256', required=True)
            command.add_argument('--request-id', required=True)
        if name == 'version-prepare':
            command.add_argument('--fallback-package',type=Path,required=True)
            command.add_argument('--fallback-manifest-sha256',required=True)
            command.add_argument('--fallback-release',type=Path,required=True)
            command.add_argument('--fallback-decision',type=Path,required=True)
            command.add_argument('--cycle-id',required=True)
        if name == 'version-adapt':
            command.add_argument('--output', type=Path, required=True)
        if name in ('version-prepare','version-register'):
            command.add_argument('--registration',type=Path,required=True)
        if name == 'version-register':command.add_argument('--expected-cycle')
        if name in ('publish','publish-resume','publish-rollback'):
            command.add_argument('--request-id', required=True)
        if name in ('publish','publish-rollback'):
            command.add_argument('--default-root', type=Path, required=True)
            command.add_argument('--expected-manifest-sha256', required=True)
            command.add_argument('--expected-revision', type=int, required=True)
        if name == 'publish':command.add_argument('--service-release',type=Path,required=True)
        if name == 'publish-rollback':command.add_argument('--generation-sha256',required=True)
    args = parser.parse_args(argv)
    locks = ExitStack()
    try:
        if args.operation == 'prepare':
            repository = (workspace if workspace is not None else
                bundle.verify(package, digest)['repository'] if package is not None else bundle.ROOT)
            result = bundle.prepare(args.binary, args.release, args.model, args.guardian,
                args.cuda_probe, args.nginx, args.policy, args.backend_policy, args.output,
                native_receipt=args.native_receipt, certificate=args.certificate, key=args.key,
                native_ingress=args.native_ingress, ingress_mode=args.ingress_mode,
                device_control=args.device_control,
                client_ca=args.client_ca, client_crl=args.client_crl,
                capacity_config=engine.read(args.capacity_config)
                    if args.capacity_config is not None else None,
                repository=repository, access_program_policy=args.access_program_policy,
                file_input_policy=args.file_input_policy,
                embedding_model_dir=args.embedding_model_dir,
                recovery_profiles=args.recovery_profiles,
                recovery_profile_policy=engine.read(args.recovery_profile_policy)
                    if args.recovery_profile_policy is not None else None,
                model_names=dict(
                    name=args.served_model_name if args.served_model_name is not None
                         else 'qwen3.8-flash-next', aliases=args.model_alias)
                    if args.served_model_name is not None or args.model_alias else None,
                application_state=({**({'directory': args.object_store_dir}
                    if args.object_store_dir is not None else {}),
                    **{k: getattr(args, 'object_'+k) for k in engine.APPLICATION_LIMITS
                       if getattr(args, 'object_'+k) is not None}} or None))
        elif args.operation == 'version-adapt':
            from tools.serve_recovery import cold_compat
            result = cold_compat.prepare(args.package, args.manifest_sha256, args.output)
        else:
            package = args.package
            digest = args.manifest_sha256
            if args.operation.startswith(('access-', 'limits-', 'usage-')):
                from tools.serve_recovery import deployment
                locks.enter_context(deployment.claim(args.state, shared=True))
                if not args.operation.endswith('-status'):
                    deployment.ready(args.state, descriptor(args.state, package, digest))
            if args.operation in ('check', 'reload'):
                from tools.serve_recovery import lifecycle
                result = (lifecycle.check(package, digest, args.state) if args.operation == 'check' else
                    lifecycle.reload(package, digest, args.state, component=args.component,
                        proposal=args.proposal, wait_seconds=args.wait_seconds))
            elif args.operation == 'init':
                result = initialize(package, digest, args.state)
            elif args.operation == 'init-resume':
                from tools.serve_recovery import initialization
                result = initialization.finish(args.state, package, digest, bundle.verify(package, digest))
            elif args.operation.startswith('publish'):
                from tools.serve_recovery import publication
                if args.operation == 'publish-status':result = publication.status(args.state)
                elif args.operation == 'publish-resume':
                    result = publication.execute(args.state,request_id=args.request_id,resume=True)
                else:
                    result = publication.execute(args.state,request_id=args.request_id,
                        root=args.default_root,package=package,digest=digest,
                        release=getattr(args,'service_release',None),
                        expected=args.expected_manifest_sha256,revision=args.expected_revision,
                        generation=getattr(args,'generation_sha256',None))
            elif args.operation == 'profiles-status':
                from tools.serve_recovery import deployment, profile_manager
                with deployment.claim(args.state, shared=True):
                    desc = descriptor(args.state, package, digest)
                    deployment.ready(args.state, desc)
                    manifest = bundle.verify(package, digest)
                    result = profile_manager.status(args.state, desc, manifest)
            elif args.operation.startswith('version-'):
                from tools.serve_recovery import deployment, version_launch
                desc = descriptor(args.state,package,digest)
                root = dict(package=desc['package'],manifest_sha256=desc['manifest_sha256'])
                if args.operation == 'version-register':
                    result = version_launch.register_file(args.state,root,
                        args.registration,args.expected_cycle)
                else:
                    with deployment.claim(args.state,shared=True):
                        deployment.ready(args.state,desc)
                        if args.operation == 'version-status':
                            result = version_launch.status(args.state,desc)
                        else:
                            receipt = {name:dict(path=str(path.absolute()),sha256=engine.sha(path))
                                for name,path in [('release',args.fallback_release),
                                                  ('decision',args.fallback_decision)]}
                            result = version_launch.prepare_file(root,
                                dict(package=str(args.fallback_package.absolute()),
                                    manifest_sha256=args.fallback_manifest_sha256),
                                receipt,args.registration,args.cycle_id)
            elif args.operation in ('deploy', 'rollback', 'deployment-status'):
                from tools.serve_recovery import deployment
                result = (deployment.status(args.state) if args.operation == 'deployment-status' else
                    deployment.switch(args.state, package, digest,
                        expected=args.expected_manifest_sha256, request_id=args.request_id,
                        rollback=args.operation == 'rollback'))
            elif args.operation in ('run', 'start'):
                result = launch(package, digest, args.state, daemon=args.operation == 'start',
                                timeout=getattr(args, 'timeout', 360))
            elif args.operation.startswith('usage-'):
                from tools.serve_recovery import usage_manager
                desc = descriptor(args.state, package, digest)
                manifest = bundle.verify(package, digest)
                result = (usage_manager.migrate(args.state, desc, manifest) if args.operation == 'usage-migrate'
                          else usage_manager.status(args.state, desc, manifest))
            elif args.operation.startswith('limits-'):
                from tools.serve_recovery import quota_manager
                desc = descriptor(args.state, package, digest)
                manifest = bundle.verify(package, digest)
                ledger.require(manifest.get('ingress_mode') == 'native', 'native limits management required')
                if args.operation == 'limits-migrate':result = quota_manager.migrate(args.state, desc, manifest)
                elif args.operation == 'limits-status':
                    result = quota_manager.status(args.state, desc, manifest, args.request_id)
                else:
                    ledger.require(manifest['version'] >= 8, 'live limits profile required')
                    if args.operation == 'limits-propose':
                        result = quota_manager.propose(args.state, desc, args.limits, args.proposal, args.request_id)
                    else:
                        result = quota_manager.apply(args.state, desc, manifest, args.proposal,
                                                     wait_seconds=args.wait_seconds)
            elif args.operation.startswith('access-'):
                from tools.serve_recovery import access_manager
                desc = descriptor(args.state, package, digest)
                manifest = bundle.verify(package, digest)
                ledger.require(manifest.get('ingress_mode') == 'native',
                               'native credential management required')
                if args.operation == 'access-migrate':
                    result = access_manager.migrate(args.state, desc, manifest)
                elif args.operation == 'access-status':
                    result = access_manager.status(args.state, desc, manifest, args.request_id)
                else:
                    ledger.require(manifest['version'] >= 7, 'live credential profile required')
                    if args.operation == 'access-propose':
                        result = access_manager.propose(args.state, desc, args.keys,
                            args.proposal, args.request_id)
                    else:
                        result = access_manager.apply(args.state, desc, manifest, args.proposal,
                                                      wait_seconds=args.wait_seconds)
            elif args.operation.startswith('retention-'):
                from tools.serve_recovery import retention
                from tools.serve_host import native
                descriptor(args.state, package, digest)
                if args.operation == 'retention-plan':
                    result = retention.plan(args.state, limits=retention.policy(
                        args.max_age_days, args.max_runs, args.max_bytes))
                    if args.plan_output is not None:
                        retention.write_new(args.plan_output, ledger.canonical(result))
                elif args.operation == 'retention-archive':
                    result = retention.archive(args.state, args.run, args.archive)
                elif args.operation == 'retention-migrate':
                    from tools.serve_recovery import retention_epoch
                    result = retention_epoch.migrate(args.state, args.run)
                elif args.operation == 'retention-resume':
                    result = retention.resume(args.state, args.run)
                else:
                    result = retention.apply(args.state, native.private_json(
                        args.plan, retention.MAX_MANIFEST))
            elif args.operation == 'export':
                from tools.serve_host.native_exporter import export
                return export(package, digest, args.state, args.port)
            elif args.operation in ('observe', 'metrics'):
                from tools.serve_host import native
                result = native.current(native.collect(native.bind(package, digest, args.state)))
                if args.operation == 'metrics':
                    print(native.metrics(result), end='')
                    return int(result['state'] == 'UNKNOWN')
            else:
                desc = descriptor(args.state, package, digest)
                try:
                    result = control.request(desc['service_id'],
                        'status' if args.operation in ('diagnostics', 'tls-status',
                            'tls-reload') else args.operation, timeout=45
                                             if args.operation == 'stop' else 3)
                    if args.operation == 'diagnostics':
                        result = dict(service_id=desc['service_id'],
                            diagnostics=read_diagnostics(result))
                    elif args.operation in ('tls-status', 'tls-reload'):
                        manifest = bundle.verify(package, digest)
                        result = tls_control.execute(desc, result, manifest,
                            reload=args.operation == 'tls-reload')
                except OSError:
                    result = dict(service_id=desc['service_id'], state='UNKNOWN',
                                  business_ready=False, live_control=False)
                    if args.operation in ('status', 'stop'):
                        from tools.serve_recovery import lifecycle
                        try:result = lifecycle.stopped(args.state, package, digest)
                        except (OSError, ValueError, RuntimeError):pass
        print(json.dumps(result, ensure_ascii=False))
        if args.operation == 'stop' and not result.get('cleanup_confirmed'):
            return 1
        if args.operation in ('tls-reload', 'reload') and not result.get('reloaded'):
            return 1
        if args.operation in ('access-status', 'limits-status', 'usage-status') and result.get('durable_error'):
            return 1
        return int(result.get('guardian_exit_code', 0) != 0 or result.get('state') == 'UNKNOWN' or
                   result.get('status') == 'UNKNOWN')
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        # Fixed error class only: do not print private config or API credentials.
        result = dict(status='FAILED', category=type(error).__name__)
        if args.operation.startswith('access-'):
            from tools.serve_recovery import access_manager
            result = access_manager.failure(error)
        if args.operation.startswith('limits-'):
            from tools.serve_recovery import quota_manager
            result = quota_manager.failure(error)
        if args.operation.startswith('usage-'):
            from tools.serve_recovery import usage_manager
            result = usage_manager.failure(error)
        print(json.dumps(result), file=sys.stderr)
        return 1
    finally:
        locks.close()


if __name__ == '__main__':
    sys.exit(main())
