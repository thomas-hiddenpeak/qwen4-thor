"""Bounded isolated policy reader; never signals or restarts a model."""
import argparse
import hashlib
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import access_policy, access_store, journal, ledger, store

PACKET = 16384
TIMEOUT = 2
ERRORS = ('state_unavailable', 'state_invalid', 'writer_busy')


def family(kind):
    ledger.require(kind in ('access', 'limits', 'usage'), 'invalid policy reader family')
    if kind == 'access':return access_policy, access_store
    if kind == 'usage':
        from tools.serve_recovery import usage_policy, usage_store
        return usage_policy, usage_store
    # Old credential-only packages do not need the limits modules.
    from tools.serve_recovery import quota_policy, quota_store
    return quota_policy, quota_store


def claim_address(service_id, kind='access'):
    ledger.require(ledger.identifier(service_id) and kind in ('access', 'limits', 'usage'),
                   'invalid policy service identity')
    return '\0q4t-' + kind + '-io-' + str(os.geteuid()) + '-' + service_id


class Reader:
    def __init__(self, state, service_id, state_identity, *, clock=time.monotonic, kind='access'):
        ledger.require(isinstance(state, (str, Path)) and Path(state).is_absolute() and
            ledger.identifier(service_id) and type(state_identity) is list and
            len(state_identity) == 2 and all(ledger.integer(n, minimum=1) for n in state_identity),
            'invalid access reader binding')
        self.state, self.service_id, self.state_identity = str(state), service_id, state_identity
        self.contract, _ = family(kind)
        self.kind = kind
        self.clock = clock
        self.process = self.socket = self.claim = None
        self.starts = []
        self.started = self.reaped = 0
        self.next_start = self.next_read = 0
        self.deadline = 0
        self.sequence = 0
        self.completed_sequence = 0
        self.phase = None
        self.killed = self.closed = self.available = False
        self.stopping = False
        self.error = 'state_unavailable'
        self.value = None
        self.payload = bytearray()
        self.expected_bytes = 0
        self.expected_digest = ''
        self.request = b''

    def disable(self):
        self.stopping = True
        self.available = False
        self.value = None
        self.payload.clear()
        if self.socket is not None:
            self.socket.close();self.socket = None
        if self.process is not None and not self.killed:
            try:
                # Popen retains the direct child identity until actual waitpid.
                self.process.kill()
                self.killed = True
            except OSError:
                # An unconfirmed signal cannot free the slot or its claim.
                self.error = 'reader_reap_pending'
        self.phase = None

    def start(self):
        now = self.clock()
        self.starts = [t for t in self.starts if now - t < 60]
        if len(self.starts) >= 3:
            self.error = 'reader_budget';self.next_start = self.starts[0] + 60
            return
        parent = child = claim = None
        try:
            # Bind before exec, including before Python can touch source files.
            # An orphan that has not loaded its module still retains this FD.
            claim = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            claim.bind(claim_address(self.service_id, self.kind))
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            parent.setblocking(False)
            self.starts.append(now)
            args = [sys.executable, '-I', '-B', str(ROOT / 'tools/serve_recovery/access_io.py'),
                '--worker-fd', str(child.fileno()), '--claim-fd', str(claim.fileno()),
                '--parent', str(os.getpid()), '--parent-start', str(journal.process_start(os.getpid())),
                '--state', self.state, '--service-id', self.service_id,
                '--state-device', str(self.state_identity[0]), '--state-inode', str(self.state_identity[1])]
            if self.kind != 'access':args += ['--kind', self.kind]
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, close_fds=True,
                pass_fds=(child.fileno(), claim.fileno()), start_new_session=True)
            self.socket, self.claim = parent, claim
            parent = claim = None
            self.started += 1;self.killed = self.stopping = False;self.sequence = 0
            self.completed_sequence = 0
            self.phase = 'starting';self.deadline = now + TIMEOUT
        except (OSError, ValueError, RuntimeError):
            self.error = 'reader_start_failed';self.next_start = now + 1
            self.disable()
        finally:
            for owned in (parent, child, claim):
                if owned is not None:owned.close()

    def packet(self):
        raw, ancillary, flags, _ = self.socket.recvmsg(PACKET)
        ledger.require(raw and not ancillary and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC),
                       'invalid access reader frame')
        return raw

    def receive(self):
        for _ in range(8):
            raw = self.packet()
            if self.phase == 'starting':
                ledger.require(store.decode(raw) == dict(sequence=0, status='READY'),
                               'invalid access reader startup')
                self.phase = 'idle';self.next_read = 0
                return
            if self.phase == 'header':
                ledger.require(len(raw) <= 1024, 'reader header bound')
                header = store.decode(raw)
                ledger.keys(header, ('sequence', 'status', 'bytes', 'sha256', 'error'))
                ledger.require(type(header['sequence']) is int and header['sequence'] == self.sequence and
                    header['status'] in ('OK', 'ERROR') and ledger.integer(header['bytes']) and
                    header['bytes'] <= self.contract.MAX_BYTES, 'invalid reader reply')
                if header['status'] == 'ERROR':
                    ledger.require(header['bytes'] == 0 and header['sha256'] == '' and
                        header['error'] in ERRORS, 'invalid reader error')
                    self.available = False;self.value = None;self.error = header['error']
                    self.phase = 'idle';self.next_read = self.clock() + 1
                    return
                ledger.require(header['bytes'] > 0 and header['error'] == '' and
                    type(header['sha256']) is str and ledger.HASH.fullmatch(header['sha256']),
                    'invalid reader payload header')
                self.expected_bytes, self.expected_digest = header['bytes'], header['sha256']
                self.payload.clear();self.phase = 'body'
            elif self.phase == 'body':
                ledger.require(len(self.payload) + len(raw) <= self.expected_bytes, 'reader byte bound')
                self.payload.extend(raw)
                if len(self.payload) == self.expected_bytes:
                    ledger.require(hashlib.sha256(self.payload).hexdigest() == self.expected_digest,
                                   'reader payload digest changed')
                    self.value = self.contract.validate(store.decode(self.payload))
                    self.completed_sequence = self.sequence
                    self.payload.clear();self.available = True;self.error = ''
                    self.phase = 'idle';self.next_read = self.clock() + 1
                    return
            else:
                raise ledger.LedgerError('unsolicited access reader reply')

    def step(self, *, allow_start=True):
        now = self.clock()
        try:
            if self.process is not None and self.process.poll() is not None:
                if not self.stopping and not self.closed:self.error = 'reader_exit'
                self.reaped += 1;self.process = None
                if self.claim is not None:self.claim.close();self.claim = None
                self.disable();self.next_start = now + 1
            if self.process is None:
                if not self.closed and allow_start and now >= self.next_start:self.start()
                return
            if self.closed or self.stopping:
                self.disable();return
            if self.phase != 'idle' and now >= self.deadline:
                self.error = 'reader_timeout';self.disable();return
            if self.phase not in ('idle', 'send'):
                self.receive()
            if self.phase == 'idle' and now >= self.next_read:
                ledger.require(self.sequence < ledger.MAX_INTEGER, 'access reader sequence exhausted')
                self.sequence += 1
                self.request = ledger.canonical(dict(self.command(), sequence=self.sequence))
                ledger.require(len(self.request) <= 65536, 'reader request byte bound')
                self.phase = 'send';self.deadline = now + TIMEOUT
            if self.phase == 'send':
                ledger.require(self.socket.send(self.request) == len(self.request),
                               'access reader request truncated')
                self.phase = 'header'
        except BlockingIOError:
            pass
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            self.error = 'reader_unknown';self.disable()

    def command(self):
        return dict(op='load')

    def close(self):
        self.closed = True
        self.disable();self.step()
        return self.process is None

    def status(self):
        return dict(available=self.available, error=self.error, started=self.started,
            reaped=self.reaped, pending=self.phase in ('starting', 'send', 'header', 'body'),
            worker_unreaped=bool(self.stopping and self.process is not None),
            model_actions=False)


