"""CPU-only gateway actor for Guardian lifecycle tests; never packaged."""
import os
from pathlib import Path
import signal
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_recovery import control


def main():
    channel = control.GatewayChannel()
    # Deliberately escape the initial process group, while remaining in the
    # gateway's subreaper domain. A controller failure must preserve this leaf.
    middle = os.fork()
    if middle == 0:
        os.setsid()
        if os.fork() == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                signal.pause()
        os._exit(0)
    os.waitpid(middle, 0)
    channel.event('ready')
    while True:
        channel.event('heartbeat')
        snapshot = control.binding(channel.service_id)
        if snapshot['model_ready'] and snapshot['binding']:
            channel.bound(snapshot['model_generation'])
        time.sleep(0.05)


if __name__ == '__main__':
    main()
