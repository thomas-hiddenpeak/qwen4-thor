"""Private same-UID operator channel for the explicitly selected outer loop."""
import os
import socket
import struct

from tools.serve_recovery import journal, ledger, store


def address(service_id):
    ledger.require(ledger.identifier(service_id), 'invalid outer service identity')
    return f'\0q4t-outer-{os.geteuid()}-{service_id}'


def request(service_id, operation, *, timeout=3):
    ledger.require(operation in ('status', 'stop'), 'invalid outer control operation')
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        sock.settimeout(timeout); sock.connect(address(service_id))
        peer = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        ledger.require(peer[1] == os.geteuid(), 'outer control peer UID mismatch')
        journal.send(sock, dict(version=1, service_id=service_id, op=operation))
        value = journal.receive(sock)
        ledger.require(type(value) is dict and value.get('version') == 1 and
            value.get('service_id') == service_id and value.get('outer_pid') == peer[0],
            'outer control reply identity changed')
        return value


class Server:
    def __init__(self, service_id, handler):
        self.service_id, self.handler = service_id, handler
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.sock.bind(address(service_id)); self.sock.listen(8); self.sock.setblocking(False)
        except BaseException:
            self.sock.close(); raise

    def tick(self):
        for _ in range(4):
            try: connection, _ = self.sock.accept()
            except BlockingIOError: return
            with connection:
                try:
                    connection.settimeout(.05)
                    peer = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    ledger.require(peer[1] == os.geteuid(), 'outer operator UID mismatch')
                    raw, _, flags, _ = connection.recvmsg(512)
                    ledger.require(not flags & socket.MSG_TRUNC, 'outer control request bound')
                    value = store.decode(raw)
                    ledger.keys(value, ('version', 'service_id', 'op'))
                    ledger.require(type(value['version']) is int and value['version'] == 1 and
                        value['service_id'] == self.service_id and value['op'] in ('status', 'stop'),
                        'invalid outer control request')
                    reply = self.handler(value['op'])
                    journal.send(connection, dict(reply, version=1, service_id=self.service_id,
                                                  outer_pid=os.getpid()))
                except (OSError, ValueError, KeyError, TypeError):
                    # A failed auxiliary client never signals or restarts work.
                    continue

    def close(self): self.sock.close()
