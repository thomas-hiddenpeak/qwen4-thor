"""Bounded local FD transport for the optional privileged cold issuer.

The kernel identifies peers; no path, PID string or JSON receipt is a permit.
Only a single worker tree exists. Timeout/disconnection does not return its
slot or close its held lease until waitid proves every owned child reaped.
"""
from array import array
from dataclasses import dataclass
import os
import select
import signal
import socket
import struct
import subprocess
import time

from tools.serve_recovery import device_control, ledger, outer_tree, store

MAXIMUM = 16384
WORKER_SECONDS = 40
CLIENT_SECONDS = 45


def address(host_id):
    ledger.require(ledger.identifier(host_id), 'cold channel host identity required')
    return '\0q4t-cold-'+host_id


def credentials(sock):
    ledger.require(sock.family == socket.AF_UNIX and
        sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_SEQPACKET,
        'cold channel requires local sequenced packets')
    return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))


def send(sock, value, fds=()):
    raw = ledger.canonical(value)
    ledger.require(0 < len(raw) <= MAXIMUM and len(fds) <= 4, 'cold channel message bound')
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array('i', fds))] if fds else []
    ledger.require(sock.sendmsg([raw], ancillary) == len(raw), 'short cold channel packet')


def receive(sock, *, count=None, peer=None):
    fds = []
    try:
        size = socket.CMSG_SPACE(5*4) + socket.CMSG_SPACE(12)
        raw, ancillary, flags, _ = sock.recvmsg(MAXIMUM, size, socket.MSG_CMSG_CLOEXEC)
        invalid = False; actual_peer = None
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
                if actual_peer is not None or len(data) != 12: invalid = True
                else: actual_peer = struct.unpack('3i', data)
                continue
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                invalid = True; continue
            numbers = array('i'); size = numbers.itemsize
            numbers.frombytes(data[:len(data)//size*size]); fds.extend(numbers)
            invalid |= len(data) % size != 0
        if not raw: raise ConnectionResetError('cold channel peer closed before its reply')
        ledger.require(raw and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) and
            not invalid and len(fds) <= 4 and (count is None or len(fds) == count) and
            (peer is None or actual_peer == peer),
            'invalid cold channel packet or descriptors')
        value = store.decode(raw)
        return device_control.Reply(value, fds)
    except BaseException:
        for fd in fds: os.close(fd)
        raise


def connect(host_id, *, timeout=CLIENT_SECONDS):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        sock.settimeout(timeout); sock.connect(address(host_id))
        ledger.require(credentials(sock)[1] == 0, 'cold issuer must be an actual root socket peer')
        return sock
    except BaseException: sock.close(); raise


def request(host_id, selected, lease_fd, instance_fd, *, tick=None, timeout=CLIENT_SECONDS):
    ledger.require(0 < timeout <= CLIENT_SECONDS, 'cold request deadline bound')
    with connect(host_id, timeout=timeout) as sock:
        send(sock, dict(version=1, operation='issue', request=selected), (lease_fd, instance_fd))
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            if tick is not None: tick()
            if not select.select([sock], [], [], min(.05, max(0, end-time.monotonic())))[0]: continue
            reply = receive(sock)
            try:
                value = reply.result
                ledger.require(type(value) is dict and type(value.get('ok')) is bool,
                               'cold issuer response differs')
                if not value['ok']:
                    ledger.keys(value, ('ok', 'reason'))
                    ledger.require(not reply.fds, 'failed cold issuer returned capabilities')
                    raise ledger.LedgerError('cold admission unavailable: '+str(value['reason'])[:256])
                ledger.keys(value, ('ok', 'result'))
                ledger.require(len(reply.fds) == 4 and
                    value['result']['request_sha256'] == ledger.digest(selected),
                    'cold issuer request or FD receipt differs')
                reply.result = value['result']; return reply
            except BaseException: reply.close(); raise
        raise TimeoutError('cold issuer deadline; lost request is never replayed')


@dataclass
class Worker:
    tree: object
    socket: object
    held: list
    deadline: float
    response: object = None
    failure: str | None = None
    sent: object = None

    @classmethod
    def start(cls, command, value, held, *, timeout=WORKER_SECONDS):
        """Dedicated issuer process only; command is from its protected source."""
        ledger.require(0 < timeout <= WORKER_SECONDS, 'cold worker deadline bound')
        tree = outer_tree.Tree().__enter__(); parent = endpoint = None; copies = []
        try:
            parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            for fd in held: copies.append(os.dup(fd))
            envelope = dict(value, descriptors=copies)
            send(parent, envelope); parent.setblocking(False)
            tree.launch([*command, '--worker-fd', str(endpoint.fileno())],
                env=dict(PATH='/usr/bin:/bin', LANG='C.UTF-8'),
                pass_fds=(*copies, endpoint.fileno()), stdio=subprocess.DEVNULL)
            endpoint.close(); endpoint = None
            return cls(tree, parent, copies, time.monotonic()+timeout, sent=set())
        except BaseException:
            if parent is not None: parent.close()
            if endpoint is not None: endpoint.close()
            # A successful fork owns all descriptors until its tree is empty.
            if tree.root is not None:
                worker = cls(tree, None, copies, time.monotonic(), failure='dispatch_unknown', sent=set())
                return worker
            for fd in copies: os.close(fd)
            tree.close(); raise

    def poll(self):
        """Return completion only after actual ECHILD, including helper children."""
        if self.socket is not None and self.response is None and self.failure is None:
            try: self.response = receive(self.socket)
            except BlockingIOError: pass
            except (OSError, ValueError, KeyError, TypeError): self.failure = 'worker_reply_unknown'
        if time.monotonic() >= self.deadline and self.failure is None:
            self.failure = 'worker_timeout'
        done = self.tree.reap()
        if done:
            if self.tree.root.returncode != 0 and self.failure is None:
                self.failure = 'worker_failed'
            if self.response is None and self.failure is None: self.failure = 'worker_reply_missing'
            return True
        if self.failure is not None:
            # Own direct/adopted helper children only. The model/outer peer is
            # never a member of this dedicated issuer's subreaper tree.
            self.tree.signal_children(signal.SIGKILL, self.sent)
        return False

    def close(self):
        ledger.require(outer_tree.no_children(), 'cold worker tree still owns its slot and lease')
        self.tree.close()
        if self.socket is not None: self.socket.close(); self.socket = None
        if self.response is not None: self.response.close(); self.response = None
        for fd in self.held: os.close(fd)
        self.held.clear()
