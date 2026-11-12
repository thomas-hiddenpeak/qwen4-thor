"""Bounded native usage IPC. No filesystem calls, signals or model restarts."""
import hashlib
import socket

from tools.serve_recovery import ledger, quota_control, store, usage_policy


def validate(value):
    ledger.keys(value, ('recovered', 'frozen', 'receipt_sha256', 'snapshot'))
    ledger.require(type(value['recovered']) is bool and type(value['frozen']) is bool and
        type(value['receipt_sha256']) is str and
        (bool(ledger.HASH.fullmatch(value['receipt_sha256'])) if value['recovered'] else
         value['receipt_sha256'] == '') and (not value['frozen'] or value['recovered']),
        'invalid native usage state')
    if value['snapshot'] is not None:
        usage_policy.snapshot(value['snapshot'])
        ledger.require(value['frozen'] and value['recovered'], 'unsealed native usage snapshot')
    return value


class Channel(quota_control.Channel):
    def __init__(self, name, **options):
        super().__init__(name, **options)
        self.operation = 'usage_status'
        self.raw = b''
        self.digest = ''
        self.ok = False

    def request(self, operation, snapshot=None):
        ledger.require(operation in ('usage_status', 'usage_restore', 'usage_freeze'),
                       'invalid native usage operation')
        if snapshot is not None:usage_policy.snapshot(snapshot)
        ledger.require(operation == 'usage_restore' or snapshot is None, 'unexpected usage input')
        raw = ledger.canonical(snapshot) if operation == 'usage_restore' else b''
        if (operation, raw) == (self.operation, self.raw):return
        self.close()
        self.operation, self.raw = operation, raw
        self.digest = hashlib.sha256(raw).hexdigest() if raw else ''
        self.observed = self.observed_at = None;self.ok = False
        self.next_poll = 0

    def reply(self):
        raw, ancillary, flags, _ = self.socket.recvmsg(65536)
        ledger.require(raw and not ancillary and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC),
                       'invalid native usage reply')
        value = store.decode(raw);ledger.keys(value, ('ok', 'error', 'usage'))
        ledger.require(type(value['ok']) is bool and type(value['error']) is str and
                       len(value['error']) <= 128, 'invalid native usage response')
        self.observed = validate(value['usage']);self.observed_at = self.clock()
        self.ok = value['ok'];self.error = value['error']

    def status(self):
        fresh = self.observed_at is not None and 0 <= self.clock()-self.observed_at <= 3
        value = self.observed if fresh else None
        applied = bool(value and self.ok)
        if self.operation == 'usage_restore':
            applied = bool(applied and value['recovered'] and value['receipt_sha256'] == self.digest)
        elif self.operation == 'usage_freeze':
            applied = bool(applied and value['frozen'] and value['snapshot'] is not None)
        return dict(operation=self.operation, observed=value, applied=applied,
                    error=self.error, receipt_sha256=self.digest)

    def step(self, pid, start_ticks):
        now = self.clock()
        try:
            ledger.require(ledger.integer(pid, minimum=2) and ledger.integer(start_ticks, minimum=1),
                           'invalid native usage owner')
            owner = (pid, start_ticks)
            if self.owner != owner:
                self.close();self.owner = owner
                self.observed = self.observed_at = None;self.ok = False;self.next_poll = 0
            if self.socket is None:
                if now < self.next_poll:return self.status()
                self.connect('header');self.offset = 0
            ledger.require(now < self.deadline, 'native usage timeout')
            if not self.peer():return self.status()
            for _ in range(8):
                if self.phase == 'header':
                    command = dict(op=self.operation)
                    if self.raw:command.update(bytes=len(self.raw), sha256=self.digest)
                    self.send(ledger.canonical(command))
                    self.phase = 'body' if self.raw else 'reply'
                elif self.phase == 'body':
                    part = self.raw[self.offset:self.offset+16384]
                    self.send(part);self.offset += len(part)
                    if self.offset == len(self.raw):self.phase = 'reply'
                elif self.phase == 'reply':
                    self.reply();self.close();self.next_poll = now + .1;break
        except BlockingIOError:pass
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            self.close();self.next_poll = now + 1
            self.ok = False;self.error = 'client_usage_status_unknown'
        return self.status()
