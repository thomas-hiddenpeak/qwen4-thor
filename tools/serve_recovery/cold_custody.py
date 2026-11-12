"""Same kernel OFD held by every cold auxiliary descendant, never a model.

After watcher loss, a replacement cannot dispatch while any original helper
still retains this descriptor. The lock conveys no GPU or service authority.
"""
import fcntl
import os
from pathlib import Path

from tools.serve_recovery import device_domain, ledger, store

NAME = 'cold-issuer-tree.lock'
ENVIRONMENT = 'Q4T_COLD_AUX_CUSTODY_FD'


def identity(config):
    return dict(format='q4t-cold-issuer-tree-v1', service_id=config['service_id'],
                configuration_sha256=ledger.digest(config))


def initialize(directory, config, *, first):
    path = Path(directory)/NAME
    if first:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = store.check_file(fd); raw = ledger.canonical(identity(config))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ledger.require(info.st_size <= len(raw) and raw.startswith(os.pread(fd, len(raw)+1, 0)),
                           'unpublished auxiliary custody changed')
            os.ftruncate(fd, 0); store.Store.write_all(fd, raw); os.fsync(fd)
        finally: os.close(fd)
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try: os.fsync(parent)
        finally: os.close(parent)
    ledger.require(device_domain.read_record(path) == identity(config), 'auxiliary custody identity differs')


def acquire(directory, config):
    path = Path(directory)/NAME
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = store.check_file(fd); named = path.lstat()
        ledger.require((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino) and
            os.pread(fd, 65537, 0) == ledger.canonical(identity(config)), 'auxiliary custody binding differs')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException: os.close(fd); raise


def inherited():
    raw = os.environ.get(ENVIRONMENT)
    if raw is None: return ()
    ledger.require(os.geteuid() == 0 and raw.isdecimal() and len(raw) <= 7 and
                   3 <= int(raw) <= 1000000, 'invalid auxiliary custody descriptor')
    fd = int(raw); store.check_file(fd)
    return (fd,)


def verify(directory, config):
    descriptors = inherited()
    ledger.require(len(descriptors) == 1, 'missing auxiliary custody descriptor')
    fd = descriptors[0]; info = os.fstat(fd); path = Path(directory)/NAME; named = path.lstat()
    ledger.require((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino) and
        os.pread(fd, 65537, 0) == ledger.canonical(identity(config)), 'auxiliary custody identity changed')
    probe = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        try: fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return descriptors
        raise ValueError('auxiliary custody lock is not held')
    finally: os.close(probe)


def environment(base):
    env = dict(base)
    descriptors = inherited()
    if descriptors: env[ENVIRONMENT] = str(descriptors[0])
    return env
