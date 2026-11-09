"""Nonblocking parent-to-native credential synchronization; no disk or model I/O."""
import os
import select
import socket
import struct
import time

from tools.serve_recovery import access_policy, ledger, store


class Publisher:
    def __init__(self, address, *, clock=time.monotonic):
        self.address = '\0' + address + '-access'
        self.clock = clock
        self.socket = None
        self.phase = None
        self.connecting = False
        self.deadline = self.next_poll = 0
        self.wanted = None
        self.digest = ''
        self.raw = b''
        self.offset = 0
        self.pending = None
        self.observed = None
        self.observed_at = None
        self.error = 'access_status_unknown'
        self.child_pid = None

    def desired(self, policy):
        digest = access_policy.digest(policy)
        if digest == self.digest:
            return
        # Freeze an in-flight publication until its reply; the next poll sends
        # any later desired revision. No interleaving fragments of snapshots.
        self.wanted = store.decode(ledger.canonical(policy))
        self.digest = digest
        self.raw = ledger.canonical(self.wanted)
        self.next_poll = 0

    def close(self):
        if self.socket is not None:
            self.socket.close()
        self.socket = None
        self.phase = None
        self.pending = None

    def connect(self, child_pid, phase):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.socket.setblocking(False)
        try:
            self.socket.connect(self.address)
            self.connecting = False
        except BlockingIOError:
            self.connecting = True
        self.child_pid = child_pid
        self.phase = phase
        self.deadline = self.clock() + 2.5

    def peer(self):
        if self.connecting:
            if not select.select([], [self.socket], [], 0)[1]:
                return False
            ledger.require(self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0,
                           'access connection failed')
            self.connecting = False
        pid, uid, _ = struct.unpack('3i', self.socket.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        ledger.require(pid == self.child_pid and uid == os.geteuid(), 'access peer changed')
        return True

    def send(self, raw):
        ledger.require(self.socket.send(raw) == len(raw), 'access packet truncated')

    def reply(self):
        raw, ancillary, flags, _ = self.socket.recvmsg(4096)
        ledger.require(raw and not ancillary and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC),
                       'invalid access status frame')
        value = store.decode(raw)
        ledger.keys(value, ('ok', 'error', 'status'))
        status = value['status']
        ledger.require(type(value['ok']) is bool and type(value['error']) is str and
            type(status) is dict and type(status.get('configured')) is bool,
            'invalid access status')
        ledger.keys(status, ('configured', 'revision', 'sha256', 'clients', 'active_keys',
                            'revoked_clients', 'history_keys', 'credential_values_redacted'))
        ledger.require(access_policy.revision(status['revision']) and
            type(status['sha256']) is str and
            ((status['configured'] and ledger.HASH.fullmatch(status['sha256'])) or
             (not status['configured'] and status['sha256'] == '')) and
            all(ledger.integer(status[n]) for n in ('clients', 'active_keys',
                'revoked_clients', 'history_keys')) and status['credential_values_redacted'] is True,
            'invalid access status projection')
        ledger.require(status['clients'] <= access_policy.MAX_CLIENTS and
            status['active_keys'] <= access_policy.MAX_CLIENTS * access_policy.MAX_KEYS and
            status['history_keys'] <= access_policy.MAX_HISTORY and
            status['revoked_clients'] <= status['clients'] and
            (status['clients'] > 0 if status['configured'] else
                status['revision'] == status['clients'] == status['active_keys'] ==
                status['history_keys'] == 0), 'invalid access status bounds')
        self.observed, self.observed_at = status, self.clock()
        self.error = '' if value['ok'] else 'access_publication_refused'
        return value['ok'], status

    def status(self):
        fresh = self.observed_at is not None and 0 <= self.clock() - self.observed_at <= 3
        observed = self.observed if fresh else None
        return dict(desired_revision=None if self.wanted is None else self.wanted['revision'],
            desired_sha256=self.digest, observed=observed, live_applied=bool(not self.error and observed and
                observed['configured'] and observed['sha256'] == self.digest and
                self.wanted is not None and observed['revision'] == self.wanted['revision']),
            error=self.error, publication_pending=self.pending is not None)

    def step(self, child_pid):
        now = self.clock()
        try:
            ledger.require(ledger.integer(child_pid, minimum=2), 'invalid access child')
            if self.child_pid is not None and child_pid != self.child_pid:
                self.close();self.observed = self.observed_at = None
                self.error = 'access_status_unknown';self.next_poll = 0
            if self.socket is None:
                if now < self.next_poll:
                    return self.status()
                self.connect(child_pid, 'status_send')
            ledger.require(now < self.deadline, 'access publication timeout')
            if not self.peer():
                return self.status()
            for _ in range(8):
                if self.phase == 'status_send':
                    self.send(b'{"op":"status"}')
                    self.phase = 'status_reply'
                elif self.phase == 'status_reply':
                    ok, observed = self.reply()
                    if not ok or self.wanted is None or self.status()['live_applied']:
                        self.close();self.next_poll = now + .25
                        break
                    if observed['configured'] and observed['revision'] >= self.wanted['revision']:
                        self.error = 'access_durable_revision_conflict'
                        self.close();self.next_poll = now + 1
                        break
                    self.close()
                    self.connect(child_pid, 'publish_header')
                    self.pending = dict(raw=self.raw, sha256=self.digest,
                        expected_revision=observed['revision'], initialize=not observed['configured'])
                    self.offset = 0
                    break
                elif self.phase == 'publish_header':
                    header = {k: self.pending[k] for k in ('sha256', 'expected_revision', 'initialize')}
                    self.send(ledger.canonical(dict(op='publish', bytes=len(self.pending['raw']), **header)))
                    self.phase = 'publish_body'
                elif self.phase == 'publish_body':
                    part = self.pending['raw'][self.offset:self.offset + 16384]
                    self.send(part);self.offset += len(part)
                    if self.offset == len(self.pending['raw']):
                        self.phase = 'publish_reply'
                elif self.phase == 'publish_reply':
                    self.reply()
                    self.close();self.next_poll = now + .25
                    break
        except BlockingIOError:
            pass
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            self.close();self.next_poll = now + 1
            self.error = 'access_status_unknown'
        return self.status()
