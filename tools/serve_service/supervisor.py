"""One managed generation: bounded recovery, no request replay."""
import ctypes
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time

from tools.release import release as processes
from tools.serve_ops import contracts, identity, transport
from tools.serve_service import package as pkg
from tools.serve_service import memory


def process_identity(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return dict(pid=pid, start_ticks=int(fields[19]),
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def signal_owner(owner, signum):
    try:
        fd = os.pidfd_open(owner['pid'])
    except ProcessLookupError:
        return False
    try:
        if process_identity(owner['pid']) != owner:
            return False
        signal.pidfd_send_signal(fd, signum)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(fd)


def arm_parent_death(owner):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), 'parent-death notification unavailable')
    # Close the race where the parent died before prctl. This runs in a fresh
    # Python helper, never in a multithreaded Popen preexec hook.
    pkg.require(os.getppid() == owner['pid'] and
                process_identity(owner['pid']) == owner, 'parent identity changed')


def backend_command(package, manifest, output):
    return [manifest['python']['path'], '-I', '-B',
        str(Path(package)/'source/tools/serve_service/backend.py'),
        '--run', str(output)]


class RotatingLog:
    """A bounded byte log, including oversized writes; errors are not hidden."""
    def __init__(self, path, maximum, *, observations=None):
        self.path, self.maximum = Path(path), maximum
        self.observations = observations
        self.error = None
        self.thread = None

    def write(self, data):
        while data:
            size = self.path.stat().st_size if self.path.exists() else 0
            if size == self.maximum:
                os.replace(self.path, self.path.with_suffix('.previous'))
                size = 0
            pkg.require(size < self.maximum, 'unexpected log size')
            chunk, data = data[:self.maximum-size], data[self.maximum-size:]
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                         os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                stream.write(chunk)

    def capture(self, pipe):
        try:
            while data := os.read(pipe.fileno(), 4096):
                if self.observations is None:
                    self.write(data)
                else:
                    # Keep draining even when the sink is unavailable. Closing
                    # this pipe would turn a log failure into backend SIGPIPE.
                    self.observations.perform('backend_log',
                        lambda: self.write(data), size=len(data))
        except Exception as error:
            self.error = repr(error)
        finally:
            pipe.close()

    def start(self, pipe):
        self.thread = threading.Thread(target=self.capture, args=(pipe,),
                                       name='q4t-managed-log', daemon=True)
        self.thread.start()


def expected_configuration():
    p = pkg.PROFILE
    return dict(max_context_tokens=p['max_len'], max_sequences=p['max_seq'],
        prefill_chunk_tokens=p['max_prefill'],
        max_inflight_requests=p['max_inflight_requests'],
        max_queued_requests=p['max_queued_requests'], mtp_enabled=True,
        structured_backend=True, memory_enabled=True,
        memory_floor_mib=p['min_available_memory_mib'],
        memory_recovery_mib=p['min_available_memory_mib']+
                            p['memory_recovery_margin_mib'])


def classify(probe, protocol, binary, client_policy=None, operations_port=0):
    """Only validated observations affect supervision; busy is not failure."""
    pkg.require(probe['process']['executable_sha256'] == binary and
                probe['process']['listener_scope'] == 'loopback',
                'backend executable/listener mismatch')
    pkg.require(probe['protocol'] == protocol, 'backend protocol mismatch')
    pkg.require(all(probe['configuration'].get(k) == v
                    for k, v in expected_configuration().items()),
                'effective serving configuration differs')
    if client_policy is not None:
        from tools.serve_gateway import client_policy as cp
        cp.observed_matches(probe.get('client_policy'), client_policy)
    if operations_port:
        pkg.require(probe.get('operations_port') == operations_port and
                    probe['configuration'].get('operations_port') == operations_port and
                    probe.get('observability_version') == 5 and
                    probe['operations_process']['executable_sha256'] == binary and
                    probe['operations_process']['listener_scope'] == 'loopback',
                    'operations listener configuration differs')
    metrics = probe['metrics']
    if metrics is None:
        pkg.require(isinstance(probe.get('health_status'), str),
                    'health observation unavailable')
    if metrics is not None and metrics['q4t_gpu_healthy'] == 0:
        return 'GPU_UNHEALTHY'
    if probe.get('health_status') == 'unhealthy':
        return 'BACKEND_UNHEALTHY'
    return 'READY' if probe['ready'] else 'DEGRADED'


