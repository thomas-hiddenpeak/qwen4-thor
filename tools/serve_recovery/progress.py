"""Private native completion observations; absence is never execution failure.

All timestamps are CLOCK_MONOTONIC on the bound boot/PID namespace. The
publisher's sequence is freshness evidence, not model work. No request data,
tokens, credentials, filesystem writes, NVML calls or blocking receives occur.
"""
from dataclasses import dataclass
from pathlib import Path
import re
import socket
import struct
import time

NS = 1_000_000_000
HEADER = struct.Struct('<8s13Q36s36s')
ROW = struct.Struct('<12Q')
CAPACITY = 1024
MAX_PACKET = HEADER.size + CAPACITY * ROW.size
FRESH_NS = 2 * NS
MAX_DRAIN = 8
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def require(condition, message):
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class Identity:
    pid: int
    start_ticks: int
    pid_namespace: int
    controller_generation: int
    model_generation: int
    boot_id: str
    service_id: str

    def __post_init__(self):
        for value in (self.pid, self.start_ticks, self.pid_namespace,
                      self.controller_generation, self.model_generation):
            require(type(value) is int and 0 < value < 1 << 64,
                    'invalid progress identity number')
        for value in (self.boot_id, self.service_id):
            require(type(value) is str and UUID.fullmatch(value) is not None,
                    'invalid progress identity UUID')

    @classmethod
    def owned(cls, owner, service_id, controller_generation, model_generation):
        return cls(owner['pid'], owner['start_ticks'],
                   Path(f"/proc/{owner['pid']}/ns/pid").stat().st_ino,
                   controller_generation, model_generation, owner['boot_id'],
                   service_id)


@dataclass(frozen=True)
class Work:
    ticket: int
    deadline_ns: int
    stage: int
    stage_since_ns: int
    wait: int
    wait_since_ns: int
    work: int
    work_id: int
    work_since_ns: int
    completed: int
    last_completed_ns: int
    updated_ns: int


@dataclass(frozen=True)
class Snapshot:
    sequence: int
    observed_ns: int
    complete: bool
    identity: Identity
    active: int
    untracked: int
    successes: int
    steps: int
    rows: tuple[Work, ...]
    wire_version: int = 1


def decode(packet):
    """Decode the explicit LE wire, not a host ABI structure or trusted JSON."""
    require(HEADER.size <= len(packet) <= MAX_PACKET, 'progress frame bound')
    (magic, sequence, observed, complete, pid, start, namespace, controller,
     model, active, untracked, count, successes, steps, boot, service) = \
        HEADER.unpack_from(packet)
    require(magic in (b'Q4TPR001', b'Q4TPR002') and sequence > 0 and observed > 0 and
            complete in (0, 1), 'invalid progress header')
    version = 1 if magic == b'Q4TPR001' else 2
    require(count <= CAPACITY and len(packet) == HEADER.size + count * ROW.size,
            'progress rows bound or trailing bytes')
    identity = Identity(pid, start, namespace, controller, model,
                        boot.decode('ascii'), service.decode('ascii'))
    require(not complete or (untracked == 0 and active == count),
            'contradictory complete progress')
    rows, tickets = [], set()
    for offset in range(HEADER.size, len(packet), ROW.size):
        row = Work(*ROW.unpack_from(packet, offset))
        require(0 < row.ticket < (1 << 64)-1 and row.ticket not in tickets,
                'invalid or duplicate request ticket')
        require(row.stage < (10 if version == 1 else 21) and row.wait < 10 and
                row.work < (8 if version == 1 else 9),
                'unknown progress phase')
        require(0 < row.stage_since_ns <= row.updated_ns and
                0 < row.wait_since_ns <= row.updated_ns <= observed,
                'invalid progress phase clock')
        require((row.completed == 0 and row.last_completed_ns == 0) or
                (row.completed > 0 and
                 0 < row.last_completed_ns <= row.updated_ns),
                'invalid actual completion clock')
        require((row.work_id == 0 and row.work == 0 and row.work_since_ns == 0)
                or (row.work_id > 0 and
                    0 < row.work_since_ns <= row.updated_ns),
                'invalid work identity or clock')
        rows.append(row)
        tickets.add(row.ticket)
    return Snapshot(sequence, observed, bool(complete), identity, active,
                    untracked, successes, steps, tuple(rows), version)


