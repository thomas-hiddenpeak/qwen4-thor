"""Isolated credential journal: fixed-size files, no model or Guardian authority.

Use only in an operator/owned I/O process. The immutable identity marker forbids
rebootstrapping a missing policy from the old package. A pending commit is a
desired durable revision, not proof that the live entrypoint has applied it.
"""
import copy
import fcntl
import os
from pathlib import Path
import stat

from tools.serve_recovery import access_policy as policy, ledger, store

MAX_RECEIPTS = 128
FORMAT = 'q4t-client-access-store-v1'
MARKER = 'client-access.json'


def binding(state, service_id):
    state = Path(state).absolute()
    info = state.stat(follow_symlinks=False)
    ledger.require(state.resolve(strict=True) == state and stat.S_ISDIR(info.st_mode) and
        info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700 and
        ledger.identifier(service_id), 'private access state required')
    return dict(state_directory=str(state), service_id=service_id,
                state_identity=[info.st_dev, info.st_ino])


def marker_value(value, writer=None):
    writer = Store if writer is None else writer
    return dict(format=writer.binding_format, service_id=value['service_id'],
                state_identity=value['state_identity'])


def check_binding(value, writer=None):
    writer = Store if writer is None else writer
    state = Path(value['state_directory'])
    ledger.require(binding(state, value['service_id']) == value, 'access state changed')
    fd = os.open(state / writer.marker_name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = store.check_file(fd)
        ledger.require(0 < before.st_size <= 1024, 'access binding size')
        raw = os.read(fd, 1025)
        after = store.check_file(fd)
        ledger.require(len(raw) == before.st_size and all(getattr(before, k) == getattr(after, k)
            for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')) and
            store.decode(raw) == marker_value(value, writer), 'access binding changed')
    finally:
        os.close(fd)


def sync_binding(value, writer=None):
    """Re-establish parent-entry durability before claiming a bound policy."""
    writer = Store if writer is None else writer
    check_binding(value, writer)
    state = Path(value['state_directory'])
    marker = os.open(state / writer.marker_name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        store.check_file(marker)
        os.fsync(marker)
    finally:os.close(marker)
    directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:os.fsync(directory)
    finally:os.close(directory)
    check_binding(value, writer)


def open_bound(value, writer=None):
    writer = Store if writer is None else writer
    check_binding(value, writer)
    current = writer(Path(value['state_directory']) / writer.directory_name, value['service_id'],
                    value['state_identity'])
    try:
        sync_binding(value, writer)
        return current
    except BaseException:
        current.close()
        raise


def initialize_bound(value, initial, writer=None):
    """Explicit one-time operator initialization, never a runtime fallback.

    The outer permanent marker survives loss of the entire journal directory.
    A crash before journal creation stays unavailable, rather than resurrecting
    sealed package keys. Existing markers permit recovery of existing data only.
    """
    writer = Store if writer is None else writer
    state = Path(value['state_directory'])
    ledger.require(binding(state, value['service_id']) == value, 'access state changed')
    first = not os.path.lexists(state / writer.marker_name)
    if first:
        ledger.require(not os.path.lexists(state / writer.directory_name), 'unbound access journal')
        fd = os.open(state / writer.marker_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            store.Store.write_all(fd, ledger.canonical(marker_value(value, writer)))
            os.fsync(fd)
        finally:
            os.close(fd)
        directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
    check_binding(value, writer)
    with writer(state / writer.directory_name, value['service_id'], value['state_identity'],
               initialize=initial if first else None) as journal:
        result = writer.contract.describe(journal.head['state']['policy'])
    # fsync inside the child directory does not persist its entry in state.
    # Repeating this on recovery also closes a prior unknown parent sync.
    sync_binding(value, writer)
    return dict(result, initialized=first, durable=True, live_applied=False)


class Store:
    contract = policy
    format = FORMAT
    marker_name = MARKER
    binding_format = 'q4t-client-access-binding-v1'
    directory_name = 'client-access'

    def __init__(self, directory, service_id, state_identity, *, initialize=None):
        ledger.require(ledger.identifier(service_id) and type(state_identity) is list and
            len(state_identity) == 2 and all(ledger.integer(n, minimum=1) for n in state_identity),
            'invalid access store identity')
        self.directory = Path(directory).absolute()
        self.service_id, self.state_identity = service_id, state_identity
        self.dir_fd = self.lock_fd = -1
        self.poisoned, self.head = False, None
        try:
            if initialize is not None:
                self.contract.validate(initialize)
                ledger.require(initialize['revision'] == 0, 'access bootstrap revision')
                self.directory.mkdir(mode=0o700, exist_ok=True)
            ledger.require(self.directory.resolve(strict=True) == self.directory,
                           'noncanonical access store')
            self.dir_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY |
                                 os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(self.dir_fd)
            ledger.require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                           'access store must be private')
            self.identity = (info.st_dev, info.st_ino)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            if initialize is not None:
                flags |= os.O_CREAT | os.O_EXCL
            self.lock_fd = os.open('writer.lock', flags, 0o600, dir_fd=self.dir_fd)
            store.check_file(self.lock_fd)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.check_identity()
            marker = dict(format=self.format, service_id=service_id, state_identity=state_identity)
            if initialize is not None:
                ledger.require(not self.exists('policy.json') and not self.exists('policy.pending'),
                               'access bootstrap collision')
                store.Store.write_all(self.lock_fd, ledger.canonical(marker))
                os.fsync(self.lock_fd); os.fsync(self.dir_fd)
                self.commit(dict(policy=copy.deepcopy(initialize), receipts=[]))
            else:
                ledger.require(self.read('writer.lock') == marker, 'access store identity changed')
                self.reconcile()
                self.head = self.envelope('policy.json')
                # A prior writer may have renamed successfully but failed its
                # directory fsync. Visibility alone does not prove durability.
                self.sync_file('policy.json')
                os.fsync(self.dir_fd)
                ledger.require(self.envelope('policy.json') == self.head,
                               'access policy changed during recovery')
        except BaseException:
            self.close()
            raise

    def check_identity(self):
        directory = self.directory.stat(follow_symlinks=False)
        held = os.fstat(self.lock_fd)
        named = os.stat('writer.lock', dir_fd=self.dir_fd, follow_symlinks=False)
        ledger.require(stat.S_ISDIR(directory.st_mode) and
            directory.st_uid == os.geteuid() and stat.S_IMODE(directory.st_mode) == 0o700 and
            (directory.st_dev, directory.st_ino) == self.identity and
            (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino),
            'access store or writer replaced')

    def exists(self, name):
        try:
            os.stat(name, dir_fd=self.dir_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def read(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                     os.O_CLOEXEC, dir_fd=self.dir_fd)
        try:
            before = store.check_file(fd)
            ledger.require(0 < before.st_size <= self.contract.MAX_BYTES, 'access file byte bound')
            raw = bytearray()
            while len(raw) <= before.st_size:
                part = os.read(fd, min(65536, before.st_size + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            after = store.check_file(fd)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            ledger.require(len(raw) == before.st_size and
                all(getattr(before, n) == getattr(after, n) for n in fields), 'access source changed')
            return store.decode(raw)
        finally:
            os.close(fd)

    def envelope(self, name):
        value = self.read(name)
        ledger.keys(value, ('format', 'service_id', 'state_identity', 'previous_sha256', 'sha256', 'state'))
        ledger.require(value['format'] == self.format and value['service_id'] == self.service_id and
            value['state_identity'] == self.state_identity and
            value['sha256'] == ledger.digest(value['state']) and
            (value['previous_sha256'] is None or type(value['previous_sha256']) is str and
                ledger.HASH.fullmatch(value['previous_sha256'])), 'access envelope changed')
        self.validate_state(value['state'])
        ledger.require((value['previous_sha256'] is None) ==
            (value['state']['policy']['revision'] == 0), 'access predecessor missing')
        return value

    def sync_file(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                     os.O_CLOEXEC, dir_fd=self.dir_fd)
        try:
            store.check_file(fd)
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def validate_state(cls, value):
        ledger.keys(value, ('policy', 'receipts'))
        cls.contract.validate(value['policy'])
        receipts = value['receipts']
        ledger.require(type(receipts) is list and len(receipts) <= MAX_RECEIPTS,
                       'access receipt count')
        ids, previous = set(), 0
        for receipt in receipts:
            ledger.keys(receipt, ('id', 'request_sha256', 'revision', 'policy_sha256'))
            ledger.require(ledger.identifier(receipt['id']) and receipt['id'] not in ids and
                type(receipt['request_sha256']) is str and ledger.HASH.fullmatch(receipt['request_sha256']) and
                type(receipt['policy_sha256']) is str and ledger.HASH.fullmatch(receipt['policy_sha256']) and
                cls.contract.revision(receipt['revision'], minimum=1) and
                receipt['revision'] > previous, 'invalid access receipt')
            ids.add(receipt['id']); previous = receipt['revision']
        revision = value['policy']['revision']
        ledger.require((revision == 0 and not receipts) or (receipts and
            receipts[-1]['revision'] == revision and
            receipts[-1]['policy_sha256'] == cls.contract.digest(value['policy'])),
            'access revision lacks receipt')

    def reconcile(self):
        if not self.exists('policy.pending'):
            return
        pending = self.envelope('policy.pending')
        if self.exists('policy.json'):
            current = self.envelope('policy.json')
            ledger.require(pending['previous_sha256'] == current['sha256'],
                           'pending access predecessor changed')
            self.contract.transition(current['state']['policy'], pending['state']['policy'])
            ledger.require(pending['state']['receipts'][:-1] ==
                current['state']['receipts'][-(MAX_RECEIPTS - 1):], 'access receipt chain changed')
        else:
            ledger.require(pending['previous_sha256'] is None and
                pending['state']['policy']['revision'] == 0, 'access initialization incomplete')
        self.check_identity()
        self.sync_file('policy.pending')
        os.replace('policy.pending', 'policy.json', src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
        os.fsync(self.dir_fd)

    def commit(self, state):
        ledger.require(not self.poisoned, 'access writer requires recovery')
        self.validate_state(state)
        value = dict(format=self.format, service_id=self.service_id, state_identity=self.state_identity,
            previous_sha256=None if self.head is None else self.head['sha256'],
            sha256=ledger.digest(state), state=state)
        raw = ledger.canonical(value)
        ledger.require(len(raw) <= self.contract.MAX_BYTES, 'access journal byte bound')
        try:
            self.check_identity()
            if self.head is not None:
                ledger.require(self.envelope('policy.json') == self.head,
                               'committed access policy changed')
            fd = os.open('policy.pending', os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.dir_fd)
            try:
                store.Store.write_all(fd, raw); os.fsync(fd)
            finally:
                os.close(fd)
            self.check_identity()
            os.replace('policy.pending', 'policy.json', src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
            os.fsync(self.dir_fd)
            self.head = value
        except BaseException:
            self.poisoned = True
            raise

    def transact(self, request_id, expected_revision, candidate):
        ledger.require(not self.poisoned and self.head is not None,
                       'access writer requires recovery')
        ledger.require(ledger.identifier(request_id) and
            self.contract.revision(expected_revision), 'access transaction identity')
        self.contract.validate(candidate)
        request_hash = ledger.digest(dict(expected_revision=expected_revision, policy=candidate))
        state = self.head['state']
        for receipt in state['receipts']:
            if receipt['id'] == request_id:
                ledger.require(receipt['request_sha256'] == request_hash, 'access idempotency conflict')
                return dict(receipt, replayed=True, durable=True, live_applied=False)
        ledger.require(state['policy']['revision'] == expected_revision, 'access revision conflict')
        self.contract.transition(state['policy'], candidate)
        receipt = dict(id=request_id, request_sha256=request_hash,
            revision=candidate['revision'], policy_sha256=self.contract.digest(candidate))
        new = dict(policy=copy.deepcopy(candidate), receipts=[
            *state['receipts'][-(MAX_RECEIPTS - 1):], receipt])
        self.commit(new)
        return dict(receipt, replayed=False, durable=True, live_applied=False)

    def close(self):
        for name in ('lock_fd', 'dir_fd'):
            fd = getattr(self, name)
            if fd >= 0:
                os.close(fd); setattr(self, name, -1)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
