"""Bounded model policy IPC. Observation/publication never authorizes recovery."""
import os
import re
import select
import socket
import struct
import time

from tools.serve_gateway import client_policy as cp
from tools.serve_recovery import journal, ledger, quota_policy, store


def address(service_id, generation):
    ledger.require(ledger.identifier(service_id) and ledger.integer(generation, minimum=1),
                   'invalid model policy channel identity')
    return 'q4t-client-policy-' + str(os.geteuid()) + '-' + service_id + '-' + str(generation)


def validate_status(value):
    ledger.keys(value, ('profile', 'configured', 'mutable', 'publisher_alive', 'revision',
        'sha256', 'content_sha256', 'global_inflight', 'global_queued',
        'credential_values_redacted', 'clients'))
    ledger.require(value['profile'] == 'q4t-native-client-policy-v1' and
        all(type(value[k]) is bool for k in ('configured', 'mutable', 'publisher_alive')) and
        value['credential_values_redacted'] is True and quota_policy.revision(value['revision']) and
        ledger.integer(value['global_inflight'], minimum=1) and value['global_inflight'] <= 2**31-1 and
        ledger.integer(value['global_queued']) and value['global_queued'] <= 2**31-1,
        'invalid model policy status')
    ids = value['clients']
    ledger.require(type(ids) is list and len(ids) <= 64 and
        all(type(i) is str and re.fullmatch('[a-z][a-z0-9_-]{0,31}', i) for i in ids) and
        len(ids) == len(set(ids)), 'invalid model policy client inventory')
    for field in ('sha256', 'content_sha256'):
        ledger.require(type(value[field]) is str and
            (bool(ledger.HASH.fullmatch(value[field])) if value['configured'] else value[field] == ''),
            'invalid model policy digest')
    ledger.require(bool(ids) == value['configured'] and
        (value['configured'] or (not value['mutable'] and value['revision'] == 0)),
        'contradictory model policy status')
    return value


class Channel:
    """Same-UID read-only by default; only the model's pinned parent can publish."""
    def __init__(self, name, *, clock=time.monotonic):
        ledger.require(type(name) is str and 0 < len(name.encode()) < 108 and '\0' not in name,
                       'invalid model policy channel name')
        self.address = '\0' + name
        self.clock = clock
        self.socket = self.pidfd = None
        self.owner = None
        self.connecting = False
        self.phase = None
        self.deadline = self.next_poll = 0
        self.wanted = None
        self.digest = ''
        self.raw = b''
        self.pending = None
        self.offset = 0
        self.observed = self.observed_at = None
        self.error = 'client_policy_status_unknown'

    def desired(self, durable, token):
        value = quota_policy.runtime(durable, token)
        raw = ledger.canonical(value)
        digest = cp.digest(value)
        if raw == self.raw:return
        self.wanted, self.digest, self.raw = value, digest, raw
        self.next_poll = 0

    def close(self):
        if self.socket is not None:self.socket.close()
        if self.pidfd is not None:os.close(self.pidfd)
        self.socket = self.pidfd = None
        self.phase = self.pending = None

    def connect(self, phase):
        pid, start = self.owner
        self.pidfd = os.pidfd_open(pid)
        ledger.require(journal.process_start(pid) == start and
            not select.select([self.pidfd], [], [], 0)[0], 'model policy owner changed')
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.socket.setblocking(False)
        try:
            self.socket.connect(self.address);self.connecting = False
        except BlockingIOError:self.connecting = True
        self.phase = phase;self.deadline = self.clock() + 2.5

    def peer(self):
        ledger.require(not select.select([self.pidfd], [], [], 0)[0], 'model policy owner exited')
        if self.connecting:
            if not select.select([], [self.socket], [], 0)[1]:return False
            ledger.require(self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0,
                           'model policy connection failed')
            self.connecting = False
        pid, uid, _ = struct.unpack('3i', self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        ledger.require(pid == self.owner[0] and uid == os.geteuid(), 'model policy peer changed')
        return True

    def send(self, raw):
        ledger.require(self.socket.send(raw) == len(raw), 'model policy packet truncated')

    def reply(self):
        raw, ancillary, flags, _ = self.socket.recvmsg(8192)
        ledger.require(raw and not ancillary and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC),
                       'invalid model policy frame')
        value = store.decode(raw);ledger.keys(value, ('ok', 'error', 'status'))
        ledger.require(type(value['ok']) is bool and type(value['error']) is str and len(value['error']) <= 128,
                       'invalid model policy response')
        self.observed = validate_status(value['status']);self.observed_at = self.clock()
        self.error = '' if value['ok'] else 'client_policy_publication_refused'
        return value['ok'], self.observed

    def status(self):
        fresh = self.observed_at is not None and 0 <= self.clock() - self.observed_at <= 3
        observed = self.observed if fresh else None
        return dict(desired_revision=None if self.wanted is None else self.wanted['revision'],
            desired_sha256=self.digest, observed=observed,
            live_applied=bool(not self.error and self.wanted is not None and observed and
                observed['configured'] and observed['mutable'] and
                observed['revision'] == self.wanted['revision'] and
                observed['content_sha256'] == self.digest), error=self.error,
            publication_pending=self.pending is not None)

    def step(self, pid, start_ticks):
        now = self.clock()
        try:
            ledger.require(ledger.integer(pid, minimum=2) and ledger.integer(start_ticks, minimum=1),
                           'invalid model policy owner')
            owner = (pid, start_ticks)
            if self.owner != owner:
                self.close();self.owner = owner;self.observed = self.observed_at = None
                self.error = 'client_policy_status_unknown';self.next_poll = 0
            if self.socket is None:
                if now < self.next_poll:return self.status()
                self.connect('status_send')
            ledger.require(now < self.deadline, 'model policy timeout')
            if not self.peer():return self.status()
            for _ in range(8):
                if self.phase == 'status_send':
                    self.send(b'{"op":"status"}');self.phase = 'status_reply'
                elif self.phase == 'status_reply':
                    ok, observed = self.reply()
                    if not ok or self.wanted is None or self.status()['live_applied']:
                        self.close();self.next_poll = now + .25;break
                    if not observed['mutable'] or not observed['publisher_alive']:
                        self.error = 'client_policy_publisher_unavailable'
                        self.close();self.next_poll = now + 1;break
                    if observed['revision'] >= self.wanted['revision']:
                        self.error = 'client_policy_durable_revision_conflict'
                        self.close();self.next_poll = now + 1;break
                    quota_policy.capacity(dict(self.wanted, gateway_token=quota_policy.TOKEN),
                        observed['global_inflight'], observed['global_queued'])
                    self.close();self.connect('publish_header')
                    self.pending = dict(raw=self.raw, sha256=self.digest, expected_revision=observed['revision'])
                    self.offset = 0;break
                elif self.phase == 'publish_header':
                    self.send(ledger.canonical(dict(op='publish', bytes=len(self.pending['raw']),
                        sha256=self.pending['sha256'], expected_revision=self.pending['expected_revision'])))
                    self.phase = 'publish_body'
                elif self.phase == 'publish_body':
                    part = self.pending['raw'][self.offset:self.offset+16384]
                    self.send(part);self.offset += len(part)
                    if self.offset == len(self.pending['raw']):self.phase = 'publish_reply'
                elif self.phase == 'publish_reply':
                    self.reply();self.close();self.next_poll = now + .25;break
        except BlockingIOError:pass
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            self.close();self.next_poll = now + 1;self.error = 'client_policy_status_unknown'
        return self.status()
