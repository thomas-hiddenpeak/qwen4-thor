"""Actual native-domain and physical-memory collection, isolated from the core."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.serve_recovery import ledger, resource_kernel as kernel, resource_launch, store
from tools.serve_recovery.resource_client import (FORMAT, MAX_AGE, Observer, Pressure,
    fresh, recovery_clear, validate)
from tools.serve_service import memory


def sample(binding):
    resource_launch.validate(binding)
    expected = binding['publication']
    current = resource_launch.install.read_public(binding['directory'])
    ledger.require(current == expected, 'resource memory installation changed')
    with kernel.Scope(expected['record']) as scope:
        scope.member('service')
        fd = scope.fds['service']; values = {}
        for name in ('memory.current', 'memory.peak', 'memory.swap.current', 'pids.current'):
            raw = kernel.read(fd, name)
            ledger.require(raw.isdecimal() and len(raw) <= 20, 'invalid resource memory counter')
            values[name] = int(raw)
        events = kernel.counters(kernel.read(fd, 'memory.events'))
        task_events = kernel.counters(kernel.read(fd, 'pids.events'))
        scope.check(); scope.member('service')
    # These are independent accounting domains, never added as "total RAM".
    physical = memory.read()
    return validate(dict(format=FORMAT, binding_sha256=ledger.digest(binding),
        sampled_monotonic_ns=time.monotonic_ns(), memcg=values, memory_events=events,
        pids_events=task_events, physical=physical, physical_ram_limit=False,
        high_bytes=expected['record']['policy']['service']['memory_high_bytes'],
        max_bytes=expected['record']['policy']['service']['memory_max_bytes']))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', required=True); args = parser.parse_args()
    ledger.require(len(args.binding) <= 8192, 'resource memory binding bound')
    print(json.dumps(sample(store.decode(args.binding)), sort_keys=True))
