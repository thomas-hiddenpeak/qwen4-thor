"""Bounded anonymous CPU pressure, released on EOF, parent death or idle expiry.

This is a validation client outside the model cgroup, not capacity offload.
There is no CLI override for its allocation cap or safety reserves.
"""
import ctypes
import json
import mmap
import os
from pathlib import Path
import select
import selectors
import signal
import subprocess
import sys
import time

MIB = 1024**2
CHUNK = 128 * MIB
MAX_BYTES = 8192 * MIB
MIN_AVAILABLE = 20480 * MIB
MIN_FREE = 12288 * MIB
IDLE_SECONDS = 10
TOTAL_SECONDS = 120


def memory():
    raw = Path('/proc/meminfo').read_text()
    return parse_memory(raw)


def parse_memory(raw):
    if not raw.endswith('\n') or len(raw) > 16384:
        raise RuntimeError('incomplete memory observation')
    fields = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(':')
        if key not in ('MemTotal', 'MemFree', 'MemAvailable'):
            continue
        parts = rest.split()
        if key in fields or len(parts) != 2 or parts[1] != 'kB' or not parts[0].isdigit():
            raise RuntimeError('invalid memory observation')
        fields[key] = int(parts[0]) * 1024
    if (len(fields) != 3 or not fields['MemTotal'] or
            not 0 <= fields['MemFree'] <= fields['MemTotal'] or
            not 0 <= fields['MemAvailable'] <= fields['MemTotal']):
        raise RuntimeError('missing or contradictory memory observation')
    return fields


def safe(observed, held, additional=0):
    if (additional not in (0, CHUNK) or held < 0 or
            held + additional > MAX_BYTES or
            observed['MemAvailable'] < MIN_AVAILABLE + additional or
            observed['MemFree'] < MIN_FREE + additional):
        raise RuntimeError('pressure safety reserve/cap reached')


def emit(value):
    print(json.dumps(value), flush=True)


def helper():
    blocks, pending = [], bytearray()
    parent = os.getppid()
    parent_fd = os.pidfd_open(parent)
    started = touched = time.monotonic()
    libc = ctypes.CDLL(None)
    libc.memset.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t)
    libc.memset.restype = ctypes.c_void_p
    failure = None
    try:
        if os.getppid() != parent:
            raise RuntimeError('parent changed during binding')
        with selectors.DefaultSelector() as selector:
            selector.register(parent_fd, selectors.EVENT_READ, 'parent')
            selector.register(sys.stdin.fileno(), selectors.EVENT_READ, 'input')
            while True:
                now = time.monotonic()
                if now - started >= TOTAL_SECONDS or now - touched >= IDLE_SECONDS:
                    raise RuntimeError('pressure watchdog expired')
                observed = memory()
                safe(observed, len(blocks) * CHUNK)
                for key, _ in selector.select(.1):
                    if key.data == 'parent':
                        raise RuntimeError('pressure parent exited')
                    part = os.read(sys.stdin.fileno(), 256)
                    if not part:
                        return 0
                    pending.extend(part)
                    if len(pending) > 256:
                        raise RuntimeError('pressure command too large')
                if b'\n' not in pending:
                    continue
                line, _, tail = pending.partition(b'\n')
                pending = bytearray(tail)
                op = json.loads(line)['op']
                touched = time.monotonic()
                before = memory()
                if op == 'add':
                    safe(before, len(blocks) * CHUNK, CHUNK)
                    block = mmap.mmap(-1, CHUNK, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
                    blocks.append(block)
                    address = ctypes.addressof(ctypes.c_char.from_buffer(block))
                    libc.memset(address, 1, CHUNK)
                elif op == 'remove':
                    if not blocks:
                        raise RuntimeError('no pressure allocation to release')
                    blocks.pop().close()
                elif op == 'release':
                    while blocks:
                        blocks.pop().close()
                elif op != 'status':
                    raise RuntimeError('unknown pressure command')
                after = memory()
                safe(after, len(blocks) * CHUNK)
                emit(dict(op=op, allocated=len(blocks)*CHUNK, before=before,
                          after=after, monotonic=time.monotonic(), pid=os.getpid()))
    except BaseException as error:
        failure = repr(error)
        return 1
    finally:
        while blocks:
            blocks.pop().close()
        os.close(parent_fd)
        emit(dict(final=True, allocated=0, error=failure,
                  monotonic=time.monotonic(), pid=os.getpid()))


class Pressure:
    def __init__(self, folder):
        self.stderr = (folder / 'pressure-stderr.log').open('xb')
        self.receipts = (folder / 'pressure.jsonl').open('xb', buffering=0)
        self.process = subprocess.Popen(
            ['/usr/bin/python3', '-B', str(Path(__file__).resolve()), '--helper'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, bufsize=0)
        self.fd = os.pidfd_open(self.process.pid)
        self.buffer = bytearray()
        self.closed = False

    def read(self):
        deadline = time.monotonic() + 5
        while b'\n' not in self.buffer:
            ready, _, _ = select.select([self.process.stdout], [], [],
                                         max(0, deadline-time.monotonic()))
            if not ready:
                raise RuntimeError('pressure response deadline')
            part = os.read(self.process.stdout.fileno(), 65536)
            if not part:
                raise RuntimeError('pressure helper exited')
            self.buffer.extend(part)
            if len(self.buffer) > 65536:
                raise RuntimeError('pressure receipt too large')
        line, _, tail = self.buffer.partition(b'\n')
        self.buffer = bytearray(tail)
        self.receipts.write(line + b'\n')
        os.fsync(self.receipts.fileno())
        return json.loads(line)

    def command(self, op):
        self.process.stdin.write(json.dumps(dict(op=op)).encode() + b'\n')
        row = self.read()
        if row.get('op') != op or row.get('error'):
            raise RuntimeError('pressure helper failure: ' + repr(row))
        return row

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                signal.pidfd_send_signal(self.fd, signal.SIGKILL)
                self.process.wait(timeout=5)
            tail = bytes(self.buffer) + self.process.stdout.read()
            self.receipts.write(tail)
            os.fsync(self.receipts.fileno())
        finally:
            self.process.stdout.close()
            os.close(self.fd)
            self.receipts.close()
            self.stderr.close()


if __name__ == '__main__':
    if sys.argv[1:] != ['--helper']:
        raise SystemExit('internal --helper only')
    sys.exit(helper())