def probe(pid, port, operations_port=0, *, deadline=None, stopped=None,
          include_metrics=True):
    started = time.monotonic()
    until = min(started+8, deadline if deadline is not None else float('inf'))
    def get(path):
        options = dict(deadline=until)
        if stopped is not None:
            options['stopped'] = stopped
        return transport.get(selected, path, **options)
    contracts.integer(operations_port, 0, 65535)
    if operations_port == port:
        raise contracts.InvalidObservation('operations_listener_port_conflict')
    owner = identity.process(pid, port)
    ops = identity.operations(owner, operations_port) if operations_port else None
    selected = operations_port or port
    live = get('/livez')
    contracts.readiness(contracts.json_object(live['body']), live['http_status'],
                        liveness=True)
    ready = get('/readyz')
    value = contracts.json_object(ready['body'])
    contracts.readiness(value, ready['http_status'], liveness=False)
    config = get('/v1/config')
    if config['http_status'] != 200:
        raise contracts.InvalidObservation('configuration_unavailable')
    raw_config = contracts.json_object(config['body'])
    version = raw_config.get('observability_version', 4)
    if type(version) is not int or version not in (4, 5):
        raise contracts.InvalidObservation('unsupported_observation_version')
    configuration = contracts.configuration(raw_config, version=version)
    if (operations_port and version != 5 or version == 5 and
            (configuration['public_port'] != port or
             configuration['operations_port'] != operations_port)):
        raise contracts.InvalidObservation('operations_listener_configuration_mismatch')
    metrics = None
    if include_metrics:
        response = get('/metrics')
        if response['http_status'] != 200:
            raise contracts.InvalidObservation('metrics_unavailable')
        metrics, _ = contracts.parse_metrics(response['body'], version=version)
    pkg.require(identity.process(pid, port) == owner, 'backend identity changed')
    if operations_port:
        pkg.require(identity.operations(owner, operations_port) == ops,
                    'operations listener identity changed')
    return dict(process=owner, protocol=configuration['protocol_sha256'],
                operations_process=ops, operations_port=operations_port,
                observability_version=version,
                client_policy=({key: raw_config['client_policy'].get(key) for key in
                    ('profile', 'enabled', 'sha256', 'clients', 'mutable',
                     'credential_values_redacted',
                     *(('snapshot', 'revision', 'content_sha256')
                       if raw_config['client_policy'].get('mutable') is True else ()))}
                    if type(raw_config.get('client_policy')) is dict else None),
                configuration=configuration,
                ready=value['ready'], health_status=value['status'],
                metrics=metrics, metrics_collection=(
                    'COLLECTED' if include_metrics else 'INDEPENDENT'))


def restart_allowed(returncode, was_ready, used, limit, stopping):
    # q4t's normal nonzero exit includes classified GPU/cleanup failures.
    # Do not guess that those are safe to recover by loading the model again.
    return (not stopping and was_ready and returncode < 0 and used < limit)


def preflight_ports(config):
    """Reject known listener conflicts before loading a managed backend.

    These checks reserve no sockets for exec. The native listener still owns
    the authoritative bind, including races after this observation.
    """
    endpoints = [('public', config['port'])]
    if config.get('operations_port', 0):
        endpoints.append(('operations', config['operations_port']))
    for role, port in endpoints:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(('127.0.0.1', port))
        except OSError as error:
            raise RuntimeError(
                f'{role} listener unavailable at 127.0.0.1:{port}: {error}') from error


