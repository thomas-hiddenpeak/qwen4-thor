"""Native bounded supervision of cold issuers, independent of the LLM tree.

Only auxiliary children belong to this subreaper. An issuer crash is reaped
before a replacement; healthy model processes are never signalled here.
"""
import os
from pathlib import Path
import select
import signal
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.serve_recovery import cold_authority, cold_channel as wire, cold_custody
from tools.serve_recovery import cold_issuer, cold_watch_policy as policy
from tools.serve_recovery import host_install as host, ledger, outer_tree


def address(host_id):
    ledger.require(ledger.identifier(host_id), 'auxiliary watch host identity required')
    return '\0q4t-cold-watch-'+host_id


def connect(host_id):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        sock.settimeout(2); sock.connect(address(host_id))
        ledger.require(wire.credentials(sock)[1] == 0, 'auxiliary watcher is not root')
        return sock
    except BaseException: sock.close(); raise


def control(directory, operation):
    value = host.public(directory)
    ledger.require(operation in ('status', 'stop'), 'invalid auxiliary watch operation')
    with connect(value['installation_id']) as sock:
        peer = wire.credentials(sock)
        wire.send(sock, dict(version=1, operation=operation))
        with wire.receive(sock, count=0) as reply:
            result = reply.result['result']
            ledger.require(reply.result.get('ok') is True and
                result['host_id'] == value['installation_id'] and result['watcher_pid'] == peer[0] and
                result['service_id'] == value['service_id'], 'auxiliary watch identity differs')
            return result


def withdraw(directory):
    value = host.public(directory)
    try: sock = connect(value['installation_id'])
    except ConnectionRefusedError: return False
    with sock:
        peer = wire.credentials(sock); pidfd = os.pidfd_open(peer[0])
        try:
            try:
                wire.send(sock, dict(version=1, operation='stop'))
                with wire.receive(sock, count=0) as reply:
                    ledger.require(reply.result.get('ok') is True and
                        reply.result['result']['host_id'] == value['installation_id'] and
                        reply.result['result']['watcher_pid'] == peer[0], 'auxiliary stop identity differs')
            except (BrokenPipeError, ConnectionResetError): pass
            ledger.require(bool(select.select([pidfd], [], [], 5)[0]),
                           'auxiliary watcher still owns its tree; retry original removal')
        finally: os.close(pidfd)
    return True


