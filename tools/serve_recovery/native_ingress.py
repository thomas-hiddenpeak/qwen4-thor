"""Guardian-owned native entrypoint with private PID-bound Unix admission.

The native child authenticates HTTP clients. This worker retains the existing
independent backend identity checks and queries live Guardian state for each
admission. No nginx, HTTP auth subrequest, model launch or request replay.
"""
import argparse
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_gateway import gateway as files
from tools.serve_recovery import control, gateway_config, journal, ledger, tls_control
from tools.serve_recovery.gateway import Admission, listener


class AdmissionSocket:
    """One bounded packet per connection, only from the pinned native child."""
    def __init__(self, address, clients, decide, child_pid):
        self.clients, self.decide = frozenset(clients), decide
        self.child_pid = child_pid
        self.selector = selectors.DefaultSelector()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.listener.bind('\0'+address)
            self.listener.listen(16)
            self.listener.setblocking(False)
            self.selector.register(self.listener, selectors.EVENT_READ)
        except BaseException:
            self.listener.close()
            self.selector.close()
            raise
        self.connections = {}

    def drop(self, sock):
        self.connections.pop(sock, None)
        self.selector.unregister(sock)
        sock.close()

    def close(self):
        for sock in list(self.connections):
            self.drop(sock)
        self.selector.close()
        self.listener.close()

    def step(self, timeout=0.05):
        # At most eight pending requests, and one live-Guardian round trip
        # per step, so an unavailable peer cannot starve worker heartbeats.
        processed = False
        for key, _ in self.selector.select(timeout):
            sock = key.fileobj
            if sock is self.listener:
                client, _ = self.listener.accept()
                peer = struct.unpack('3i', client.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if (peer[0] != self.child_pid() or peer[1] != os.geteuid() or
                        len(self.connections) >= 8):
                    client.close()
                    continue
                client.setblocking(False)
                self.connections[client] = time.monotonic()+1
                self.selector.register(client, selectors.EVENT_READ)
                continue
            if processed:
                continue
            processed = True
            try:
                raw, ancillary, flags, _ = sock.recvmsg(4096)
                ledger.require(raw and not ancillary and
                    not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC),
                    'invalid native admission frame')
                value = files.parse(raw)
                ledger.keys(value, ('version', 'client', 'lane'))
                ledger.require(type(value['version']) is int and value['version'] == 1
                    and type(value['client']) is str and value['client'] in self.clients
                    and value['lane'] in ('data', 'control'), 'invalid native admission')
                result = self.decide(value['lane'])
                raw = ledger.canonical(result)
                ledger.require(len(raw) <= 4096, 'oversized native admission reply')
                sock.send(raw)
            except (OSError, ValueError, TypeError, RuntimeError):
                pass
            finally:
                self.drop(sock)
        now = time.monotonic()
        for sock, deadline in list(self.connections.items()):
            if now >= deadline:
                self.drop(sock)


def run(config, channel):
    gateway_config.validate(config)
    ledger.require(config['version'] == 2, 'native ingress configuration required')
    directory = Path(config['directory'])
    info = directory.lstat()
    ledger.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
        stat.S_IMODE(info.st_mode) == 0o700, 'private native runtime parent required')
    binary, digest = config['native_ingress'], config['native_ingress_sha256']
    ledger.require(files.sha(binary) == digest, 'native ingress identity changed')
    runtime = directory/f'gateway-{channel.generation}'
    runtime.mkdir(mode=0o700)
    stopped, heartbeat, process = False, 0, None
    def stop(_signal, _frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def tick():
        nonlocal heartbeat
        if stopped or (process is not None and process.poll() is not None):
            return True
        if time.monotonic() >= heartbeat:
            channel.event('heartbeat')
            heartbeat = time.monotonic()+0.25
        return False
    admission = Admission(config, channel, stopped=tick)
    address = f'q4t-ingress-{os.geteuid()}-{channel.service_id}-{channel.generation}'
    tls_observation = tls_control.Observation(address) if config['tls'] else None
    tls_ready = True
    server = AdmissionSocket(address, (c['id'] for c in config['policy']['clients']),
        admission.request, lambda: process.pid if process is not None and
                                  process.poll() is None else None)
    try:
        settings = dict(version=2, parent_pid=os.getpid(),
            parent_start_ticks=journal.process_start(os.getpid()), address=address,
            policy=files.policy(config['policy']), tls=config['tls'],
            tls_lease=f'q4t-tls-{os.geteuid()}-{channel.service_id}')
        files.save(runtime/'ingress.json', ledger.canonical(settings)+b'\n')
        process = subprocess.Popen([sys.executable, '-I', '-B',
            str(ROOT/'tools/serve_recovery/cpu_exec.py'), '--parent-pid', str(os.getpid()),
            '--parent-start', str(journal.process_start(os.getpid())), '--',
            binary, '--config', str(runtime/'ingress.json')],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True)
        end = time.monotonic()+10
        while not tick():
            try:
                listener(process.pid, config['policy']['listen_host'],
                         config['policy']['listen_port'], digest)
                break
            except (OSError, ValueError, RuntimeError):
                ledger.require(time.monotonic() < end, 'native listener startup deadline')
                server.step()
        ledger.require(not tick(), 'native ingress exited during startup')
        channel.event('ready')
        next_update = 0
        while not tick():
            if tls_observation is not None:
                observed = tls_observation.step(process.pid)
                if observed is not None:
                    if tls_ready and not observed:
                        admission.close()
                        channel.unbound()
                    tls_ready = observed
            if time.monotonic() >= next_update:
                if tls_ready:
                    admission.update()
                next_update = time.monotonic()+0.25
            server.step()
    finally:
        server.close()
        if tls_observation is not None:
            tls_observation.close()
        admission.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass  # Guardian keeps ownership until actual reap.
        channel.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    run(files.parse(files.read_file(args.config, private=True)), control.GatewayChannel())


if __name__ == '__main__':
    main()
