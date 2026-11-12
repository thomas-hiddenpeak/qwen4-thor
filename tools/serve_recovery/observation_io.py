"""Lossy, bounded observation I/O; never a recovery or durability authority.

One exec worker per model generation owns all ordinary disk writes. The capture
thread only drains a nonblocking model pipe and exchanges bounded seqpackets.
A timed-out/unreaped writer never gets a replacement within that generation.
Returned disk errors retry the same worker after 60 seconds. Counters survive
model generations within a controller, and are projected via Guardian IPC.
"""
import argparse
from collections import deque
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import journal, ledger
from tools.serve_service import package as engine, supervisor

MAX_RECORD = 16384
MAX_RECORDS = 32
MAX_BYTES = 128 * 1024
ACK_SECONDS = 2
RETRY_SECONDS = 60
LOG_BYTES = 4 * 1024 * 1024
HEADER = struct.Struct('!QB')
ACK = struct.Struct('!QB')
LOG, SELF_CHECK = 1, 2


class Observations:
    """Only fixed counters/state: no payloads, paths, or exception strings."""
    def __init__(self):
        self.lock = threading.RLock()
        self.failed = self.dropped = self.acknowledged = 0
        self.unavailable = False

    def fail(self, size):
        # Also used for unconfirmed writes: a prefix may already be on disk.
        self.failed = min(ledger.MAX_INTEGER, self.failed+1)
        self.dropped = min(ledger.MAX_INTEGER, self.dropped+size)
        self.unavailable = True

    def success(self):
        self.acknowledged = min(ledger.MAX_INTEGER, self.acknowledged+1)
        self.unavailable = False

    def snapshot(self, writer=None):
        with self.lock:
            return dict(failed_operations=self.failed, dropped_bytes=self.dropped,
                acknowledged_operations=self.acknowledged,
                queued_bytes=writer.queued_bytes if writer else 0,
                queued_records=len(writer.queue) if writer else 0,
                pending_records=int(writer is not None and writer.pending is not None),
                worker_pid=writer.process.pid if writer and writer.process and
                    writer.process.returncode is None else 0,
                worker_unreaped=bool(writer and writer.killed and writer.process and
                    writer.process.returncode is None),
                worker_state=writer.state if writer else 'ABSENT')


