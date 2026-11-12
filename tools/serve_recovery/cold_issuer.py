"""Project-owned root issuer, independent of healthy model lifetimes.

Run only from an explicitly installed immutable host source. One private local
request and one bounded helper tree may be active; no model is its descendant.
Stopping this auxiliary process only withdraws future cold-start admission.
"""
import os
from pathlib import Path
import signal
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.serve_recovery import cold_authority, cold_channel as wire, cold_custody, cold_requests
from tools.serve_recovery import device_resource, host_artifact, host_install as host, ledger, store


def protected(directory):
    ledger.require(os.geteuid() == 0, 'cold issuer requires explicit root invocation')
    with host.exclusive(directory) as directory:
        value = host.load(directory)
        ledger.require(host_artifact.verify(directory, value['artifact']) == ROOT,
                       'cold issuer must execute installed protected source')
        return value


def worker(directory, fd):
    value = protected(directory)
    if cold_custody.inherited():
        with host.exclusive(directory):
            _, _, device, config = cold_authority.selection(directory)
            cold_custody.verify(device, config)
    with socket.socket(fileno=fd) as channel:
        peer = wire.credentials(channel)
        ledger.require(peer[0] == os.getppid() and peer[1] == 0,
                       'cold worker requires its original root parent')
        with wire.receive(channel, count=0) as incoming:
            payload = incoming.result
            ledger.keys(payload, ('version', 'request', 'peer', 'descriptors'))
            numbers = payload['descriptors']
            ledger.require(type(payload['version']) is int and payload['version'] == 1 and
                type(numbers) is list and len(numbers) == 3 and len(set(numbers)) == 3 and
                all(ledger.integer(n, minimum=3) and n != fd for n in numbers) and
                type(payload['peer']) is list and len(payload['peer']) == 3 and
                payload['peer'][1:] == [value['runtime_uid'], value['runtime_gid']],
                'cold worker capability binding differs')
            try:
                with cold_authority.issue(directory, payload['request'], numbers[0], numbers[1],
                    tuple(payload['peer']), numbers[2]) as result:
                    wire.send(channel, dict(ok=True, result=result.result), result.fds)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                # Error text can include private paths; expose only a stable
                # bounded category to the unprivileged caller.
                wire.send(channel, dict(ok=False, reason='admission_rejected'))
                return 1
    return 0


