"""Private single-writer storage. Run only in the isolated journal process.

Filesystem I/O can block indefinitely in the kernel. The Guardian must never
call this module in its event loop, nor start another writer until this worker
has actually exited. A failed commit poisons this writer; no grant is returned.
"""
import fcntl
import json
import os
from pathlib import Path
import stat
import time

from tools.serve_recovery import ledger

LOCK = 'writer.lock'
STATE = 'ledger.json'
PENDING = 'ledger.pending'
CONTRACT_PENDING = 'ledger.contract.pending'
SERVICE_CONTRACT = 'q4t-service-state-v2'


def envelope_keys(value):
    ledger.keys(value, ('sha256', 'previous_sha256', 'state',
                       *(('service_contract',) if 'service_contract' in value else ())))
    ledger.require('service_contract' not in value or
                   value['service_contract'] == SERVICE_CONTRACT,
                   'unknown service state contract')


def stamp():
    return ledger.Stamp(
        Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        time.monotonic_ns())


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        ledger.require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def decode(data):
    def invalid_constant(_value):
        raise ledger.LedgerError('non-finite JSON value')
    try:
        return json.loads(data, object_pairs_hook=unique_object,
                          parse_constant=invalid_constant)
    except (ValueError, RecursionError) as error:
        raise ledger.LedgerError('invalid bounded JSON') from error


def check_file(fd):
    info = os.fstat(fd)
    ledger.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                   info.st_uid == os.geteuid() and
                   stat.S_IMODE(info.st_mode) == 0o600,
                   'private regular file required')
    return info