class Reader:
    """Owns one generation's receive FD; every poll has a fixed work bound."""
    def __init__(self, sock, identity):
        require(sock.family == socket.AF_UNIX and
                sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) ==
                socket.SOCK_SEQPACKET, 'private progress socket required')
        sock.setblocking(False)
        sock.set_inheritable(False)
        self.sock, self.identity = sock, identity
        self.last = None
        self.sequence = self.observed_ns = self.successes = self.steps = 0
        self.reason = 'awaiting_progress'
        self.ready_ns = None
        self.success_baseline = None
        self.row_marks = {}

    def unknown(self, reason):
        self.last, self.reason = None, reason

    def accept(self, packet, now_ns):
        value = decode(packet)
        require(value.identity == self.identity, 'progress identity mismatch')
        require(value.sequence > self.sequence and
                value.observed_ns > self.observed_ns,
                'progress replay or clock regression')
        require(0 <= now_ns-value.observed_ns <= FRESH_NS,
                'stale or future progress')
        require(value.successes >= self.successes and value.steps >= self.steps,
                'progress cumulative counters regressed')
        for row in value.rows:
            prior = self.row_marks.get(row.ticket)
            if prior is None:
                continue
            require(row.deadline_ns == prior.deadline_ns and
                    row.work_id >= prior.work_id and
                    row.completed >= prior.completed and
                    row.last_completed_ns >= prior.last_completed_ns and
                    row.updated_ns >= prior.updated_ns,
                    'request progress regressed')
            if row.work_id == prior.work_id:
                require(row.work_since_ns == prior.work_since_ns and
                        (row.work == 0 or row.work == prior.work),
                        'work identity reused')
            if row.completed == prior.completed:
                require(row.last_completed_ns == prior.last_completed_ns,
                        'completion timestamp without actual completion')
        self.sequence, self.observed_ns = value.sequence, value.observed_ns
        self.successes, self.steps = value.successes, value.steps
        if not value.complete:
            self.unknown('incomplete_progress')
            return
        self.last, self.reason = value, 'available'
        self.row_marks = {row.ticket: row for row in value.rows}
        if (self.ready_ns is not None and self.success_baseline is None and
                value.observed_ns >= self.ready_ns):
            # Capture AFTER self-check and ready publication. A delayed packet
            # from self-check cannot count as a successful external request.
            self.success_baseline = value.successes

    def poll(self, now_ns=None):
        supplied_clock = now_ns
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        if self.sock is None:
            return None
        for _ in range(MAX_DRAIN):
            try:
                packet, ancillary, flags, _ = self.sock.recvmsg(MAX_PACKET)
                if not packet:
                    self.close('progress_disconnected')
                    return None
                require(not ancillary and not flags &
                        (socket.MSG_TRUNC | socket.MSG_CTRUNC),
                        'truncated or ancillary progress frame')
                if supplied_clock is None:
                    now_ns = time.monotonic_ns()
                self.accept(packet, now_ns)
            except BlockingIOError:
                return self.snapshot(now_ns)
            except (OSError, ValueError, struct.error):
                self.unknown('invalid_progress')
                # Do not let a later good frame in this batch erase a gap in
                # the evidence used to confirm continuous stalled execution.
                return None
        self.unknown('progress_queue_backlog')
        return None

    def snapshot(self, now_ns=None):
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        if self.last is not None and not (
                0 <= now_ns-self.last.observed_ns <= FRESH_NS):
            self.unknown('stale_progress')
        return self.last

    def mark_ready(self, now_ns):
        self.ready_ns, self.success_baseline = now_ns, None

    def stable(self, now_ns=None):
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        value = self.snapshot(now_ns)
        return (value is not None and self.ready_ns is not None and
                now_ns-self.ready_ns >= 600 * NS and
                self.success_baseline is not None and
                value.successes > self.success_baseline)

    def close(self, reason='progress_closed'):
        sock, self.sock = self.sock, None
        self.unknown(reason)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


@dataclass(frozen=True)
class Stall:
    ticket: int
    work_id: int
    work: int
    completed: int
    deadline_ns: int
    since_ns: int
    confirmed_ns: int


class Detector:
    """Operational deadline overrun, never a claim of physical device damage."""
    def __init__(self):
        self.pending = {}
        self.sequence = 0
        self.confirmed = None

    def assess(self, value, now_ns=None):
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        if (value is None or not value.complete or
                not 0 <= now_ns-value.observed_ns <= FRESH_NS):
            self.pending.clear()
            self.confirmed = None
            return None
        if value.sequence <= self.sequence:
            # Re-reading a cached sample cannot confirm continuous failure.
            return self.confirmed
        self.sequence = value.sequence
        pending, confirmed = {}, []
        for row in value.rows:
            # Scheduler wait describes the parked request handler while its
            # selected worker executes. Other waits are not GPU failure facts.
            budget = {3: 300 * NS, 5: 300 * NS, 6: 120 * NS,
                      7: 120 * NS, 8: 30 * NS}.get(row.work)
            loop = (value.wire_version == 2 and row.work == 8 and
                    row.stage == 10 and row.deadline_ns == 0 and row.wait == 0)
            if (budget is None or row.wait not in (0, 4) or
                    (not row.deadline_ns and not loop) or
                    (row.work == 8 and not loop)):
                continue
            last = max(row.work_since_ns, row.last_completed_ns)
            deadline = last + budget if loop else row.deadline_ns
            if value.observed_ns < max(deadline + 30 * NS, last + budget):
                continue
            key = (row.work_id, row.work, row.completed, row.last_completed_ns,
                   row.deadline_ns, row.wait, row.wait_since_ns)
            prior = self.pending.get(row.ticket)
            first = prior[1] if prior is not None and prior[0] == key else value.observed_ns
            pending[row.ticket] = (key, first)
            if value.observed_ns-first >= 2 * NS:
                confirmed.append(Stall(row.ticket, row.work_id, row.work,
                    row.completed, row.deadline_ns, first, value.observed_ns))
        self.pending = pending
        self.confirmed = min(confirmed,
            key=lambda row: (row.deadline_ns, row.ticket), default=None)
        return self.confirmed