class Writer:
    """Single owned CPU worker with nonblocking bounded request/ACK slots."""
    def __init__(self, directory, lease_fd, observations, *, clock=time.monotonic):
        self.clock, self.observations = clock, observations
        self.lock = observations.lock
        self.process, self.pidfd, self.sock = None, None, None
        self.queue, self.queued_bytes = deque(), 0
        self.pending = None
        self.sequence = 0
        self.next_attempt = 0
        self.killed = self.closed = False
        self.poll_failed = False
        self.state = 'STARTING'
        self.started_at = clock()
        parent, child = None, None
        try:
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            parent.setblocking(False)
            # No disk-backed stdio, and the worker has no model/Guardian signal
            # authority. The inherited lease stays held until actual exit.
            self.process = subprocess.Popen([sys.executable, '-I', '-B',
                str(ROOT/'tools/serve_recovery/cpu_exec.py'),
                '--parent-pid', str(os.getpid()), '--parent-start',
                str(journal.process_start(os.getpid())), '--', sys.executable,
                '-I', '-B', str(Path(__file__).resolve()), '--worker-fd',
                str(child.fileno()), '--directory', str(directory)],
                pass_fds=(child.fileno(), lease_fd), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            self.sock = parent
            self.pidfd = os.pidfd_open(self.process.pid)
        except (OSError, ValueError, RuntimeError):
            # Retain any spawned child even if identity setup failed.
            self.sock = parent
            with self.lock:
                self.observations.fail(0)
                self._disable()
        finally:
            if child is not None:
                child.close()

    def _drop_queue(self):
        while self.queue:
            _, payload = self.queue.popleft()
            self.observations.fail(len(payload))
        self.queued_bytes = 0

    def _poll(self):
        try:
            return self.process.poll()
        except OSError:
            # Keep the owned identity and slot when the OS cannot confirm exit.
            self.poll_failed = True
            return None

    def _disable(self):
        self.state = 'DISABLED'
        self.observations.unavailable = True
        if self.pending is not None:
            self.observations.fail(self.pending[1])
            self.pending = None
        self._drop_queue()
        if self.process is not None and self._poll() is None and not self.killed:
            self.killed = True
            try:
                if self.pidfd is None:
                    self.process.kill()  # Unreaped direct child, no PID reuse.
                else:
                    signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
            except OSError:
                # A failed signal is unconfirmed cleanup, not permission to
                # forget the child or to stop draining healthy model output.
                pass
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def submit(self, kind, payload):
        if kind not in (LOG, SELF_CHECK) or type(payload) is not bytes:
            raise ValueError('invalid internal observation record')
        with self.lock:
            if (self.closed or self.state == 'DISABLED' or
                    self.clock() < self.next_attempt or len(payload) > MAX_RECORD or
                    len(self.queue) >= MAX_RECORDS or
                    self.queued_bytes+len(payload) > MAX_BYTES):
                self.observations.fail(len(payload))
                return False
            self.queue.append((kind, payload))
            self.queued_bytes += len(payload)
            return True

    def save_self_check(self, value):
        # The caller's self-check is already a bounded internal result. This
        # observation copy grants no readiness or restart authorization.
        return self.submit(SELF_CHECK, ledger.canonical(value)+b'\n')

    def tick(self):
        with self.lock:
            self._tick()

    def _tick(self):
        if self.process is not None and self._poll() is not None:
            if self.state != 'DISABLED':
                self._disable()
            if self.pidfd is not None:
                os.close(self.pidfd)
                self.pidfd = None
            return
        if self.poll_failed:
            self._disable()
            return
        if self.state == 'DISABLED':
            return
        now = self.clock()
        if ((self.state == 'STARTING' and now-self.started_at >= ACK_SECONDS) or
                (self.pending is not None and now >= self.pending[2])):
            self._disable()
            return
        try:
            raw, _, flags, _ = self.sock.recvmsg(ACK.size+1)
        except BlockingIOError:
            raw = None
            flags = 0
        except OSError:
            self._disable()
            return
        if raw is not None:
            if len(raw) != ACK.size or flags & socket.MSG_TRUNC:
                self._disable()
                return
            sequence, status = ACK.unpack(raw)
            if self.state == 'STARTING':
                if (sequence, status) != (0, 0):
                    self._disable()
                    return
                self.state = 'AVAILABLE'
            elif (self.pending is None or sequence != self.pending[0] or
                  status not in (0, 1)):
                self._disable()
                return
            else:
                if status == 0:
                    self.observations.success()
                    self.state = 'AVAILABLE'
                else:
                    self.observations.fail(self.pending[1])
                    self._drop_queue()
                    self.next_attempt = now+RETRY_SECONDS
                    self.state = 'BACKOFF'
                self.pending = None
        if (self.state == 'STARTING' or self.pending is not None or
                not self.queue or now < self.next_attempt):
            return
        if self.sequence == ledger.MAX_INTEGER:
            self._disable()
            return
        kind, payload = self.queue[0]
        packet = HEADER.pack(self.sequence+1, kind)+payload
        try:
            count = self.sock.send(packet, socket.MSG_NOSIGNAL)
        except BlockingIOError:
            # Only one packet may be outstanding. A full idle IPC endpoint
            # violates this contract; do not retain a never-dispatched queue.
            self._disable()
            return
        except OSError:
            self._disable()
            return
        if count != len(packet):
            self._disable()
            return
        self.sequence += 1
        self.queue.popleft()
        self.queued_bytes -= len(payload)
        self.pending = (self.sequence, len(payload), now+ACK_SECONDS)

    def stop(self):
        # No wait(), fsync(), or unbounded join on a shutdown path.
        with self.lock:
            self.closed = True
            self._disable()
            self._tick()

    @property
    def reaped(self):
        with self.lock:
            self._tick()
            return self.process is None or self.process.returncode is not None


class Capture:
    def __init__(self, writer, pipe):
        self.writer, self.pipe = writer, pipe
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name='q4t-observation-drain',
                                       daemon=True)
        os.set_blocking(pipe.fileno(), False)
        self.thread.start()

    def run(self):
        try:
            while not self.stopped.is_set():
                self.writer.tick()
                if not select.select([self.pipe], [], [], 0.01)[0]:
                    continue
                for _ in range(16):
                    try:
                        raw = os.read(self.pipe.fileno(), 4096)
                    except BlockingIOError:
                        break
                    if not raw:
                        return
                    self.writer.submit(LOG, raw)
        except (OSError, ValueError):
            with self.writer.lock:
                self.writer.observations.fail(0)
        finally:
            self.pipe.close()

    def stop(self):
        # Call only after the owned model tree has exited. While it is live,
        # the pipe must remain open even when all observations are discarded.
        self.stopped.set()
        self.thread.join(timeout=0.2)
        return not self.thread.is_alive()


def worker(fd, directory):
    """The sole place where an observation touches a filesystem sink."""
    with socket.socket(fileno=fd) as channel:
        log = supervisor.RotatingLog(Path(directory)/'model.log', LOG_BYTES)
        channel.sendall(ACK.pack(0, 0))
        sequence = 0
        while True:
            raw, _, flags, _ = channel.recvmsg(HEADER.size+MAX_RECORD+1)
            if not raw:
                return
            ledger.require(not flags & socket.MSG_TRUNC and
                HEADER.size <= len(raw) <= HEADER.size+MAX_RECORD,
                'invalid observation frame')
            observed, kind = HEADER.unpack(raw[:HEADER.size])
            ledger.require(observed == sequence+1 and observed <= ledger.MAX_INTEGER
                and kind in (LOG, SELF_CHECK), 'invalid observation sequence')
            sequence = observed
            payload = raw[HEADER.size:]
            status = 0
            try:
                if kind == LOG:
                    log.write(payload)
                else:
                    engine.save(Path(directory)/'self-check.json', json.loads(payload))
            except (OSError, ValueError, TypeError, RuntimeError):
                status = 1
            channel.sendall(ACK.pack(sequence, status))


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-fd', type=int, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    worker(args.worker_fd, args.directory)
