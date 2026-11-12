"""Bounded resource observation client, usable by retained controller sources.

This module validates representation only; the selected collector verifies the
original kernel domain. Observation never grants device or model authority.
"""
from copy import deepcopy
import hashlib
import fcntl
import os
from pathlib import Path
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.serve_recovery import jobs, ledger, store

FORMAT = 'q4t-native-resource-memory-v1'
MAX_AGE = 2*ledger.SECOND


def validate_binding(value):
    ledger.keys(value, ('directory', 'publication'))
    ledger.require(type(value['directory']) is str and Path(value['directory']).is_absolute() and
        type(value['publication']) is dict and len(ledger.canonical(value)) <= 8192,
        'invalid resource observation binding')
    public = value['publication']
    ledger.require(public['phase'] == 'ready' and public['admission_enabled'] is True and
        public['device_authorized'] is False and public['model_actions'] is False and
        ledger.identifier(public['service_id']) and ledger.identifier(public['domain_id']) and
        ledger.integer(public['runtime_uid'], minimum=1), 'resource observation selection differs')
    # Complete filesystem, permissions, namespace and limits checks execute in
    # the separately bounded collector, not the healthy controller loop.
    return value


def validate_worker(value):
    ledger.keys(value, ('path', 'sha256'))
    ledger.require(type(value['path']) is str and Path(value['path']).is_absolute() and
        0 < len(value['path']) <= 4096 and type(value['sha256']) is str and ledger.HASH.fullmatch(value['sha256']),
        'invalid fixed resource collector')
    return value


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
    def __init__(self, binding, lease_fd, *, worker, factory=jobs.Job, clock=time.monotonic):
        self.binding = deepcopy(validate_binding(binding)); self.lease_fd = lease_fd
        self.worker = deepcopy(validate_worker(worker))
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
                    '--q4t-resource-worker', ledger.canonical(self.worker).decode(),
                    '--binding', ledger.canonical(self.binding).decode()], lease_fd=self.lease_fd,
                    timeout=2, maximum=16384, clock=self.clock)
            except Exception:
                self.unknown('resource_collector_start_failed'); self.next_at = self.clock()+5

    def cancel(self):
        if self.job is not None: self.job.kill('stopping')


def run_worker(spec, binding):
    """Only this isolated child reads the selected collector file."""
    validate_worker(spec); validate_binding(binding)
    path = Path(spec['path']); fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        ledger.require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
            not before.st_mode & 0o222 and 0 < before.st_size <= 1024**2,
            'resource collector must be a sealed regular source')
        raw = bytearray()
        while len(raw) <= before.st_size:
            part = os.read(fd, min(65536, before.st_size+1-len(raw)))
            if not part: break
            raw.extend(part)
        after = os.fstat(fd)
        ledger.require(len(raw) == before.st_size and all(getattr(before,k) == getattr(after,k)
            for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) and
            hashlib.sha256(raw).hexdigest() == spec['sha256'], 'resource collector identity changed')
    finally: os.close(fd)
    # Execute the verified bytes, not a second path lookup. A fresh interpreter
    # prevents retained imports from shadowing the root collector's modules.
    sealed = os.memfd_create('q4t-resource-collector', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(sealed, raw[offset:])
            ledger.require(written > 0, 'short sealed collector write'); offset += written
        os.lseek(sealed, 0, os.SEEK_SET)
        fcntl.fcntl(sealed, fcntl.F_ADD_SEALS, 15)
        os.set_inheritable(sealed, True)
        bootstrap = ("import os,sys;fd=int(sys.argv[1]);p=sys.argv[2];"
            "data=os.read(fd,1048577);os.close(fd);"
            "sys.argv=[p,*sys.argv[3:]];__file__=p;"
            "exec(compile(data,p,'exec'),globals(),globals())")
        os.execve(sys.executable, [sys.executable, '-I', '-B', '-c', bootstrap,
            str(sealed), str(path), '--binding', ledger.canonical(binding).decode()], dict(os.environ))
    finally: os.close(sealed)



if __name__ == '__main__' and sys.argv[1:2] == ['--q4t-resource-worker']:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--q4t-resource-worker', required=True)
    parser.add_argument('--binding', required=True); args = parser.parse_args()
    ledger.require(len(args.q4t_resource_worker) <= 8192 and len(args.binding) <= 8192,
                   'resource worker input bound')
    run_worker(store.decode(args.q4t_resource_worker), store.decode(args.binding))
