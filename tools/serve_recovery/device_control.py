"""Bounded root-only native device control, never a running-model dependency.

The caller holds its deployment lock and the shared GPU lease. A stuck adapter
retains that lease and this process's worker slot; it cannot silently start a
replacement adapter. An entry/setup failure does not signal any model.
"""
from array import array
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import subprocess
import time

from tools.serve_recovery import device_store, ledger, store

MAXIMUM = 65536


@dataclass
class Reply:
    result: object
    fds: list

    def close(self):
        for fd in self.fds: os.close(fd)
        self.fds.clear()

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


class Control:
    def __init__(self, executable, lease_fd, lease_path):
        device_store.privileged()
        self.executable = Path(executable).resolve(strict=True)
        self.lease_fd, self.lease_path = lease_fd, str(lease_path)
        self.pending = None

    def call(self, operation, args, *, inherited=(), timeout=5):
        device_store.privileged()
        ledger.require(operation in ('attach', 'adopt', 'recover', 'remove', 'issue') and
                       0 < timeout <= 5, 'invalid bounded device operation')
        ledger.require(self.pending is None or self.pending.poll() is not None,
                       'device control worker is not reaped')
        expected = 0 if operation == 'remove' else 2
        command = ledger.canonical(dict(version=1, op=operation,
            lease_fd=self.lease_fd, lease_path=self.lease_path, args=args))
        ledger.require(len(command) <= MAXIMUM, 'device control request too large')
        parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        descriptors = []
        end = time.monotonic()+timeout
        try:
            # Queue before exec: the native child receives once without a
            # blocking read and never races the first request against startup.
            parent.settimeout(timeout)
            ledger.require(parent.send(command) == len(command), 'short control command')
            self.pending = subprocess.Popen([str(self.executable), '--fd', str(endpoint.fileno())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={}, pass_fds=tuple(set((self.lease_fd, endpoint.fileno(), *inherited))))
            endpoint.close()
            raw, ancillary, flags, _ = parent.recvmsg(MAXIMUM, socket.CMSG_SPACE(4*4),
                                                     socket.MSG_CMSG_CLOEXEC)
            invalid_ancillary = False
            for level, kind, data in ancillary:
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                    invalid_ancillary = True
                    continue
                numbers = array('i')
                size = numbers.itemsize
                numbers.frombytes(data[:len(data)//size*size])
                descriptors.extend(numbers)
                if len(data) % size: invalid_ancillary = True
            ledger.require(raw and not invalid_ancillary and
                not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC),
                'truncated or missing device control reply')
            value = store.decode(raw)
            ledger.require(type(value) is dict and type(value.get('ok')) is bool,
                           'invalid device control reply')
            code = self.pending.wait(timeout=max(0.001, end-time.monotonic()))
            self.pending = None
            if not value['ok']:
                ledger.keys(value, ('ok', 'error'))
                ledger.require(False, 'device control rejected: '+str(value['error'])[:2048])
            ledger.keys(value, ('ok', 'result'))
            ledger.require(code == 0 and len(descriptors) == expected,
                           'device control exit or descriptor mismatch')
            reply = Reply(value['result'], descriptors)
            descriptors = []
            return reply
        finally:
            parent.close(); endpoint.close()
            for fd in descriptors: os.close(fd)
            if self.pending is not None:
                if self.pending.poll() is None:
                    self.pending.kill()  # Unreaped direct child; never another service.
                    try: self.pending.wait(timeout=1)
                    except subprocess.TimeoutExpired: pass
                if self.pending.poll() is not None: self.pending = None
                # If unreaped, keep this object AND the inherited lease slot.
                # The explicit administrative caller must stop this operation.
