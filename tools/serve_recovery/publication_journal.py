"""Repair a known publication write, never infer intent from partial JSON."""
import copy
import os
import uuid

from tools.serve_recovery import access_store, ledger, store


def transaction_id(request_id, phase):
    return str(uuid.uuid5(uuid.UUID(request_id), phase))


def successor(writer, head, candidate, request_id):
    previous = head['state']['policy']
    writer.contract.transition(previous, candidate)
    receipt = dict(id=request_id, request_sha256=ledger.digest(dict(
        expected_revision=previous['revision'], policy=candidate)),
        revision=candidate['revision'], policy_sha256=writer.contract.digest(candidate))
    contents = dict(policy=copy.deepcopy(candidate), receipts=[
        *head['state']['receipts'][-(access_store.MAX_RECEIPTS-1):], receipt])
    writer.validate_state(contents)
    return dict(format=writer.format, service_id=head['service_id'],
        state_identity=head['state_identity'], previous_sha256=head['sha256'],
        sha256=ledger.digest(contents), state=contents)


def recovering(base, candidate_for):
    """The callback proves the predecessor and any required external commit.

    Reconciliation runs under the journal's writer lock and the caller's
    deployment/publication ownership. Unknown bytes are never truncated.
    """
    class RecoveryStore(base):
        def reconcile(self):
            if self.exists('policy.pending'):
                head = self.envelope('policy.json')
                candidate, request_id = candidate_for(head['state']['policy'])
                value = successor(self, head, candidate, request_id)
                raw = ledger.canonical(value)
                ledger.require(len(raw) <= self.contract.MAX_BYTES, 'publication journal byte bound')
                fd = os.open('policy.pending', os.O_RDWR | os.O_NOFOLLOW |
                    os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.dir_fd)
                try:
                    held = store.check_file(fd)
                    ledger.require(held.st_size <= len(raw) and
                        raw.startswith(os.pread(fd, len(raw)+1, 0)),
                        'publication pending differs from immutable intent')
                    self.check_identity()
                    ledger.require(self.envelope('policy.json') == head,
                                   'publication predecessor changed during recovery')
                    named = os.stat('policy.pending', dir_fd=self.dir_fd, follow_symlinks=False)
                    ledger.require((named.st_dev, named.st_ino) == (held.st_dev, held.st_ino),
                                   'publication pending replaced')
                    os.ftruncate(fd, 0);store.Store.write_all(fd, raw);os.fsync(fd)
                finally:os.close(fd)
            super().reconcile()
    return RecoveryStore
