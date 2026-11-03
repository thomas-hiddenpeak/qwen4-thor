"""Validation custody of an owned controller's actual flock description.

No alternate lock, unlock, service mutation or automatic restart. The original
production guard owns startup. This observer duplicates its locked description
before the intentional crash and keeps central GPU monitoring through cleanup.
"""
import ctypes
import json
import os
from pathlib import Path
import signal
import threading
import time

from tools.serve_service import package as pkg, supervisor as sup
from tools.serve_service.service import guard


class EnvironmentAbort(RuntimeError):
    pass


def copy_owned_lease(owner, lock_path):
    """pidfd_getfd duplicates an open-file-description, unlike /proc/fd open."""
    pidfd = os.pidfd_open(owner['pid'])
    copied = -1
    try:
        pkg.require(sup.process_identity(owner['pid']) == owner,
                    'controller identity changed')
        fields = Path(f"/proc/{owner['pid']}/stat").read_text().rsplit(')', 1)[1].split()
        pkg.require(int(fields[1]) == os.getpid(), 'controller is not our direct child')
        st = Path(lock_path).stat()
        expected = (st.st_dev, st.st_ino)
        libc = ctypes.CDLL(None, use_errno=True)
        getfd = libc.pidfd_getfd
        getfd.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint)
        getfd.restype = ctypes.c_int
        for entry in Path(f"/proc/{owner['pid']}/fd").iterdir():
            try:
                candidate = entry.stat()
            except FileNotFoundError:
                continue
            if (candidate.st_dev, candidate.st_ino) != expected:
                continue
            copied = getfd(pidfd, int(entry.name), 0)
            if copied < 0:
                raise OSError(ctypes.get_errno(), 'pidfd_getfd owned lease')
            with guard.borrowed_lock(copied, Path(lock_path)):
                pass  # Never LOCK_UN this shared open-file-description.
            pkg.require(sup.process_identity(owner['pid']) == owner,
                        'controller changed while copying lease')
            return copied, dict(source_fd=int(entry.name), copied_fd=copied,
                device=st.st_dev, inode=st.st_ino, owner=owner,
                source_fdinfo=Path(f"/proc/{owner['pid']}/fdinfo/{entry.name}").read_text(),
                copied_fdinfo=Path(f'/proc/self/fdinfo/{copied}').read_text())
        raise RuntimeError('controller has no repository lease descriptor')
    except BaseException:
        if copied >= 0:
            os.close(copied)
        raise
    finally:
        os.close(pidfd)


class Custody:
    def __init__(self, owner, lock_path, output, *, on_failure,
                 scan=None, interval=1):
        self.owner, self.lock_path, self.output = owner, Path(lock_path), Path(output)
        self.fd, binding = copy_owned_lease(owner, self.lock_path)
        try:
            pkg.save(self.output/'lease-copy.json', binding)
        except BaseException:
            os.close(self.fd)
            raise
        self.history = {}
        self.mutex = threading.Lock()
        self.stop = threading.Event()
        self.error = None
        self.samples = 0
        self.interval = interval
        self.on_failure = on_failure
        self.scan = scan or self.inspect
        self.thread = None

    def inspect(self):
        try:
            alive = sup.process_identity(self.owner['pid']) == self.owner
        except (FileNotFoundError, ProcessLookupError):
            alive = False
        # Once the controller is gone, only known start identities and their
        # descendants are owned. A recycled PGID cannot become a new root.
        return guard.inspect(self.owner['pid'] if alive else -1, self.history,
            compute_query=lambda: guard.nvml_pids(self.output/'nvml.jsonl'))

    def sample(self):
        with self.mutex:
            if self.error is not None:
                raise EnvironmentAbort(self.error)
            try:
                with guard.borrowed_lock(self.fd, self.lock_path):
                    pass
                snapshot = self.scan()
                if snapshot['conflicts']:
                    raise EnvironmentAbort('competing workload during custody')
                row = dict(monotonic=time.monotonic(), observation=snapshot)
                self.samples += 1
            except Exception as error:
                self.error = repr(error)
                row = dict(monotonic=time.monotonic(), status='ENV_ABORT',
                           error=self.error)
            with (self.output/'events.jsonl').open('a') as stream:
                stream.write(json.dumps(row)+'\n')
                stream.flush()
            if self.error is not None:
                raise EnvironmentAbort(self.error)
            return snapshot

    def start(self):
        self.sample()
        def poll():
            try:
                while not self.stop.wait(self.interval):
                    self.sample()
            except BaseException as error:
                # Also surface a failed event-log write, never silently lose
                # the only live monitor after the controller has died.
                self.error = self.error or repr(error)
                self.on_failure()
        self.thread = threading.Thread(target=poll, name='q4t-lease-custody',
                                       daemon=True)
        self.thread.start()

    def check(self):
        if self.error is not None:
            raise EnvironmentAbort(self.error)

    def identities(self):
        with self.mutex:
            return {pid:dict(row) for pid,row in self.history.items()}

    def finish(self, *, cleanup_confirmed):
        """Caller must prove owned processes and actual GPU contexts are gone."""
        self.stop.set()
        if self.thread is not None:
            self.thread.join(7)
            pkg.require(not self.thread.is_alive(), 'custody monitor did not stop')
        try:
            self.check()
            self.sample()
            pkg.require(cleanup_confirmed, 'owned cleanup not confirmed')
            pkg.save(self.output/'exit.json', dict(status='COMPLETED',
                cleanup_confirmed=True, samples=self.samples,
                lock_inode=os.fstat(self.fd).st_ino))
        except BaseException as error:
            pkg.save(self.output/'exit.json', dict(status='ENV_ABORT',
                cleanup_confirmed=cleanup_confirmed, samples=self.samples,
                error=repr(error)))
            raise
        finally:
            # Closing this duplicate does not unlock a still-live original.
            os.close(self.fd)
            self.fd = -1
