"""Exercise serve's inherited diagnostic output before model initialization."""
import argparse
import os
from pathlib import Path
import socket
import subprocess


def fill(fd):
    """Fill a private test sink, then restore the inherited blocking mode."""
    count = 0
    os.set_blocking(fd, False)
    try:
        for chunk in (b'x' * 4096, b'x'):
            try:
                while True:
                    count += os.write(fd, chunk)
            except BlockingIOError:
                pass
    finally:
        os.set_blocking(fd, True)
    return count


def check(binary):
    args = [str(binary.resolve(strict=True)), 'serve', '--port', 'invalid-port']

    def run(sink):
        child = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=sink,
                               restore_signals=True, timeout=3)
        if child.returncode != 2:
            raise ValueError(f'expected option error, got {child.returncode}')
        return child

    normal = run(subprocess.PIPE)
    if b'invalid numeric value for --port' not in normal.stderr:
        raise ValueError('missing serve option error')

    read_fd, write_fd = os.pipe()
    try:
        if not fill(write_fd):
            raise ValueError('empty test pipe')
        run(write_fd)
        if not os.get_blocking(write_fd):
            raise ValueError('serve changed the inherited pipe flags')
        os.close(read_fd)
        read_fd = -1
        run(write_fd)
    finally:
        if read_fd >= 0:
            os.close(read_fd)
        os.close(write_fd)

    reader, writer = socket.socketpair()
    with reader, writer:
        if not fill(writer.fileno()):
            raise ValueError('empty test socket')
        run(writer.fileno())
        if not os.get_blocking(writer.fileno()):
            raise ValueError('serve changed the inherited socket flags')
    return normal.returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    print(check(parser.parse_args().binary))
