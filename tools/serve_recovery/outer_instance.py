"""An explicit outer owner keeps the actual service instance lock across roots.

Guardian receives a duplicate of this open-file-description. Closing or reaping
one Guardian never releases the parent's custody, so another launcher cannot
interleave with old-tree cleanup and persistent slot reconciliation.
"""
import fcntl
import os
from pathlib import Path
import uuid

from tools.serve_recovery import access_store, child, journal, ledger, store


class Instance:
    def __init__(self, state, service_id):
        self.binding = access_store.binding(state, service_id)
        self.path = Path(state).absolute()/'guardian.lock'
        self.fd = -1
        self.identity = None

    def __enter__(self):
        ledger.require(self.fd < 0, 'outer instance is already held')
        access_store.binding(self.path.parent, self.binding['service_id'])
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            store.check_file(self.fd)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            now = store.stamp()
            self.identity = dict(token=str(uuid.uuid4()), acquired_ns=now.monotonic_ns,
                owner=dict(pid=os.getpid(), start_ticks=journal.process_start(os.getpid()),
                           boot_id=now.boot_id))
            self.check()
            return self
        except BaseException:
            self.close()
            raise

    def check(self):
        ledger.require(self.fd >= 3 and access_store.binding(self.path.parent,
            self.binding['service_id']) == self.binding, 'outer instance state changed')
        ledger.require(self.identity is not None and self.identity['owner'] == dict(
            pid=os.getpid(), start_ticks=journal.process_start(os.getpid()),
            boot_id=store.stamp().boot_id), 'outer instance requires its original owner')
        store.check_file(self.fd)
        child.verify_lease(self.fd, str(self.path))

    def custody(self):
        self.check()
        return dict(self.identity, owner=dict(self.identity['owner']))

    def close(self):
        if self.fd >= 0:
            # No LOCK_UN: a still-running Guardian may share this description.
            os.close(self.fd)
            self.fd = -1
        self.identity = None

    def __exit__(self, *_args): self.close()
