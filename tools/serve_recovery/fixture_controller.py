"""CPU-only Guardian fixture. Never packaged as a production controller."""
import argparse
import os
from pathlib import Path
import signal
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_recovery.control import ControllerChannel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--escape-child', action='store_true')
    args = parser.parse_args()
    channel = ControllerChannel()
    if args.escape_child:
        middle = os.fork()
        if middle == 0:
            os.setsid()
            leaf = os.fork()
            if leaf == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                # CPU-only orphan deliberately keeps its inherited lease FD.
                while True:
                    signal.pause()
            os._exit(0)
        os.waitpid(middle, 0)
    channel.event('ready')
    try:
        while True:
            time.sleep(0.1)
            channel.event('heartbeat')
    finally:
        channel.close()


if __name__ == '__main__':
    main()