def worker(args):
    kind = getattr(args, 'kind', 'access')
    _, writer = family(kind)
    with socket.socket(fileno=args.worker_fd) as channel, socket.socket(fileno=args.claim_fd) as claim:
        journal.verify_parent(channel, args.parent, args.parent_start)
        ledger.require(claim.family == socket.AF_UNIX and claim.type == socket.SOCK_DGRAM and
            claim.getsockname() == claim_address(args.service_id, kind).encode(), 'reader claim changed')
        channel.settimeout(3)
        channel.send(ledger.canonical(dict(sequence=0, status='READY')))
        sequence = 0
        while True:
            # Idle reads need no timeout; the parent-death handler remains active.
            channel.settimeout(None)
            raw, ancillary, flags, _ = channel.recvmsg(65536 if kind == 'usage' else 1024)
            if not raw:return
            ledger.require(not ancillary and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC),
                           'invalid reader request')
            request = store.decode(raw)
            if kind != 'usage':
                ledger.keys(request, ('sequence', 'op'))
                ledger.require(request['op'] == 'load', 'invalid reader operation')
            ledger.require(type(request) is dict and type(request.get('sequence')) is int and
                request['sequence'] == sequence + 1, 'reader request sequence')
            sequence += 1
            error, payload = '', b''
            try:
                state = Path(args.state)
                info = state.stat(follow_symlinks=False)
                ledger.require(state.resolve(strict=True) == state and
                    [info.st_dev, info.st_ino] == [args.state_device, args.state_inode] and
                    stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
                    stat.S_IMODE(info.st_mode) == 0o700,
                    'reader state identity changed')
                value = dict(state_directory=str(state), service_id=args.service_id,
                             state_identity=[args.state_device, args.state_inode])
                if kind == 'usage':
                    payload = ledger.canonical(writer.execute(value, request))
                else:
                    with writer.open_bound(value) as journal_store:
                        payload = ledger.canonical(journal_store.head['state']['policy'])
            except BlockingIOError:
                error = 'writer_busy'
            except (OSError, RuntimeError):
                error = 'state_unavailable'
            except (ValueError, KeyError, TypeError):
                error = 'state_invalid'
            channel.settimeout(3)
            header = dict(sequence=sequence, status='ERROR' if error else 'OK',
                bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest() if payload else '', error=error)
            channel.send(ledger.canonical(header))
            for offset in range(0, len(payload), PACKET):
                part = payload[offset:offset + PACKET]
                ledger.require(channel.send(part) == len(part), 'reader response truncated')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('worker-fd', 'claim-fd', 'parent', 'parent-start', 'state-device', 'state-inode'):
        parser.add_argument('--' + name, required=True, type=int)
    parser.add_argument('--state', required=True)
    parser.add_argument('--service-id', required=True)
    parser.add_argument('--kind', choices=('access', 'limits', 'usage'), default='access')
    worker(parser.parse_args())


if __name__ == '__main__':
    main()