class Store:
    def __init__(self, directory, service_id, *, initialize=False, clock=stamp):
        ledger.require(ledger.identifier(service_id), 'invalid service_id')
        self.directory = Path(directory).absolute()
        ledger.require(self.directory.resolve() == self.directory,
                       'symlink or noncanonical ledger directory')
        self.service_id = service_id
        self.clock = clock
        self.dir_fd = -1
        self.lock_fd = -1
        self.poisoned = False
        self.state = None
        self.service_contract = False
        try:
            if initialize:
                self.directory.mkdir(mode=0o700, parents=False, exist_ok=True)
            self.dir_fd = os.open(self.directory,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(self.dir_fd)
            ledger.require(info.st_uid == os.geteuid() and
                           stat.S_IMODE(info.st_mode) == 0o700,
                           'ledger directory must be private 0700')
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            if initialize:
                # The lock is also the immutable service identity marker.
                # A missing ledger never turns an existing service into a new
                # budget, including after an interrupted initialization.
                flags |= os.O_CREAT | os.O_EXCL
            self.lock_fd = os.open(LOCK, flags, 0o600, dir_fd=self.dir_fd)
            check_file(self.lock_fd)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            named = os.stat(LOCK, dir_fd=self.dir_fd, follow_symlinks=False)
            held = os.fstat(self.lock_fd)
            ledger.require((named.st_dev, named.st_ino) ==
                           (held.st_dev, held.st_ino), 'writer lock replaced')
            identity = dict(version=ledger.VERSION, service_id=service_id)
            if initialize:
                ledger.require(not self.exists(STATE) and
                               not self.exists(PENDING),
                               'refusing initialization over existing state')
                self.write_all(self.lock_fd, ledger.canonical(identity))
                os.fsync(self.lock_fd)
                os.fsync(self.dir_fd)
                self.commit(ledger.new_state(service_id, self.clock()))
            else:
                marker = self.read(LOCK)
                ledger.require(type(marker) is dict and
                               type(marker.get('version')) is int and
                               marker == identity,
                               'persistent service identity mismatch')
                self.reconcile_contract()
                self.reconcile_pending()
                self.state = self.read_state(STATE)
                # Reaffirm an ambiguous rename after a former writer died
                # before its directory fsync. Do not issue a launch yet.
                with self.open_file(STATE) as file:
                    os.fsync(file.fileno())
                os.fsync(self.dir_fd)
        except BaseException:
            self.close()
            raise

    def exists(self, name):
        try:
            os.stat(name, dir_fd=self.dir_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def open_file(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=self.dir_fd)
        try:
            check_file(fd)
        except BaseException:
            os.close(fd)
            raise
        return os.fdopen(fd, 'rb')

    def read(self, name):
        with self.open_file(name) as file:
            ledger.require(os.fstat(file.fileno()).st_size <=
                           ledger.MAX_BYTES+1024, 'ledger file too large')
            data = file.read(ledger.MAX_BYTES+1025)
        ledger.require(len(data) <= ledger.MAX_BYTES+1024,
                       'ledger file grew past limit')
        return decode(data)

    def read_envelope(self, name):
        envelope = self.read(name)
        envelope_keys(envelope)
        ledger.require(envelope['sha256'] == ledger.digest(envelope['state']),
                       'ledger checksum mismatch')
        previous = envelope['previous_sha256']
        ledger.require(previous is None or
                       (type(previous) is str and
                        ledger.HASH.fullmatch(previous) is not None),
                       'invalid ledger predecessor')
        ledger.validate(envelope['state'], self.service_id)
        return envelope

    def read_state(self, name):
        value = self.read_envelope(name)
        self.service_contract = 'service_contract' in value
        return value['state']

    def reconcile_contract(self):
        if not self.exists(CONTRACT_PENDING):return
        current = self.read_envelope(STATE)
        raw = ledger.canonical(dict(current, service_contract=SERVICE_CONTRACT))
        # Unlike a grant transaction, this file can only add a format fence.
        # A short write may be completed from the still authoritative ledger;
        # no spent grant, revision or desired state is reconstructed or reset.
        fd = os.open(CONTRACT_PENDING, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=self.dir_fd)
        try:
            info = check_file(fd)
            ledger.require(info.st_size <= len(raw) and
                raw.startswith(os.pread(fd, len(raw)+1, 0)), 'service contract predecessor changed')
            if info.st_size != len(raw):
                os.ftruncate(fd, 0);self.write_all(fd, raw)
            os.fsync(fd)
        finally:os.close(fd)
        with self.open_file(CONTRACT_PENDING) as file:os.fsync(file.fileno())
        os.replace(CONTRACT_PENDING, STATE, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
        os.fsync(self.dir_fd)

    def require_service_contract(self):
        """One-way legacy fence; preserve every ledger value and spent grant.

        Old journal code rejects the extra envelope field before any grant.
        This also fences a legacy launcher that read its descriptor before a
        concurrent STOPPED upgrade. Only the explicit deployment owner calls it.
        """
        ledger.require(not self.poisoned and not self.exists(PENDING),
                       'service contract requires recovered writer')
        if self.service_contract:return
        value = dict(self.read_envelope(STATE), service_contract=SERVICE_CONTRACT)
        try:
            fd = os.open(CONTRACT_PENDING, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.dir_fd)
            try:self.write_all(fd, ledger.canonical(value));os.fsync(fd)
            finally:os.close(fd)
            self.reconcile_contract()
            self.service_contract = True
        except BaseException:
            self.poisoned = True
            raise

    def reconcile_pending(self):
        if not self.exists(PENDING):
            return
        # A complete pre-rename transaction is conservatively committed. Its
        # authorized slot remains occupied until Guardian proves no child from
        # that ticket survives. A partial/corrupt file freezes recovery instead
        # of discarding a possibly spent attempt.
        envelope = self.read_envelope(PENDING)
        pending = envelope['state']
        if self.exists(STATE):
            previous = self.read_envelope(STATE)
            ledger.require('service_contract' not in previous or
                           envelope.get('service_contract') == SERVICE_CONTRACT,
                           'service contract cannot regress')
            old = previous['state']
            ledger.require(pending['revision'] == old['revision']+1,
                           'unreconciled pending revision')
            ledger.require(envelope['previous_sha256'] == ledger.digest(old),
                           'pending predecessor mismatch')
            request = pending['last']
            ledger.require(request is not None, 'pending receipt missing')
        else:
            ledger.require(envelope['previous_sha256'] is None and
                           pending['revision'] == 0 and pending['last'] is None
                           and pending['desired'] == 'STOPPED' and
                           all(value == 0 for value in
                               pending['generations'].values()),
                           'missing established ledger')
        with self.open_file(PENDING) as file:
            os.fsync(file.fileno())
        os.replace(PENDING, STATE, src_dir_fd=self.dir_fd,
                   dst_dir_fd=self.dir_fd)
        os.fsync(self.dir_fd)

    @staticmethod
    def write_all(fd, data):
        offset = 0
        while offset < len(data):
            count = os.write(fd, data[offset:])
            if count <= 0:
                raise OSError('short journal write')
            offset += count

    def commit(self, state):
        ledger.require(not self.poisoned, 'journal is frozen after I/O failure')
        ledger.validate(state, self.service_id)
        data = ledger.canonical(dict(sha256=ledger.digest(state), state=state,
            previous_sha256=ledger.digest(self.state) if self.state else None,
            **({'service_contract': SERVICE_CONTRACT} if self.service_contract else {})))
        try:
            fd = os.open(PENDING, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                         dir_fd=self.dir_fd)
            try:
                self.write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(PENDING, STATE, src_dir_fd=self.dir_fd,
                       dst_dir_fd=self.dir_fd)
            os.fsync(self.dir_fd)
        except BaseException:
            self.poisoned = True
            raise
        self.state = state

    def transact(self, request):
        ledger.require(not self.poisoned, 'journal is frozen after I/O failure')
        state, reply = ledger.transition(self.state, request, self.clock())
        if state['revision'] != self.state['revision']:
            self.commit(state)
        return reply

    def close(self):
        for field in ('lock_fd', 'dir_fd'):
            fd = getattr(self, field, -1)
            if fd >= 0:
                # Do not LOCK_UN a description that may have been inherited.
                os.close(fd)
                setattr(self, field, -1)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
