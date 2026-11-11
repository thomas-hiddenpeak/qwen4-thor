"""An explicit outer owner keeps the actual service instance lock across roots.

Guardian receives a duplicate of this open-file-description. Closing or reaping
one Guardian never releases the parent's custody, so another launcher cannot
interleave with old-tree cleanup and persistent slot reconciliation.
"""
import fcntl
import os
from pathlib import Path

from tools.serve_recovery import access_store, child, ledger, store


class Instance:
    def __init__(self, state, service_id):
        self.binding = access_store.binding(state, service_id)
        self.path = Path(state).absolute()/'guardian.lock'
        self.fd = -1

    def __enter__(self):
        ledger.require(self.fd < 0, 'outer instance is already held')
        access_store.binding(self.path.parent, self.binding['service_id'])
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            store.check_file(self.fd)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.check()
            return self
        except BaseException:
            self.close()
            raise

    def check(self):
        ledger.require(self.fd >= 3 and access_store.binding(self.path.parent,
            self.binding['service_id']) == self.binding, 'outer instance state changed')
        store.check_file(self.fd)
        child.verify_lease(self.fd, str(self.path))

    def close(self):
        if self.fd >= 0:
            # No LOCK_UN: a still-running Guardian may share this description.
            os.close(self.fd)
            self.fd = -1

    def __exit__(self, *_args): self.close()
