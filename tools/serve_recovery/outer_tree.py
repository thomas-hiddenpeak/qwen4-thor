"""Continuous kernel custody for one outer-owned service tree.

Use only in a dedicated single-threaded outer process with no existing children.
Subreaper ancestry survives setsid/double-fork. Only ECHILD after reaping proves
this particular tree empty; a new observer cannot adopt that proof from JSON.
This module does not inspect GPU availability or authorize another model.
"""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

from tools.serve_recovery import journal, ledger, store

MAX_CHILDREN = 512


def direct_candidate(raw, parent_pid):
    """A disappearing/unreadable foreign record is never a cleanup verdict."""
    if not raw or len(raw) > 4096: return None
    try:
        data = raw.rsplit(b')', 1)[1].split()
        return int(data[19]) if int(data[1]) == parent_pid else None
    except (ValueError, IndexError): return None


def no_children():
    try:
        os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    return False


class Tree:
    def __init__(self):
        self.root = None
        self.root_identity = None
        self.entered = False
        self.previous_subreaper = None
        self.scope_id = str(uuid.uuid4())
        self.owner_pid = os.getpid()
        self.owner_start = journal.process_start(self.owner_pid)
        self.boot_id = store.stamp().boot_id
        self.reaped = []

    def local(self):
        ledger.require(os.getpid() == self.owner_pid and
            journal.process_start(self.owner_pid) == self.owner_start and
            len(list(Path('/proc/self/task').iterdir())) == 1,
            'tree custody requires its original single-threaded owner')

    def __enter__(self):
        self.local()
        ledger.require(not self.entered and no_children(),
                       'outer custody cannot take over pre-existing children')
        ledger.require(signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL,
                       'outer custody requires explicit child reaping')
        libc = ctypes.CDLL(None, use_errno=True); old = ctypes.c_int()
        if libc.prctl(37, ctypes.byref(old), 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'read subreaper')
        fd = os.pidfd_open(os.getpid())
        try: signal.pidfd_send_signal(fd, 0)
        finally: os.close(fd)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'enable subreaper')
        self.previous_subreaper = old.value; self.entered = True
        return self

    def launch(self, command, *, env, pass_fds=()):
        self.local()
        ledger.require(self.entered and self.root is None and no_children(),
                       'outer tree can dispatch only one owned root')
        ledger.require(type(command) is list and 1 <= len(command) <= 64 and
            os.path.isabs(command[0]) and
            all(type(arg) is str and len(arg) <= 4096 and '\0' not in arg for arg in command),
            'invalid bounded outer command')
        self.root = subprocess.Popen(command, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, env=env, pass_fds=pass_fds)
        # It is our unreaped direct child: its PID cannot recycle before poll.
        self.root_identity = dict(pid=self.root.pid,
            start_ticks=journal.process_start(self.root.pid), boot_id=self.boot_id)
        return dict(self.root_identity)

    def poll(self):
        self.local()
        ledger.require(self.entered and self.root is not None, 'no owned outer root')
        return self.root.poll()

    def reap(self):
        self.poll()
        for _ in range(MAX_CHILDREN):
            try: info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError: return True
            if info is None: return False
            if info.si_pid == self.root.pid:
                self.root.poll()
            else:
                pid, status = os.waitpid(info.si_pid, os.WNOHANG)
                ledger.require(pid == info.si_pid, 'owned descendant wait changed')
                ledger.require(len(self.reaped) < MAX_CHILDREN, 'outer reaping history bound')
                self.reaped.append(dict(pid=pid, exit_code=os.waitstatus_to_exitcode(status)))
        return no_children()

    def signal_children(self, signum, sent):
        # Thor's kernel does not expose /proc/PID/task/TID/children. Discovery
        # through /proc only selects signal candidates; absence here NEVER
        # proves cleanup. waitid/ECHILD remains the independent completion gate.
        children = []
        for index, entry in enumerate(Path('/proc').iterdir()):
            ledger.require(index < 65536, 'outer process discovery bound')
            if not entry.name.isdigit(): continue
            try:
                with (entry/'stat').open('rb') as stream: raw = stream.read(4097)
            except OSError: continue  # Unknown children keep waitid nonempty.
            start = direct_candidate(raw, self.owner_pid)
            if start is None: continue
            ledger.require(len(children) < MAX_CHILDREN, 'outer child count bound')
            children.append((int(entry.name), start))
        for pid, start in children:
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError: continue
            except FileNotFoundError: continue
            try:
                fields = Path('/proc')/str(pid)/'stat'
                try: data = fields.read_text().rsplit(')', 1)[1].split()
                except FileNotFoundError: continue
                ledger.require(int(data[1]) == self.owner_pid and int(data[19]) == start,
                               'process is no longer an owned direct child')
                key = (pid, start, int(signum))
                if key in sent: continue
                ledger.require(len(sent) < 2*MAX_CHILDREN, 'outer signal history bound')
                try: signal.pidfd_send_signal(fd, signum)
                except ProcessLookupError: pass
                sent.add(key)
            finally: os.close(fd)

    def drain(self, *, term_seconds=30, kill_seconds=5):
        self.local()
        ledger.require(self.entered and self.root is not None and
            0 <= term_seconds <= 30 and 0 < kill_seconds <= 5,
            'invalid outer cleanup bound')
        started = time.monotonic(); sent = set()
        while True:
            if self.reap(): return self.witness(self.root_identity)
            elapsed = time.monotonic()-started
            if elapsed >= term_seconds+kill_seconds:
                raise TimeoutError('owned outer tree not empty; retain custody and deny replacement')
            self.signal_children(signal.SIGTERM if elapsed < term_seconds else signal.SIGKILL, sent)
            time.sleep(.01)

    def witness(self, expected_owner):
        self.local()
        ledger.require(self.entered and self.root_identity is not None and
            expected_owner == self.root_identity and self.root.returncode is not None and no_children(),
            'no continuous empty-tree evidence for the requested root')
        return dict(scope_id=self.scope_id, supervisor=dict(pid=self.owner_pid,
            start_ticks=self.owner_start, boot_id=self.boot_id), root=dict(self.root_identity),
            root_exit_code=self.root.returncode, reaped=list(self.reaped),
            kernel_echild=True, device_authorized=False, model_actions=False)

    def close(self):
        self.local()
        ledger.require(self.entered and no_children(),
                       'cannot abandon outer custody while children remain')
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, self.previous_subreaper, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'restore subreaper')
        self.entered = False

    def __exit__(self, *_args): self.close()
