"""Private, fixed-size durable state with stable inode claims."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from tools.serve_watchdog.core import MAXIMUM, parse, require


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) +
            '\n').encode()


def attributes(info, directory=False):
    require((stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode) and
            info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0 and
            (directory or info.st_nlink == 1), 'private_owned_file_required')


class Directory:
    def __init__(self, path):
        self.path = Path(path).absolute()
        require(self.path == self.path.resolve(strict=True), 'state_symlink')
        self.fd = os.open(self.path, os.O_DIRECTORY | os.O_RDONLY |
                          os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            attributes(os.fstat(self.fd), True)
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    def consistent(self):
        before, after = os.fstat(self.fd), self.path.lstat()
        require((before.st_dev, before.st_ino) ==
                (after.st_dev, after.st_ino) and
                stat.S_ISDIR(after.st_mode), 'state_directory_replaced')

    def read(self, name, maximum=MAXIMUM):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                     os.O_CLOEXEC, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            attributes(info)
            require(info.st_size <= maximum, 'file_size')
            with os.fdopen(fd, 'rb', closefd=False) as source:
                data = source.read(maximum + 1)
            require(len(data) <= maximum, 'file_size')
            self.consistent()
            return data
        finally:
            os.close(fd)

    def json(self, name):
        return parse(self.read(name))

    def write(self, name, raw, replace=True):
        require(isinstance(raw, bytes) and len(raw) <= MAXIMUM, 'write_size')
        self.consistent()
        try:
            info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            attributes(info)
            require(replace, 'preserve_existing_file')
        # A leftover .new means the previous write did not complete. Preserve
        # it and fail closed; do not guess whether it is safe to overwrite.
        temp = name + '.new'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as output:
                output.write(raw)
                output.flush()
                os.fsync(fd)
            self.consistent()
            os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            os.close(fd)

    @contextmanager
    def claim(self, name='state.lock', wait=1):
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                     os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            attributes(info)
            require(info.st_size == 0, 'claim_content')
            until = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < until, 'state_busy')
                    time.sleep(0.01)
            current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            require((current.st_dev, current.st_ino) ==
                    (info.st_dev, info.st_ino), 'claim_replaced')
            self.consistent()
            yield fd
        finally:
            os.close(fd)
