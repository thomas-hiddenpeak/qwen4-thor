"""CPU-only gateway actor for Guardian lifecycle tests; never packaged."""
import argparse
import os
from pathlib import Path
import signal
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_recovery import control


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--diagnostics-dir', type=Path)
    args = parser.parse_args()
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
            mode = ''
            if args.diagnostics_dir is not None:
                marker = args.diagnostics_dir/'gateway-diagnostic-mode'
                if marker.exists():mode = marker.read_text()
            if mode == 'reject':
                channel.unbound('gateway_capacity_mismatch', snapshot['model_generation'])
            elif mode in ('malformed', 'stale'):
                channel._send('unbound', binding_failure=dict(
                    reason='PRIVATE_DIAGNOSTIC_MUST_NOT_ESCAPE' if mode == 'malformed'
                           else 'gateway_identity_mismatch',
                    model_generation=snapshot['model_generation']+(mode == 'stale')))
            elif mode == 'legacy':
                channel.unbound()
            else:
                channel.bound(snapshot['model_generation'])
        time.sleep(0.05)


if __name__ == '__main__':
    main()
