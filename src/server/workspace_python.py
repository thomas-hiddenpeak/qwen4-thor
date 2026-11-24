"""In-memory IO for a single isolated interpreter run. The parent owns writeback."""

import encodings.cp437
import encodings.utf_8
import base64
import builtins
import collections
import contextlib
import csv
import datetime
import decimal
import fractions
import hashlib
import io
import itertools
import json
import math
import os
import pathlib
import posixpath
import re
import statistics
import stat
import sys
import zipfile

ROOT = '/mnt/data'
MAX_FILE = 8 * 1024 * 1024
MAX_FILES = 256
MAX_TOTAL = 16 * 1024 * 1024
MAX_LOG = 64 * 1024


class Workspace:
    def __init__(self, files):
        self.files = {}
        self.initial = {}
        self.writable = set()
        self.cwd = ROOT
        self.dirs = {ROOT, '/mnt', '/'}
        for entry in files:
            path = entry['path']
            if path != self.resolve(path) or path in self.files:
                raise ValueError('invalid workspace path')
            data = base64.b64decode(entry['data'], validate=True)
            self.store(path, data)
            self.initial[path] = data
            if entry['writable']:
                self.writable.add(path)

    def resolve(self, path):
        path = os.fspath(path)
        if not isinstance(path, str) or not path or '\x00' in path:
            raise ValueError('invalid workspace path')
        path = posixpath.normpath(posixpath.join(self.cwd, path))
        return path

    def store(self, path, data):
        size = len(data)
        if size > MAX_FILE or len(self.files) + (path not in self.files) > MAX_FILES:
            raise OSError('workspace file limit')
        if (
            sum(map(len, self.files.values())) - len(self.files.get(path, b'')) + size
            > MAX_TOTAL
        ):
            raise OSError('workspace size limit')
        self.files[path] = data
        self.dirs.update(str(p) for p in pathlib.PurePosixPath(path).parents)

    def check_writable(self, path):
        if not path.startswith(ROOT + '/') or (
            path in self.initial and path not in self.writable
        ):
            raise PermissionError(path)
        if posixpath.dirname(path) not in self.dirs:
            raise FileNotFoundError(posixpath.dirname(path))

    def open(
        self,
        file,
        mode='r',
        buffering=-1,
        encoding=None,
        errors=None,
        newline=None,
        closefd=True,
        opener=None,
    ):
        path = self.resolve(file)
        if not closefd or opener is not None:
            raise ValueError('file descriptor options are unavailable')
        if (
            not isinstance(mode, str)
            or len(set(mode)) != len(mode)
            or any(c not in 'rwxa+bt' for c in mode)
            or ('t' in mode and 'b' in mode)
        ):
            raise ValueError('invalid open mode')
        flags = [c for c in 'rwxa' if c in mode]
        if len(flags) != 1:
            raise ValueError('expected one file access mode')
        flag = flags[0]
        writable = flag != 'r' or '+' in mode
        if writable:
            self.check_writable(path)
        if path in self.dirs:
            raise IsADirectoryError(path)
        if flag == 'r' and path not in self.files:
            raise FileNotFoundError(path)
        if flag == 'x' and path in self.files:
            raise FileExistsError(path)
        if 'b' in mode and any(v is not None for v in (encoding, errors, newline)):
            raise ValueError('binary mode forbids encoding, errors and newline')
        if flag in 'wx' or (flag == 'a' and path not in self.files):
            self.store(path, b'')
        stream = WorkspaceFile(self, path, mode, writable, flag == 'a')
        if 'b' in mode:
            return stream
        return io.TextIOWrapper(
            stream,
            encoding=encoding or 'utf-8',
            errors=errors,
            newline=newline,
            write_through=True,
        )

    def listdir(self, path='.'):
        directory = self.resolve(path)
        if directory not in self.dirs:
            raise NotADirectoryError(directory)
        return sorted(
            {
                posixpath.basename(n)
                for n in self.files.keys() | self.dirs
                if n != directory and posixpath.dirname(n) == directory
            }
        )

    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None:
            raise io.UnsupportedOperation('directory descriptors')
        path = self.resolve(path)
        if path in self.dirs:
            mode, size = stat.S_IFDIR | 0o555, 0
        elif path in self.files:
            mode = stat.S_IFREG | (0o644 if path.startswith(ROOT + '/') else 0o444)
            size = len(self.files[path])
        else:
            raise FileNotFoundError(path)
        return os.stat_result((mode, 0, 0, 1, 0, 0, size, 0, 0, 0))

    def mkdir(self, path, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise io.UnsupportedOperation('directory descriptors')
        path = self.resolve(path)
        self.check_writable(path)
        if path in self.dirs or path in self.files:
            raise FileExistsError(path)
        if len(self.dirs) >= MAX_FILES * 4:
            raise OSError('workspace directory limit')
        self.dirs.add(path)

    def unlink(self, path, *, dir_fd=None):
        if dir_fd is not None:
            raise io.UnsupportedOperation('directory descriptors')
        path = self.resolve(path)
        self.check_writable(path)
        if path in self.dirs:
            raise IsADirectoryError(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]

    def chdir(self, path):
        path = self.resolve(path)
        if path not in self.dirs:
            raise NotADirectoryError(path)
        self.cwd = path

    def scandir(self, path='.'):
        return Listing([Member(self, path, name) for name in self.listdir(path)])

    def changes(self):
        return [
            {'path': n, 'data': base64.b64encode(v).decode('ascii')}
            for n, v in sorted(self.files.items())
            if v != self.initial.get(n)
        ] + [
            {'path': n, 'deleted': True}
            for n in sorted(self.initial.keys() - self.files.keys())
        ]


class Member:
    def __init__(self, fs, parent, name):
        self.fs, self.name = fs, name
        self.path = posixpath.join(os.fspath(parent), name)

    def __fspath__(self):
        return self.path

    def stat(self, *, follow_symlinks=True):
        return self.fs.stat(self.path)

    def is_dir(self, *, follow_symlinks=True):
        return stat.S_ISDIR(self.stat().st_mode)

    def is_file(self, *, follow_symlinks=True):
        return stat.S_ISREG(self.stat().st_mode)

    def is_symlink(self):
        return False

    def inode(self):
        return 0


class Listing:
    def __init__(self, entries):
        self.entries = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.entries)

    def close(self):
        self.entries = iter(())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class WorkspaceFile(io.RawIOBase):
    def __init__(self, workspace, path, mode, writable, append):
        self.workspace = workspace
        self.name = path
        self.mode = mode
        self._read = 'r' in mode or '+' in mode
        self._write = writable
        self.append = append
        self.offset = len(self.data) if append else 0

    @property
    def data(self):
        return self.workspace.files[self.name]

    def readable(self):
        return self._read

    def writable(self):
        return self._write

    def seekable(self):
        return True

    def tell(self):
        self._checkClosed()
        return self.offset

    def seek(self, offset, whence=0):
        self._checkClosed()
        if whence not in (0, 1, 2):
            raise ValueError('invalid whence')
        pos = (0, self.offset, len(self.data))[whence] + offset
        if pos < 0:
            raise ValueError('negative seek')
        self.offset = pos
        return pos

    def readinto(self, target):
        self._checkClosed()
        if not self._read:
            raise io.UnsupportedOperation('read')
        data = self.data[self.offset : self.offset + len(target)]
        target[: len(data)] = data
        self.offset += len(data)
        return len(data)

    def write(self, value):
        self._checkClosed()
        if not self._write:
            raise io.UnsupportedOperation('write')
        raw = bytes(value)
        at = len(self.data) if self.append else self.offset
        end = at + len(raw)
        if end > MAX_FILE:
            raise OSError('workspace file limit')
        if raw:
            self.workspace.store(
                self.name, self.data[:at].ljust(at, b'\0') + raw + self.data[end:]
            )
            self.offset = end
        return len(raw)

    def truncate(self, size=None):
        self._checkClosed()
        if not self._write:
            raise io.UnsupportedOperation('truncate')
        size = self.offset if size is None else size
        if not 0 <= size <= MAX_FILE:
            raise ValueError('invalid file size')
        self.workspace.store(self.name, self.data[:size].ljust(size, b'\0'))
        return size


class Transcript(io.StringIO):
    def __init__(self):
        super().__init__()
        self.size = 0

    def write(self, value):
        self.size += len(value.encode('utf-8'))
        if self.size > MAX_LOG:
            raise OSError('interpreter output limit')
        return super().write(value)


def evaluate(request):
    fs = Workspace(request['files'])
    log = Transcript()
    builtins.open = io.open = fs.open
    os.listdir, os.scandir = fs.listdir, fs.scandir
    os.stat = os.lstat = fs.stat
    os.remove = os.unlink = fs.unlink
    os.chdir, os.mkdir = fs.chdir, fs.mkdir
    os.getcwd = lambda: fs.cwd
    error = False
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        try:
            exec(
                compile(request['code'], '<container>', 'exec'),
                {'__name__': '__main__'},
            )
        except BaseException as exc:
            message = type(exc).__name__ + ': ' + str(exc)[:1024]
            if log.size <= MAX_LOG - 4096:
                print(message, file=log)
            else:
                io.StringIO.write(log, '\n[interpreter output limit]\n')
            error = True
    return {'logs': log.getvalue(), 'error': error, 'files': fs.changes()}