def run(package, digest, output, *, observe=None, read_memory=None):
    package, output = Path(package), Path(output)
    manifest = pkg.verify(package, digest)
    cfg = manifest['settings']
    observations = None
    if manifest.get('version') in (9, 10):
        from tools.serve_service import auxiliary
        pkg.require(cfg.get('nvml_policy') == 'degrade' and
                    cfg['restart_limit'] == 0,
                    'auxiliary isolation requires degradation without restart')
        observations = auxiliary.Observations()
    stopping = threading.Event()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: stopping.set())
    state = dict(version=1, state='STARTING', owner=process_identity(os.getpid()),
                 attempts=0, restarts=0, replayed_requests=0,
                 package_manifest_sha256=digest, backend=None)
    child, logger = None, None
    status_written = False
    envelope = None
    events = RotatingLog(output/'events.log', 256*1024)
    watchdog = memory.Watchdog(cfg,
        lambda owner: signal_owner(owner, signal.SIGTERM),
        reader=read_memory or memory.read)

    def stopped():
        return stopping.is_set() or os.path.lexists(output/'stop.request.json')

    def publish(status, **changes):
        nonlocal status_written
        changed = status != state['state']
        state.update(state=status, updated_unix=time.time(),
                     memory=watchdog.snapshot(), **changes)
        if envelope is not None:
            state['host_resources'] = dict(policy=envelope.selected, observation=envelope.latest,
                violation=envelope.error, payload_removed=envelope.closed)
            if manifest.get('version') == 10:
                state['host_resources'].update(runtime_verification='CGROUPFS',
                    runtime_manager_observation='NOT_QUERIED',
                    statistics_gap_samples=envelope.statistics_gap_samples)
        if changed and observations is not None:
            data = f'{time.time()} {status}\n'.encode()
            observations.perform('events', lambda: events.write(data),
                                 size=len(data))
        if observations is not None:
            state['auxiliary_io'] = observations.snapshot()
            state['had_auxiliary_gaps'] = observations.had_gaps
        # The first snapshot contains the parent identity used by backend.py.
        # It must exist before exec. Later snapshots are observations only.
        if observations is None or not status_written:
            pkg.save(output/'status.json', state)
            status_written = True
        else:
            observations.save(output/'status.json', state, 'status')
        if changed and observations is None:
            data = f'{time.time()} {status}\n'.encode()
            events.write(data)

    def clean():
        nonlocal child, logger
        if child is not None:
            processes.stop_children({'backend': child},
                {'backend': logger.thread}, grace=cfg['stop_seconds'],
                kill_grace=cfg['kill_seconds'])
            pkg.require(logger.error is None, 'backend log capture failed')
            child, logger = None, None
            watchdog.attach(None)

    try:
        with processes.child_subreaper():
            try:
                if 'host_resources' in manifest:
                    from tools.serve_service import resources
                    options = ({'isolate_manager': True}
                               if manifest.get('version') == 10 else {})
                    envelope = resources.Envelope(manifest['host_resources'], output,
                                                  **options)
                watchdog.start()
                while not stopped():
                    watchdog.check()
                    pkg.verify(package, digest)
                    preflight_ports(cfg)
                    # No Q4T fault hooks, linker injection, or caller cache
                    # settings cross the managed-service boundary.
                    env = dict(PATH='/usr/bin:/bin', LANG='C.UTF-8',
                               HOME=str(Path.home()))
                    inherited = ()
                    if 'Q4T_MANAGED_LEASE_FD' in os.environ:
                        fd = int(os.environ['Q4T_MANAGED_LEASE_FD'])
                        inherited = (fd,)
                        env['Q4T_MANAGED_LEASE_FD'] = str(fd)
                    publish('STARTING')
                    watchdog.sample(startup=True)
                    watchdog.check()
                    if stopped():
                        break
                    start = time.monotonic()
                    child = subprocess.Popen(backend_command(package, manifest, output),
                        cwd=manifest['repository'], env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        start_new_session=True, close_fds=True,
                        pass_fds=inherited)
                    logger = RotatingLog(output/'backend.log', cfg['log_bytes'],
                                         observations=observations)
                    logger.start(child.stdout)
                    state['attempts'] += 1
                    owner = process_identity(child.pid)
                    watchdog.attach(owner)
                    publish('STARTING', backend=owner)
                    pkg.save(output/f"attempt-{state['attempts']}.json",
                        dict(owner=owner, command=pkg.command(package, manifest,
                             state_directory=output),
                             started_unix=time.time()))
                    missing, was_ready = None, False
                    def phase_limit():
                        if not was_ready:
                            return start+cfg['startup_seconds'], 'startup'
                        if missing is not None:
                            return missing+cfg['unavailable_seconds'], 'observation'
                        return float('inf'), 'observation'

                    def check_limit():
                        until, phase = phase_limit()
                        pkg.require(time.monotonic() < until,
                                    phase+' deadline exceeded')
                        return until

                    while not stopped() and child.poll() is None:
                        watchdog.check()
                        if envelope is not None: envelope.check()
                        pkg.require(logger.error is None, 'backend log capture failed')
                        deadline = check_limit()
                        now = time.monotonic()
                        try:
                            operations_port = cfg.get('operations_port', 0)
                            if observe is None:
                                options = ({'include_metrics': False}
                                           if observations is not None else {})
                                observation = probe(child.pid, cfg['port'], operations_port,
                                                    deadline=deadline, stopped=stopped,
                                                    **options)
                            else:
                                # Injected CPU observers retain their historical
                                # call shape, but cannot bypass post-call limits.
                                observation = (observe(child.pid, cfg['port'], operations_port)
                                               if operations_port else observe(child.pid, cfg['port']))
                            if stopped():
                                break
                            check_limit()
                            status = classify(observation, manifest['protocol_sha256'],
                                              manifest['files']['q4t'],
                                              manifest.get('client_policy'), operations_port)
                            pkg.require(status != 'GPU_UNHEALTHY', 'GPU unhealthy')
                            pkg.require(status != 'BACKEND_UNHEALTHY',
                                        'Backend unhealthy')
                            if stopped():
                                break
                            check_limit()
                            missing = None
                            was_ready = was_ready or status == 'READY'
                            publish(status, observation=observation)
                        except contracts.InvalidObservation as error:
                            if stopped():
                                break
                            check_limit()
                            missing = now if missing is None else missing
                            publish('OBSERVATION_UNAVAILABLE', observation_error=error.code)
                        watchdog.check()
                        if stopped():
                            break
                        until = min(time.monotonic()+cfg['poll_seconds'], check_limit())
                        while not stopped() and time.monotonic() < until:
                            stopping.wait(min(.1, max(0, until-time.monotonic())))
                    code = child.poll()
                    # Even a dead parent can leave grammar descendants. Cleanup
                    # precedes replacement and stays inside the same GPU lease.
                    clean()
                    if envelope is not None: envelope.check()
                    watchdog.check()
                    if stopped():
                        break
                    if not restart_allowed(code, was_ready, state['restarts'],
                                           cfg['restart_limit'], stopped()):
                        raise RuntimeError('backend exited; recovery not permitted')
                    state['restarts'] += 1
                    publish('RESTART_WAIT', backend_returncode=code)
                    until = time.monotonic()+cfg['restart_delay_seconds']
                    while not stopped() and time.monotonic() < until:
                        watchdog.check()
                        stopping.wait(min(.1, max(0, until-time.monotonic())))
                publish('STOPPING')
            finally:
                try:
                    clean()
                finally:
                    try:
                        watchdog.close()
                    finally:
                        if envelope is not None: envelope.close()
        watchdog.check()
        if envelope is not None:
            pkg.require(envelope.error is None, envelope.error)
        publish('STOPPED', backend=None, cleanup_confirmed=True)
        return 0
    except Exception as error:
        # If cleanup failed, retain identities and failure instead of claiming
        # a successful stop. The outer lease guard also drains owned children.
        publish('FAILED', error=str(error), cleanup_confirmed=child is None and
                (envelope is None or envelope.closed))
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
