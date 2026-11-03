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
    def __init__(self, path, maximum):
        self.path, self.maximum = Path(path), maximum
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
                self.write(data)
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


def classify(probe, protocol, binary):
    """Only validated observations affect supervision; busy is not failure."""
    pkg.require(probe['process']['executable_sha256'] == binary and
                probe['process']['listener_scope'] == 'loopback',
                'backend executable/listener mismatch')
    pkg.require(probe['protocol'] == protocol, 'backend protocol mismatch')
    pkg.require(all(probe['configuration'].get(k) == v
                    for k, v in expected_configuration().items()),
                'effective serving configuration differs')
    metrics = probe['metrics']
    if metrics['q4t_gpu_healthy'] == 0:
        return 'GPU_UNHEALTHY'
    return 'READY' if probe['ready'] else 'DEGRADED'


def probe(pid, port):
    started = time.monotonic()
    owner = identity.process(pid, port)
    live = transport.get(port, '/livez', deadline=started+8)
    contracts.readiness(contracts.json_object(live['body']), live['http_status'],
                        liveness=True)
    ready = transport.get(port, '/readyz', deadline=started+8)
    value = contracts.json_object(ready['body'])
    contracts.readiness(value, ready['http_status'], liveness=False)
    config = transport.get(port, '/v1/config', deadline=started+8)
    if config['http_status'] != 200:
        raise contracts.InvalidObservation('configuration_unavailable')
    configuration = contracts.configuration(contracts.json_object(config['body']),
                                            version=4)
    response = transport.get(port, '/metrics', deadline=started+8)
    if response['http_status'] != 200:
        raise contracts.InvalidObservation('metrics_unavailable')
    metrics, _ = contracts.parse_metrics(response['body'], version=4)
    pkg.require(identity.process(pid, port) == owner, 'backend identity changed')
    return dict(process=owner, protocol=configuration['protocol_sha256'],
                configuration=configuration,
                ready=value['ready'], metrics=metrics)


def restart_allowed(returncode, was_ready, used, limit, stopping):
    # q4t's normal nonzero exit includes classified GPU/cleanup failures.
    # Do not guess that those are safe to recover by loading the model again.
    return (not stopping and was_ready and returncode < 0 and used < limit)


def run(package, digest, output, *, observe=probe, read_memory=None):
    package, output = Path(package), Path(output)
    manifest = pkg.verify(package, digest)
    cfg = manifest['settings']
    stopping = threading.Event()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: stopping.set())
    state = dict(version=1, state='STARTING', owner=process_identity(os.getpid()),
                 attempts=0, restarts=0, replayed_requests=0,
                 package_manifest_sha256=digest, backend=None)
    child, logger = None, None
    events = RotatingLog(output/'events.log', 256*1024)
    watchdog = memory.Watchdog(cfg,
        lambda owner: signal_owner(owner, signal.SIGTERM),
        reader=read_memory or memory.read)

    def stopped():
        return stopping.is_set() or os.path.lexists(output/'stop.request.json')

    def publish(status, **changes):
        changed = status != state['state']
        state.update(state=status, updated_unix=time.time(),
                     memory=watchdog.snapshot(), **changes)
        pkg.save(output/'status.json', state)
        if changed:
            events.write(f'{time.time()} {status}\n'.encode())

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
                watchdog.start()
                while not stopped():
                    watchdog.check()
                    pkg.verify(package, digest)
                    with socket.socket() as port_check:
                        port_check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        port_check.bind(('127.0.0.1', cfg['port']))
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
                    child = subprocess.Popen(backend_command(package, manifest, output),
                        cwd=manifest['repository'], env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        start_new_session=True, close_fds=True,
                        pass_fds=inherited)
                    logger = RotatingLog(output/'backend.log', cfg['log_bytes'])
                    logger.start(child.stdout)
                    state['attempts'] += 1
                    owner = process_identity(child.pid)
                    watchdog.attach(owner)
                    publish('STARTING', backend=owner)
                    pkg.save(output/f"attempt-{state['attempts']}.json",
                        dict(owner=owner, command=pkg.command(package, manifest),
                             started_unix=time.time()))
                    start, missing, was_ready = time.monotonic(), None, False
                    while not stopped() and child.poll() is None:
                        watchdog.check()
                        pkg.require(logger.error is None, 'backend log capture failed')
                        now = time.monotonic()
                        try:
                            observation = observe(child.pid, cfg['port'])
                            status = classify(observation, manifest['protocol_sha256'],
                                              manifest['files']['q4t'])
                            pkg.require(status != 'GPU_UNHEALTHY', 'GPU unhealthy')
                            missing = None
                            was_ready = was_ready or status == 'READY'
                            publish(status, observation=observation)
                        except contracts.InvalidObservation as error:
                            missing = now if missing is None else missing
                            publish('OBSERVATION_UNAVAILABLE', observation_error=error.code)
                        watchdog.check()
                        if not was_ready:
                            pkg.require(time.monotonic()-start < cfg['startup_seconds'],
                                        'startup deadline exceeded')
                        elif missing is not None:
                            pkg.require(time.monotonic()-missing < cfg['unavailable_seconds'],
                                        'observation deadline exceeded')
                        if stopping.wait(cfg['poll_seconds']):
                            break
                    code = child.poll()
                    # Even a dead parent can leave grammar descendants. Cleanup
                    # precedes replacement and stays inside the same GPU lease.
                    clean()
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
                    watchdog.close()
        watchdog.check()
        publish('STOPPED', backend=None, cleanup_confirmed=True)
        return 0
    except Exception as error:
        # If cleanup failed, retain identities and failure instead of claiming
        # a successful stop. The outer lease guard also drains owned children.
        publish('FAILED', error=str(error), cleanup_confirmed=child is None)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
