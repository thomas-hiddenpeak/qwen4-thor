"""Separate memcg/physical-memory observations; never a signal or launch grant."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.serve_recovery import jobs, ledger, resource_kernel as kernel, resource_launch, store
from tools.serve_service import memory

FORMAT = 'q4t-native-resource-memory-v1'
MAX_AGE = 2*ledger.SECOND


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


def validate(value):
    ledger.keys(value, ('format', 'binding_sha256', 'sampled_monotonic_ns', 'memcg', 'memory_events',
                       'pids_events', 'physical', 'physical_ram_limit', 'high_bytes', 'max_bytes'))
    ledger.require(value['format'] == FORMAT and type(value['binding_sha256']) is str and
        ledger.HASH.fullmatch(value['binding_sha256']) and ledger.integer(value['sampled_monotonic_ns']) and
        value['physical_ram_limit'] is False and ledger.integer(value['high_bytes'], minimum=1) and
        ledger.integer(value['max_bytes'], minimum=value['high_bytes']), 'invalid resource memory observation')
    ledger.keys(value['memcg'], ('memory.current', 'memory.peak', 'memory.swap.current', 'pids.current'))
    for group in ('memcg', 'memory_events', 'pids_events'):
        counters = value[group]
        ledger.require(type(counters) is dict and 0 < len(counters) <= 32 and all(
            type(k) is str and 0 < len(k) <= 64 and ledger.integer(v) for k,v in counters.items()),
            'invalid resource counters')
    ledger.require({'high','max','oom','oom_kill'} <= value['memory_events'].keys() and
                   'max' in value['pids_events'], 'missing resource event counters')
    from tools.serve_recovery import profile_memory
    profile_memory.available(value['physical'])
    return value


def fresh(value, now):
    try:
        validate(value)
        return 0 <= now-value['sampled_monotonic_ns'] <= MAX_AGE
    except (OSError, ValueError, KeyError, TypeError): return False


def recovery_clear(value, now, additional_bytes):
    return (fresh(value, now) and ledger.integer(additional_bytes) and
            value['memcg']['memory.current']+additional_bytes < value['high_bytes'])


class Pressure:
    def __init__(self): self.reset()

    def reset(self): self.last = self.since = None

    def assess(self, value, now, seconds):
        if not fresh(value, now): self.reset(); return False
        at = value['sampled_monotonic_ns']
        if self.last is not None:
            if at == self.last: return False
            if at < self.last or at-self.last > MAX_AGE: self.reset()
        self.last = at
        if value['memcg']['memory.current'] < value['high_bytes']:
            self.since = None; return False
        if self.since is None: self.since = at
        return at-self.since >= seconds*ledger.SECOND


class Observer:
    """One bounded owned worker; failures never restart a model or free a live slot."""
    def __init__(self, binding, lease_fd, *, factory=jobs.Job, clock=time.monotonic):
        self.binding = deepcopy(resource_launch.validate(binding)); self.lease_fd = lease_fd
        self.factory, self.clock = factory, clock
        self.job = None; self.value = None; self.next_at = 0; self.reason = 'resource_observation_pending'

    def unknown(self, reason): self.value = None; self.reason = reason

    def snapshot(self):
        return deepcopy(self.value) if fresh(self.value, int(self.clock()*ledger.SECOND)) else None

    def tick(self, *, allow_query=True):
        if self.job is not None:
            try: complete = self.job.tick()
            except Exception:
                self.unknown('resource_collector_failed')
                try: self.job.kill('collector_failed')
                except Exception: pass
                return  # The same owned slot is retained until actual reap.
            if self.job.reason is not None: self.unknown('resource_observation_unavailable')
            if not complete: return
            try:
                ledger.require(self.job.process.returncode == 0 and self.job.reason is None,
                               'resource collector failed')
                value = validate(store.decode(bytes(self.job.output)))
                ledger.require(value['binding_sha256'] == ledger.digest(self.binding) and
                    fresh(value, int(self.clock()*ledger.SECOND)), 'resource observation binding/age differs')
                self.value = value; self.reason = None
            except (OSError, ValueError, KeyError, TypeError): self.unknown('resource_observation_unavailable')
            self.job.close(); self.job = None
            self.next_at = self.clock()+(1 if self.value is not None else 5)
        if allow_query and self.clock() >= self.next_at:
            try:
                self.job = self.factory([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                    '--binding', ledger.canonical(self.binding).decode()], lease_fd=self.lease_fd,
                    timeout=2, maximum=16384, clock=self.clock)
            except Exception:
                self.unknown('resource_collector_start_failed'); self.next_at = self.clock()+5

    def cancel(self):
        if self.job is not None: self.job.kill('stopping')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', required=True); args = parser.parse_args()
    ledger.require(len(args.binding) <= 8192, 'resource memory binding bound')
    print(json.dumps(sample(store.decode(args.binding)), sort_keys=True))