class Server:
    def __init__(self, directory, value):
        self.directory, self.value = Path(directory), value
        self.pending = None; self.client = None; self.stopping = False
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            self.socket.setblocking(False)
            self.socket.bind(wire.address(value['installation_id'])); self.socket.listen(4)
        except BaseException: self.socket.close(); raise

    def stop(self, *_args): self.stopping = True

    def status(self):
        admission = 'unknown'
        try:
            with host.exclusive(self.directory):
                _, _, directory, config = cold_authority.selection(self.directory)
                fence = device_resource.lifecycle(directory, config)
                admission = 'unarmed'
                if fence['phase'] == 'armed':
                    admission = 'armed' if fence['boot_id'] == store.stamp().boot_id else 'requalify_required'
        except (OSError, ValueError, KeyError, TypeError): pass
        return dict(host_id=self.value['installation_id'], service_id=self.value['service_id'],
            issuer_pid=os.getpid(), accepting=not self.stopping,
            worker_pending=self.pending is not None, admission_state=admission, model_actions=False)

    def answer(self, sock, value, fds=()):
        try: wire.send(sock, value, fds)
        except (OSError, ValueError): pass

    def accept(self):
        try: connection, _ = self.socket.accept()
        except BlockingIOError: return
        keep = False
        try:
            connection.settimeout(.05)
            peer = wire.credentials(connection)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            ledger.require(peer[1] in (0, self.value['runtime_uid']), 'cold peer UID not selected')
            with wire.receive(connection, peer=peer) as incoming:
                message = incoming.result
                ledger.require(type(message) is dict and type(message.get('version')) is int and
                    message['version'] == 1, 'invalid cold request envelope')
                operation = message.get('operation')
                if operation in ('status', 'stop'):
                    ledger.keys(message, ('version', 'operation'))
                    ledger.require(not incoming.fds and (operation != 'stop' or peer[1] == 0),
                                   'cold stop requires the root administrator')
                    if operation == 'stop': self.stop()
                    self.answer(connection, dict(ok=True, result=self.status())); return
                ledger.keys(message, ('version', 'operation', 'request'))
                ledger.require(operation == 'issue' and len(incoming.fds) == 2 and
                    peer[1:] == (self.value['runtime_uid'], self.value['runtime_gid']),
                    'cold request descriptors or credentials differ')
                selected = cold_requests.request(message['request'])
                ledger.require(selected['host_id'] == self.value['installation_id'] and
                    selected['custody']['supervisor']['pid'] == peer[0], 'cold request owner differs')
                if self.pending is not None or self.stopping:
                    self.answer(connection, dict(ok=False, reason='issuer_busy')); return
                pidfd = os.pidfd_open(peer[0])
                try:
                    command = [self.value['artifact']['python']['path'], '-I', '-B',
                        str(ROOT/'tools/serve_recovery/cold_issuer.py'), '--directory', str(self.directory)]
                    self.pending = wire.Worker.start(command,
                        dict(version=1, request=selected, peer=list(peer)), (*incoming.fds, pidfd))
                finally: os.close(pidfd)
                self.client = connection; keep = True
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            self.answer(connection, dict(ok=False, reason='request_rejected'))
        finally:
            if not keep: connection.close()

    def tick(self):
        if self.pending is not None:
            current = self.pending
            if self.stopping and current.failure is None: current.failure = 'issuer_stopped'
            done = current.poll()
            if current.failure is not None and self.client is not None:
                self.answer(self.client, dict(ok=False, reason=current.failure))
                self.client.close(); self.client = None
            if done:
                if self.client is not None:
                    reply = current.response
                    try:
                        ledger.require(type(reply.result) is dict and type(reply.result.get('ok')) is bool,
                                       'invalid cold worker response')
                        if reply.result['ok']:
                            ledger.keys(reply.result, ('ok', 'result'))
                            ledger.require(len(reply.fds) == 4, 'cold worker missing descriptors')
                        else:
                            ledger.keys(reply.result, ('ok', 'reason'))
                            ledger.require(not reply.fds, 'failed cold worker returned descriptors')
                        self.answer(self.client, reply.result, reply.fds)
                    except (OSError, ValueError, KeyError, TypeError):
                        self.answer(self.client, dict(ok=False, reason='worker_reply_unknown'))
                    self.client.close(); self.client = None
                current.close(); self.pending = None
        if not self.stopping: self.accept()

    def run(self):
        previous = {sig: signal.signal(sig, self.stop) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            while not self.stopping or self.pending is not None:
                try:
                    value = host.load(self.directory)
                    if value['phase'] != 'ready' or not value['enabled']: self.stop()
                except (OSError, ValueError, KeyError, TypeError): self.stop()
                self.tick(); time.sleep(.02)
        finally:
            # A runtime error cannot silently release an unreaped worker slot.
            self.stop()
            while self.pending is not None:
                self.tick(); time.sleep(.05)
            for sig, handler in previous.items(): signal.signal(sig, handler)
            self.socket.close()


def control(directory, operation):
    value = host.public(directory)
    ledger.require(operation in ('status', 'stop'), 'invalid cold issuer control')
    with wire.connect(value['installation_id'], timeout=2) as sock:
        peer = wire.credentials(sock)
        wire.send(sock, dict(version=1, operation=operation))
        with wire.receive(sock, count=0) as result:
            ledger.require(result.result.get('ok') is True and
                result.result['result']['host_id'] == value['installation_id'] and
                result.result['result']['service_id'] == value['service_id'] and
                result.result['result']['issuer_pid'] == peer[0], 'cold issuer control identity differs')
            return result.result['result']


def daemon(directory, value):
    parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try: pid = os.fork()
    except BaseException: parent.close(); endpoint.close(); raise
    if pid:
        endpoint.close(); parent.settimeout(5)
        try:
            with wire.receive(parent, count=0) as reply:
                ledger.require(reply.result == dict(ready=True, issuer_pid=pid),
                               'cold issuer bootstrap receipt differs')
            return 0
        finally: parent.close()
    parent.close(); os.setsid()
    null = os.open('/dev/null', os.O_RDWR | os.O_CLOEXEC)
    for target in (0, 1, 2): os.dup2(null, target)
    if null > 2: os.close(null)
    code = 0
    try:
        # AF_UNIX records the creator/listener credentials. Bind only here,
        # after fork, so clients pin this actual long-lived issuer, not the
        # short-lived bootstrap parent. No service process is its descendant.
        server = Server(directory, value)
        try: wire.send(endpoint, dict(ready=True, issuer_pid=os.getpid()))
        except (OSError, ValueError): pass  # An explicit daemon survives a lost startup receipt.
        endpoint.close(); server.run()
    except BaseException: code = 1
    finally: os._exit(code)


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    operations = parser.add_mutually_exclusive_group(required=True)
    for name in ('serve', 'daemon', 'status', 'stop'): operations.add_argument('--'+name, action='store_true')
    operations.add_argument('--worker-fd', type=int)
    args = parser.parse_args()
    if args.worker_fd is not None: return worker(args.directory, args.worker_fd)
    if args.serve or args.daemon:
        value = protected(args.directory)
        with host.exclusive(args.directory):
            _, _, device, config = cold_authority.selection(args.directory)
            if cold_custody.inherited(): cold_custody.verify(device, config)
        if args.daemon: return daemon(args.directory, value)
        Server(args.directory, value).run(); return 0
    print(json.dumps(control(args.directory, 'stop' if args.stop else 'status'), sort_keys=True))
    return 0


if __name__ == '__main__': raise SystemExit(main())
