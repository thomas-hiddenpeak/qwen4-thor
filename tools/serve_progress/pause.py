"""Validation-only single-thread pause of a live descendant; never SIGSTOP.

The main thread is observed with ptrace while native progress and GPU worker
threads remain schedulable. No EXITKILL option is installed. This helper is
not part of a production service bundle or its recovery decisions.
"""
import ctypes
import errno
import os
from pathlib import Path
import time

SEIZE, INTERRUPT, DETACH = 0x4206, 0x4207, 17
WALL = 0x40000000


def identity(pid):
    if type(pid) is not int or pid <= 1:
        raise ValueError('invalid pause owner PID')
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] in ('Z', 'X'):
        raise ValueError('pause target already exited')
    return dict(pid=pid, start_ticks=int(fields[19]),
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        pid_namespace=Path(f'/proc/{pid}/ns/pid').stat().st_ino)


def own_descendant(owner):
    if identity(owner['pid']) != owner:
        raise ValueError('pause owner identity changed')
    current, visited = owner['pid'], set()
    for _ in range(64):
        if current <= 1 or current in visited or current == os.getpid():
            break
        visited.add(current)
        fields = Path(f'/proc/{current}/stat').read_text().rsplit(')', 1)[1].split()
        current = int(fields[1])
        if current == os.getpid():
            return
    raise ValueError('pause target is not an owned descendant')


def trace(operation, pid):
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.ptrace
    call.argtypes = (ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)
    call.restype = ctypes.c_long
    if call(operation, pid, None, None) == -1:
        number = ctypes.get_errno()
        raise OSError(number, 'validation ptrace operation '+str(operation))


class PausedMain:
    def __init__(self, owner):
        self.owner = dict(owner)
        self.attached = self.stopped = False

    def _wait(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.owner['pid'], os.WNOHANG | os.WUNTRACED | WALL)
            if pid:
                if os.WIFSTOPPED(status):
                    self.stopped = True
                    return
                self.attached = False
                raise RuntimeError('pause target exited during validation')
            time.sleep(.01)
        raise TimeoutError('owned thread did not reach ptrace stop')

    def __enter__(self):
        own_descendant(self.owner)
        trace(SEIZE, self.owner['pid'])
        self.attached = True
        try:
            trace(INTERRUPT, self.owner['pid'])
            self._wait(2)
            own_descendant(self.owner)
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.attached:
            return
        try:
            if not self.stopped:
                trace(INTERRUPT, self.owner['pid'])
                self._wait(2)
            trace(DETACH, self.owner['pid'])
            self.attached = self.stopped = False
        except (ProcessLookupError, ChildProcessError):
            self.attached = self.stopped = False
        except OSError as error:
            if error.errno != errno.ESRCH:
                raise
            self.attached = self.stopped = False

    def __exit__(self, *_args):
        self.close()
