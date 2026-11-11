"""Root-protected copies of explicitly selected, hash-bound runtime source.

Never execute mutable package code with privileges. Only the selected Python
source is copied here: no model, API key, TLS private key or gateway policy.
"""
import hashlib
import os
from pathlib import Path
import stat

from tools.serve_recovery import bundle, device_domain, ledger, store

MANIFEST_LIMIT = 1024**2
SOURCE_LIMIT = 4*1024**2
TOTAL_LIMIT = 64*1024**2


def read(path, maximum, expected=None):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        ledger.require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= maximum,
                       'host artifact file bound')
        raw = bytearray()
        while len(raw) < before.st_size:
            block = os.read(fd, min(1024**2, before.st_size-len(raw)))
            ledger.require(block, 'short host artifact')
            raw.extend(block)
        after = os.fstat(fd)
        ledger.require(not os.read(fd, 1) and all(getattr(before, key) == getattr(after, key)
            for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')),
            'host artifact changed while reading')
        ledger.require(expected is None or hashlib.sha256(raw).hexdigest() == expected,
                       'host artifact digest differs')
        return bytes(raw)
    finally: os.close(fd)


def root_file(path, maximum, expected=None, *, mode=None):
    path = Path(path).absolute(); device_domain.secure_directory(path.parent)
    info = path.lstat()
    ledger.require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
        not info.st_mode & 0o022 and (mode is None or stat.S_IMODE(info.st_mode) == mode),
        'host artifact must remain root-protected')
    return read(path, maximum, expected)


def selection(package, digest):
    ledger.require(type(digest) is str and ledger.HASH.fullmatch(digest), 'host manifest digest required')
    package = Path(package).resolve(strict=True)
    raw = read(package/'manifest.json', MANIFEST_LIMIT, digest); value = store.decode(raw)
    ledger.require(value.get('version') == 18 and value.get('profile') == 'q4t-service-recovery-v18'
        and value.get('ingress_mode') == 'native' and type(value.get('files')) is dict,
        'host entry requires the selected native outer-runtime package')
    files = {key[7:]: val for key, val in value['files'].items() if key.startswith('source/')}
    ledger.require(files.keys() == set(bundle.SOURCES) and all(type(x) is str and
        ledger.HASH.fullmatch(x) for x in files.values()), 'host source inventory differs')
    python = value['python']; ledger.keys(python, ('path', 'sha256'))
    ledger.require(type(python['path']) is str and os.path.isabs(python['path']) and
        type(python['sha256']) is str and ledger.HASH.fullmatch(python['sha256']),
        'host interpreter identity required')
    ledger.require(str(Path(python['path']).resolve(strict=True)) == python['path'],
                   'host interpreter path must be canonical')
    root_file(python['path'], TOTAL_LIMIT, python['sha256'])
    return dict(package=str(package), manifest_sha256=digest,
                python=python, sources=files)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try: os.fsync(fd)
    finally: os.close(fd)


def write_new(path, raw):
    """A pending file is resumable only before its immutable name is published."""
    path = Path(path); pending = path.with_name(path.name+'.pending')
    device_domain.secure_directory(path.parent)
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        store.check_file(fd); os.ftruncate(fd, 0)
        store.Store.write_all(fd, raw); os.fsync(fd)
    finally: os.close(fd)
    # No writer other than the installation admin lock is permitted here.
    ledger.require(not os.path.lexists(path), 'host artifact already published')
    os.replace(pending, path); path.chmod(0o444); fsync_directory(path.parent)


def source_directory(path):
    path.mkdir(mode=0o755, exist_ok=True)
    device_domain.secure_directory(path)
    ledger.require(stat.S_IMODE(path.stat().st_mode) in (0o700, 0o755),
                   'unexpected host source directory permissions')
    # The service CLI intentionally sets umask 077. These public, immutable
    # source directories must still be traversable after permanent UID drop.
    path.chmod(0o755)


def prepare(directory, chosen):
    """Only called for the durable, never-enabled initial installation intent."""
    root = Path(directory)/'source'; source_directory(root)
    package = Path(chosen['package']); total = 0
    # Verify the immutable selected manifest again before resuming partial copy.
    read(package/'manifest.json', MANIFEST_LIMIT, chosen['manifest_sha256'])
    for name, digest in sorted(chosen['sources'].items()):
        path = root/name
        for parent in reversed(path.parent.relative_to(root).parents):
            source_directory(root/parent)
        source_directory(path.parent)
        if path.exists():
            # A crash between rename/chmod can leave the original 0600 file.
            raw = root_file(path, SOURCE_LIMIT, digest)
            path.chmod(0o444)
        else:
            raw = read(package/'source'/name, SOURCE_LIMIT, digest)
            ledger.require(total+len(raw) <= TOTAL_LIMIT, 'host source total bound')
            write_new(path, raw)
        total += len(raw); ledger.require(total <= TOTAL_LIMIT, 'host source total bound')
    fsync_directory(root); fsync_directory(directory)
    verify(directory, chosen)


def verify(directory, chosen):
    root = Path(directory)/'source'; device_domain.secure_directory(root)
    actual = set(); total = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        device_domain.secure_directory(parent)
        for name in directories: device_domain.secure_directory(Path(parent)/name)
        for name in files: actual.add(str((Path(parent)/name).relative_to(root)))
    ledger.require(actual == set(chosen['sources']), 'installed host source inventory changed')
    for name, digest in chosen['sources'].items():
        total += len(root_file(root/name, SOURCE_LIMIT, digest, mode=0o444))
        ledger.require(total <= TOTAL_LIMIT, 'host source total bound')
    root_file(chosen['python']['path'], TOTAL_LIMIT, chosen['python']['sha256'])
    return root