class Watch:
    def __init__(self, directory, value):
        self.directory, self.value = Path(directory), value
        self.tree = None; self.lease = -1; self.stopping = False
        self.reason = 'starting'; self.total = None; self.next_attempt = 0
        self.draining = None; self.sent = set()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            self.socket.setblocking(False)
            self.socket.bind(address(value['installation_id'])); self.socket.listen(4)
        except BaseException: self.socket.close(); raise

    def stop(self, *_args): self.stopping = True

    def status(self):
        return dict(host_id=self.value['installation_id'], service_id=self.value['service_id'],
            watcher_pid=os.getpid(), stopping=self.stopping, reason=self.reason, charged_starts=self.total,
            issuer_pid=None if self.tree is None else self.tree.root.pid,
            cleanup_pending=self.draining is not None, model_actions=False)

    def accept(self):
        try: connection, _ = self.socket.accept()
        except BlockingIOError: return
        with connection:
            try:
                connection.settimeout(.05); peer = wire.credentials(connection)
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                with wire.receive(connection, count=0, peer=peer) as incoming:
                    message = incoming.result; ledger.keys(message, ('version', 'operation'))
                    ledger.require(type(message['version']) is int and message['version'] == 1 and
                        message['operation'] in ('status', 'stop') and
                        peer[1] in (0, self.value['runtime_uid']) and
                        (message['operation'] != 'stop' or peer[1] == 0), 'auxiliary control denied')
                    if message['operation'] == 'stop': self.stop()
                    wire.send(connection, dict(ok=True, result=self.status()))
            except (OSError, ValueError, KeyError, TypeError): pass

    def dispatch(self):
        with host.exclusive(self.directory):
            value, root, directory, config = cold_authority.selection(self.directory)
            ledger.require(value == self.value, 'auxiliary host selection changed')
            # Old watcher loss does not release an issuer/helper's same OFD.
            # Busy custody is an observation, never permission to kill/adopt.
            fd = cold_custody.acquire(directory, config)
            try:
                result = policy.attempt(directory, config)
                self.reason, self.total = result['reason'], result['total']
                if not result['allowed']:
                    self.next_attempt = time.monotonic()+min(result['retry_after_seconds'], 5)
                    return
                self.tree = outer_tree.Tree().__enter__(); self.lease = fd; fd = -1
                env = dict(host.environment(), **{cold_custody.ENVIRONMENT: str(self.lease)})
                self.tree.launch([value['artifact']['python']['path'], '-I', '-B',
                    str(root/'tools/serve_recovery/cold_issuer.py'), '--directory', str(self.directory),
                    '--serve'], env=env, pass_fds=(self.lease,))
                self.reason = 'running'
            finally:
                if fd >= 0: os.close(fd)

    def tick(self):
        try:
            value = host.load(self.directory)
            if value != self.value or value['phase'] != 'ready' or not value['enabled']: self.stop()
        except (OSError, ValueError, KeyError, TypeError): self.stop()
        self.accept()
        if self.tree is not None:
            if self.tree.root is None:
                self.tree.close(); self.tree = None
                os.close(self.lease); self.lease = -1
            elif self.tree.poll() is not None or self.stopping or self.draining is not None:
                if self.draining is None: self.draining = time.monotonic(); self.reason = 'cleaning_auxiliary_tree'
                if self.tree.reap():
                    self.tree.close(); self.tree = None
                    os.close(self.lease); self.lease = -1
                    self.draining = None; self.sent.clear(); self.reason = 'issuer_exited'
                else:
                    self.tree.signal_children(signal.SIGTERM if time.monotonic()-self.draining < 2
                                              else signal.SIGKILL, self.sent)
            return
        if not self.stopping and time.monotonic() >= self.next_attempt:
            self.next_attempt = time.monotonic()+1
            try: self.dispatch()
            except BlockingIOError: self.reason = 'existing_auxiliary_custody'
            except (OSError, ValueError, RuntimeError, KeyError, TypeError): self.reason = 'auxiliary_history_unavailable'

    def run(self, endpoint=None):
        previous = {sig: signal.signal(sig, self.stop) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            self.tick()
            if endpoint is not None:
                try: wire.send(endpoint, dict(ready=True, watcher_pid=os.getpid()))
                except (OSError, ValueError): pass
                endpoint.close()
            while not self.stopping or self.tree is not None:
                self.tick(); time.sleep(.02)
        finally:
            self.stop()
            while self.tree is not None: self.tick(); time.sleep(.05)
            for sig, handler in previous.items(): signal.signal(sig, handler)
            self.socket.close()


def daemon(directory, value):
    parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try: pid = os.fork()
    except BaseException: parent.close(); endpoint.close(); raise
    if pid:
        endpoint.close(); parent.settimeout(5)
        try:
            with wire.receive(parent, count=0) as reply:
                ledger.require(reply.result == dict(ready=True, watcher_pid=pid), 'auxiliary bootstrap identity differs')
            return 0
        finally: parent.close()
    parent.close(); os.setsid()
    null = os.open('/dev/null', os.O_RDWR | os.O_CLOEXEC)
    for fd in (0, 1, 2): os.dup2(null, fd)
    if null > 2: os.close(null)
    code = 0
    try: Watch(directory, value).run(endpoint)
    except BaseException: code = 1
    finally: os._exit(code)


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    commands = parser.add_mutually_exclusive_group(required=True)
    for name in ('daemon', 'status', 'stop'): commands.add_argument('--'+name, action='store_true')
    args = parser.parse_args()
    if args.daemon:
        value = cold_issuer.protected(args.directory)
        with host.exclusive(args.directory): cold_authority.selection(args.directory)
        return daemon(args.directory, value)
    print(json.dumps(control(args.directory, 'stop' if args.stop else 'status'), sort_keys=True)); return 0


if __name__ == '__main__': raise SystemExit(main())
