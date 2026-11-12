"""Bounded owned subprocess pipes; an unreaped job keeps its slot and lease."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from tools.serve_recovery import journal, ledger

ROOT = Path(__file__).resolve().parents[2]


class Job:
    def __init__(self, command, *, lease_fd, timeout=5, maximum=65536,
                 clock=time.monotonic):
        self.clock, self.maximum = clock, maximum
        self.deadline = clock()+timeout
        self.killed_at = None
        self.reason = None
        self.output = bytearray()
        self.eof = False
        self.process = subprocess.Popen([sys.executable, '-I', '-B',
            str(ROOT/'tools/serve_recovery/cpu_exec.py'), '--parent-pid', str(os.getpid()),
            '--parent-start', str(journal.process_start(os.getpid())), '--', *command],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            pass_fds=(lease_fd,), start_new_session=True)
        self.fd = None
        try:
            self.fd = os.pidfd_open(self.process.pid)
            os.set_blocking(self.process.stdout.fileno(), False)
        except OSError:
            # Popen already succeeded: retain the child/lease until reaped,
            # instead of throwing away the only worker slot on setup failure.
            self.process.stdout.close()
            self.eof = True
            self.kill('setup_failed')

    def kill(self, reason):
        if self.killed_at is not None or self.process.poll() is not None:
            return
        self.reason, self.killed_at = reason, self.clock()
        try:
            if self.fd is None:
                self.process.kill()  # Unreaped direct child, never a reused PID.
            else:
                signal.pidfd_send_signal(self.fd, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def tick(self):
        # Bound both retained bytes and work per controller iteration.
        if not self.eof:
            for _ in range(8):
                try:
                    part = os.read(self.process.stdout.fileno(), 4096)
                except BlockingIOError:
                    break
                if not part:
                    self.eof = True
                    self.process.stdout.close()
                    break
                room = max(0, self.maximum-len(self.output))
                self.output.extend(part[:room])
                if len(part) > room:
                    self.kill('output_limit')
                    break
        if self.clock() >= self.deadline:
            self.kill('timeout')
        return self.process.poll() is not None and self.eof

    @property
    def unreaped(self):
        return (self.killed_at is not None and self.clock()-self.killed_at >= 1 and
                self.process.poll() is None)

    def close(self):
        ledger.require(self.process.poll() is not None,
                       'cannot release an unreaped job')
        if not self.eof:
            self.process.stdout.close()
            self.eof = True
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def finish(job, tick, *, timeout=7):
    """Used only by bounded startup probes; tick advances the real loop."""
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if tick():
            job.kill('stopping')
        if job.tick():
            result = dict(returncode=job.process.returncode, reason=job.reason,
                          output=bytes(job.output))
            job.close()
            return result
        if job.unreaped:
            break
        time.sleep(0.025)
    job.kill('timeout')
    # Caller retains this object. An unconfirmed child forbids another probe
    # or model and remains in Guardian's owned subtree.
    return None
