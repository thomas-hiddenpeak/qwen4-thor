"""Reap the privileged launcher before monitoring the adopted user collector."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


class Collector:
    def __init__(self, program, out, deadline, log):
        # The forked collector is adopted after its sudo/direct parent exits.
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):
            raise OSError(ctypes.get_errno(), 'PR_SET_CHILD_SUBREAPER')
        launcher = subprocess.Popen(['sudo', '-n', sys.executable, '-B',
            str(Path(__file__).with_name('collect.py')), str(program), str(out),
            str(deadline), '--detach'], stdout=log, stderr=subprocess.STDOUT)
        if launcher.wait(timeout=10):
            raise RuntimeError('privileged launcher failed')
        self.launcher_pid = launcher.pid
        limit = min(time.time()+20, deadline)
        while not (out/'ready.json').exists():
            if (out/'exit.json').exists():
                raise RuntimeError('collector exited before ready')
            if time.time() >= limit:
                raise TimeoutError('collector ready')
            time.sleep(.01)
        ready = json.loads((out/'ready.json').read_text())
        self.pid = ready['pid']
        self.returncode = None
        self.pidfd = os.pidfd_open(self.pid)
        fields = Path(f'/proc/{self.pid}/stat').read_text().rsplit(')', 1)[1].split()
        if int(fields[1]) != os.getpid() or ready['uid'] != os.getuid():
            self.terminate()
            raise RuntimeError('collector not adopted by unprivileged driver')
        # Real reads through the same restricted proc interfaces used by
        # monitoring, rather than inferring access from uid alone.
        os.readlink(f'/proc/{self.pid}/exe')
        for name in ('io', 'status', 'stat', 'cmdline'):
            Path(f'/proc/{self.pid}/{name}').read_bytes()
        (out/'ownership.json').write_text(json.dumps(dict(
            launcher_pid=launcher.pid, launcher_returncode=launcher.returncode,
            collector_pid=self.pid, driver_pid=os.getpid(), uid=ready['uid'],
            start_ticks=int(fields[19]), proc_access=True))+'\n')

    def poll(self):
        if self.returncode is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.returncode = os.waitstatus_to_exitcode(status)
                os.close(self.pidfd)
        return self.returncode

    def wait(self, timeout):
        end = time.monotonic()+timeout
        while self.poll() is None:
            if time.monotonic() >= end:
                raise subprocess.TimeoutExpired('collector', timeout)
            time.sleep(.01)
        return self.returncode

    def terminate(self):
        if self.poll() is None:
            signal.pidfd_send_signal(self.pidfd, signal.SIGTERM)
