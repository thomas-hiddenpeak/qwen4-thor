"""Internal CPU exec helper with parent-death protection; no recovery policy."""
import argparse
import ctypes
import os
from pathlib import Path
import signal
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_recovery import journal, ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent-pid', type=int, required=True)
    parser.add_argument('--parent-start', type=int, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    ledger.require(command and Path(command[0]).is_absolute(),
                   'absolute exec target required')
    libc = ctypes.CDLL(None, use_errno=True)
    ledger.require(libc.prctl(1, signal.SIGTERM, 0, 0, 0) == 0 and
                   os.getppid() == args.parent_pid and
                   journal.process_start(args.parent_pid) == args.parent_start,
                   'CPU exec parent identity changed')
    os.execv(command[0], command)


if __name__ == '__main__':
    main()
