"""Check that a disconnected diagnostic reader preserves the serve CLI outcome."""
import argparse
import os
from pathlib import Path
import signal
import subprocess


def check(binary):
    # Option parsing returns before model or device initialization.
    args = [str(binary.resolve(strict=True)), 'serve', '--port', 'invalid-port']
    normal = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            restore_signals=True, timeout=10)
    if normal.returncode != 2 or b'invalid numeric value for --port' not in normal.stderr:
        raise ValueError('unexpected serve option validation result')

    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        closed = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=write_fd,
                                restore_signals=True, timeout=10)
    finally:
        os.close(write_fd)
    if closed.returncode != normal.returncode:
        reason = signal.strsignal(-closed.returncode) if closed.returncode < 0 else closed.returncode
        raise ValueError(f'closed diagnostic reader changed the result to {reason}')
    return closed.returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    print(check(parser.parse_args().binary))
