"""Bounded FIFO collection and streaming single-frame zstd validation.

No CUDA/profiler APIs. The caller must stop its actual log writers before
finish(); EOF and byte equality do not establish NVLOG event completeness.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import time

LOGICAL_LIMIT = 8 * 1024 ** 3
COMPRESSED_LIMIT = 256 * 1024 ** 2
LINE_LIMIT = 1024 ** 2
BLOCK = 128 * 1024
LIBRARY = '/usr/lib/aarch64-linux-gnu/libzstd.so.1.5.5'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _path(path):
    path = Path(path).absolute()
    require(path.parent.resolve() == path.parent, 'parent must be canonical, without symlinks')
    return path


def _same(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _json(path):
    path = _path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 65536,
                'invalid small receipt')
        data = os.read(fd, 65537)
        require(len(data) <= 65536 and _same(info, path.lstat()), 'receipt identity changed')
        return json.loads(data)
    finally:
        os.close(fd)


class Collector:
    """Own exactly one native worker and one private FIFO/control pipe.

    start() returns after the native reader and keeper are ready. finish()
    closes the keeper by a private control message, then requires true EOF.
    It does not stop arbitrary writers or certify their process lifecycle.
    """
    def __init__(self, binary, directory, *, timeout_seconds=900,
                 logical_limit=LOGICAL_LIMIT, compressed_limit=COMPRESSED_LIMIT):
        require(type(timeout_seconds) is int and 0 < timeout_seconds <= 3600,
                'invalid worker deadline')
        require(type(logical_limit) is int and 0 < logical_limit <= LOGICAL_LIMIT and
                type(compressed_limit) is int and 0 < compressed_limit <= COMPRESSED_LIMIT,
                'limits may only be lowered for fixed CPU boundary contracts')
        self.binary, self.directory = _path(binary), _path(directory)
        self.timeout = timeout_seconds
        self.logical_limit, self.compressed_limit = logical_limit, compressed_limit
        self.fifo_path = self.directory/'input.fifo'
        self.compressed_path = self.directory/'nvlog.zst'
        self.receipt_path, self.ready_path = self.directory/'receipt.json', self.directory/'ready.json'
        self.child = self.control = self.log = None
        self.identity = None
        self.finish_sent = False
        self.complete = False
        self.deadline = None

    def start(self):
        require(self.child is None and self.deadline is None, 'collector start is once-only')
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        os.mkfifo(self.fifo_path, 0o600)
        self.identity = self.fifo_path.lstat()
        reader, self.control = os.pipe2(os.O_CLOEXEC)
        self.deadline = time.monotonic()+self.timeout
        try:
            self.log = (self.directory/'worker.log').open('xb')
            command = [str(self.binary), '--fifo', str(self.fifo_path),
                '--output', str(self.compressed_path), '--ready', str(self.ready_path),
                '--receipt', str(self.receipt_path), '--fifo-device', str(self.identity.st_dev),
                '--fifo-inode', str(self.identity.st_ino), '--control-fd', str(reader),
                '--logical-limit', str(self.logical_limit),
                '--compressed-limit', str(self.compressed_limit),
                '--timeout-seconds', str(self.timeout)]
            (self.directory/'command.json').write_text(json.dumps(command)+'\n')
            self.child = subprocess.Popen(command, pass_fds=(reader,), close_fds=True,
                                          stdout=self.log, stderr=subprocess.STDOUT)
            while not self.ready_path.exists():
                self.check()
                time.sleep(.005)
            ready = _json(self.ready_path)
            require(ready.get('ready') is True and ready.get('pid') == self.child.pid and
                    ready.get('fifo_device') == self.identity.st_dev and
                    ready.get('fifo_inode') == self.identity.st_ino, 'native ready identity differs')
            self.check()
            return self
        except BaseException:
            self.abort()
            raise
        finally:
            os.close(reader)

    def check(self):
        require(self.child is not None, 'collector was not started')
        require(self.deadline is not None and time.monotonic() < self.deadline,
                'collector deadline exhausted')
        require(_same(self.identity, self.fifo_path.lstat()) and
                stat.S_ISFIFO(self.fifo_path.lstat().st_mode), 'FIFO replaced')
        code = self.child.poll()
        require(code is None or self.complete, 'native encoder exited before successful finish')

    def write_chunks(self, chunks):
        """CPU producer/replay convenience; no separate producer process."""
        self.check()
        fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            require(_same(self.identity, os.fstat(fd)), 'producer opened another FIFO')
            for chunk in chunks:
                require(isinstance(chunk, (bytes, bytearray)) and len(chunk) <= BLOCK,
                        'producer chunks must be bounded bytes')
                view = memoryview(chunk)
                while view:
                    self.check()
                    try:
                        count = os.write(fd, view)
                        require(count > 0, 'producer write made no progress')
                        view = view[count:]
                    except BlockingIOError:
                        select.select([], [fd], [], .02)
        finally:
            os.close(fd)

    def finish(self, *, timeout_seconds=10):
        require(not self.finish_sent and not self.complete, 'finish is once-only')
        try:
            self.check()
            self.finish_sent = True
            os.write(self.control, b'F')
            os.close(self.control)
            self.control = None
            limit = min(self.deadline, time.monotonic()+timeout_seconds)
            require(timeout_seconds > 0, 'invalid finish timeout')
            self.child.wait(timeout=max(.001, limit-time.monotonic()))
            require(self.child.returncode == 0, 'native stream did not finish normally')
            receipt = _json(self.receipt_path)
            require(receipt.get('status') == 'VALID' and receipt.get('pid') == self.child.pid and
                    receipt.get('input_eof') is True and receipt.get('frame_completed') is True and
                    receipt.get('fsynced') is True, 'native completion receipt invalid')
            require((receipt.get('level'), receipt.get('window_log'), receipt.get('workers'),
                     receipt.get('checksum')) == (1, 20, 0, True) and
                    receipt.get('logical_limit') == self.logical_limit and
                    receipt.get('compressed_limit') == self.compressed_limit and
                    0 < receipt.get('controlled_workspace_bytes', 0) <= 16*1024**2 and
                    receipt.get('address_space_limit_bytes') == 64*1024**2,
                    'native frozen configuration or memory bounds differ')
            info = self.compressed_path.lstat()
            require(stat.S_ISREG(info.st_mode) and
                    (info.st_dev, info.st_ino) == (receipt['output_device'], receipt['output_inode']) and
                    info.st_size == receipt['compressed_bytes'] and not info.st_mode & 0o222,
                    'completed compressed identity differs')
            self.complete = True
            return receipt
        except BaseException:
            self.abort()
            raise
        finally:
            if self.log is not None:
                self.log.close()
                self.log = None

    def abort(self):
        """Stop only this directly owned Popen; partial files remain evidence."""
        if self.control is not None:
            os.close(self.control)
            self.control = None
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=2)
        if self.log is not None:
            self.log.close()
            self.log = None


class InBuffer(ctypes.Structure):
    _fields_ = [('src', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]


class OutBuffer(ctypes.Structure):
    _fields_ = [('dst', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]


def _library():
    library = ctypes.CDLL(LIBRARY)
    library.ZSTD_createDStream.argtypes = []
    library.ZSTD_createDStream.restype = ctypes.c_void_p
    library.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
    library.ZSTD_freeDStream.restype = ctypes.c_size_t
    library.ZSTD_DCtx_setParameter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    library.ZSTD_DCtx_setParameter.restype = ctypes.c_size_t
    library.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(OutBuffer), ctypes.POINTER(InBuffer)]
    library.ZSTD_decompressStream.restype = ctypes.c_size_t
    library.ZSTD_isError.argtypes = [ctypes.c_size_t]
    library.ZSTD_isError.restype = ctypes.c_uint
    library.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
    library.ZSTD_getErrorName.restype = ctypes.c_char_p
    return library


def iter_chunks(path, *, receipt=None, logical_limit=LOGICAL_LIMIT,
                compressed_limit=COMPRESSED_LIMIT, result=None):
    """Yield ≤128KiB blocks; validity exists only after complete exhaustion.

    Exactly one checksum-protected frame is required. Extra frames/trailing
    bytes are rejected, including when they begin in a later input block.
    """
    require(type(logical_limit) is int and 0 < logical_limit <= LOGICAL_LIMIT and
            type(compressed_limit) is int and 0 < compressed_limit <= COMPRESSED_LIMIT,
            'invalid decoder caps')
    path = _path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    library, context = None, None
    try:
        first = os.fstat(fd)
        require(stat.S_ISREG(first.st_mode) and 0 < first.st_size <= compressed_limit,
                'compressed source is not bounded regular data')
        library = _library()
        context = library.ZSTD_createDStream()
        require(context, 'zstd decoder allocation failed')
        code = library.ZSTD_DCtx_setParameter(context, 100, 20)  # windowLogMax
        require(not library.ZSTD_isError(code), 'decoder window cap failed')
        raw_hash, packed_hash = hashlib.sha256(), hashlib.sha256()
        logical = packed = 0
        ended = False
        out_storage = ctypes.create_string_buffer(BLOCK)
        while True:
            chunk = os.read(fd, BLOCK)
            if not chunk:
                break
            require(not ended, 'trailing bytes or extra zstd frame')
            if packed == 0:
                require(len(chunk) >= 5 and chunk[:4] == b'\x28\xb5\x2f\xfd' and
                        chunk[4] & 4 and not chunk[4] & 3,
                        'expected checksum-protected zstd frame without dictionary')
            packed += len(chunk)
            require(packed <= compressed_limit, 'compressed cap exceeded')
            packed_hash.update(chunk)
            source = ctypes.create_string_buffer(chunk)
            incoming = InBuffer(ctypes.cast(source, ctypes.c_void_p), len(chunk), 0)
            while True:
                outgoing = OutBuffer(ctypes.cast(out_storage, ctypes.c_void_p), BLOCK, 0)
                before = incoming.pos
                remaining = library.ZSTD_decompressStream(context, ctypes.byref(outgoing), ctypes.byref(incoming))
                require(not library.ZSTD_isError(remaining),
                        'zstd frame/checksum failed: ' + library.ZSTD_getErrorName(remaining).decode())
                require(incoming.pos > before or outgoing.pos > 0 or remaining == 0 or
                        incoming.pos == incoming.size,
                        'decoder made no progress')
                data = out_storage.raw[:outgoing.pos]
                logical += len(data)
                require(logical <= logical_limit, 'logical cap exceeded')
                raw_hash.update(data)
                if data:
                    yield data
                if remaining == 0:
                    ended = True
                    require(incoming.pos == incoming.size, 'extra frame or trailing data in block')
                    break
                if incoming.pos == incoming.size and outgoing.pos < BLOCK:
                    break
        require(ended, 'truncated zstd frame')
        last, named = os.fstat(fd), path.lstat()
        metadata = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        require(metadata(first) == metadata(last) == metadata(named) and stat.S_ISREG(named.st_mode),
                'compressed source changed during decode')
        actual = dict(status='VALID', logical_bytes=logical, logical_sha256=raw_hash.hexdigest(),
                      compressed_bytes=packed, compressed_sha256=packed_hash.hexdigest(),
                      frames=1, checksum_validated=True, path=str(path),
                      device=first.st_dev, inode=first.st_ino)
        if receipt is not None:
            require(receipt.get('status') == 'VALID' and receipt.get('frame_completed') is True and
                    receipt.get('input_eof') is True and receipt.get('fsynced') is True,
                    'receipt is not complete')
            require(all(actual[key] == receipt[key] for key in
                        ('logical_bytes', 'logical_sha256', 'compressed_bytes', 'compressed_sha256')) and
                    (first.st_dev, first.st_ino) == (receipt['output_device'], receipt['output_inode']),
                    'decoded bytes/hash/identity differ from native receipt')
        if result is not None:
            result.update(actual)
    finally:
        if context:
            library.ZSTD_freeDStream(context)
        os.close(fd)


def verify(path, receipt=None, **limits):
    result = {}
    for _ in iter_chunks(path, receipt=receipt, result=result, **limits):
        pass
    return result


def iter_lines(path, *, receipt=None, max_line=LINE_LIMIT):
    """Raw byte lines with newlines; unterminated or oversized final line fails."""
    require(type(max_line) is int and 0 < max_line <= LINE_LIMIT, 'invalid line bound')
    pending = bytearray()
    for chunk in iter_chunks(path, receipt=receipt):
        cursor = 0
        while cursor < len(chunk):
            end = chunk.find(b'\n', cursor)
            end = len(chunk) if end < 0 else end+1
            require(len(pending)+end-cursor <= max_line, 'NVLOG line exceeds bound')
            pending.extend(chunk[cursor:end])
            cursor = end
            if pending.endswith(b'\n'):
                yield bytes(pending)
                pending.clear()
    require(not pending, 'NVLOG ends in an incomplete line')
