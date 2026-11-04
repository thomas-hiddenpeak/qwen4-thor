"""Bounded loss-detecting log copies and separate long-run resource evidence."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import threading
import time

from tools.serve_service import package as pkg
from tools.serve_pressure import observe

ROTATION_BYTES = 4*1024**2
ENVELOPE = dict(rss=64*1024**2, anon=128*1024**2, fds=2, threads=2)
WARMUP = 16


class EvidenceAbort(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise EvidenceAbort(message)


class StreamCopy:
    """Follow the guard's rename rotation, retaining each old inode until read.

    More than one unseen rotation, truncation, unexpected links, or size limits
    invalidate evidence. Never write to the original log or its directory.
    """
    def __init__(self, source, destination, *, rotation=ROTATION_BYTES,
                 maximum=1024**3):
        self.source, self.rotation, self.maximum = Path(source), rotation, maximum
        self.output = Path(destination).open('xb')
        self.fd = None
        self.identity = None
        self.offset = self.total = self.rotations = 0
        self.digest = hashlib.sha256()
        self.closed = False

    def open_current(self):
        fd = os.open(self.source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            value = os.fstat(fd)
            require(stat.S_ISREG(value.st_mode), 'log is not regular')
            return fd, (value.st_dev, value.st_ino)
        except BaseException:
            os.close(fd)
            raise

    def drain(self):
        require(os.fstat(self.fd).st_size >= self.offset, 'log truncated')
        for _ in range(self.rotation//65536+2):
            data = os.read(self.fd, 65536)
            if not data:
                return
            require(self.total+len(data) <= self.maximum, 'log archive byte limit')
            self.output.write(data)
            self.digest.update(data)
            self.offset += len(data)
            self.total += len(data)
        raise EvidenceAbort('log exceeds expected rotation size')

    def poll(self):
        require(not self.closed, 'log copy is closed')
        previous = self.source.with_name(self.source.name+'.previous')
        if self.fd is None:
            require(not os.path.lexists(previous), 'initial log prefix unobserved')
            try:
                self.fd, self.identity = self.open_current()
            except FileNotFoundError:
                return
        self.drain()
        try:
            current = self.source.lstat()
        except FileNotFoundError:
            # The producer may be between rename and creating the new file.
            prior = previous.lstat()
            require((prior.st_dev, prior.st_ino) == self.identity,
                    'log disappeared without its rotation predecessor')
            return
        require(stat.S_ISREG(current.st_mode), 'current log replaced by non-file')
        if (current.st_dev, current.st_ino) != self.identity:
            prior = previous.lstat()
            require(stat.S_ISREG(prior.st_mode) and
                    (prior.st_dev, prior.st_ino) == self.identity,
                    'one or more log generations missing')
            self.drain()
            require(self.offset == self.rotation, 'incomplete rotation predecessor')
            fd, identity = self.open_current()
            if identity != (current.st_dev, current.st_ino):
                os.close(fd)
                raise EvidenceAbort('log changed while opening next generation')
            os.close(self.fd)
            self.fd, self.identity, self.offset = fd, identity, 0
            self.rotations += 1
            self.drain()
        self.output.flush()

    def close(self):
        if self.closed:
            return
        try:
            self.poll()
            require(self.total > 0, 'log archive is empty')
            return dict(bytes=self.total, sha256=self.digest.hexdigest(),
                        rotations=self.rotations, complete_observed_chain=True)
        finally:
            self.closed = True
            if self.fd is not None:
                os.close(self.fd)
            self.output.close()


class Capture:
    """One bounded collector; observation errors interrupt only its driver."""
    def __init__(self, source, output, on_failure, *, sampler=None):
        self.output = Path(output)
        self.output.mkdir()
        self.logs = {name: StreamCopy(Path(source)/name, self.output/name)
                     for name in ('events.jsonl', 'nvml-queries.jsonl')}
        self.samples = (self.output/'resources.jsonl').open('x', buffering=1)
        self.sample_bytes = self.count = 0
        self.sampler, self.on_failure = sampler, on_failure
        self.mutex = threading.Lock()
        self.stopping = threading.Event()
        self.error = None
        self.last_tick = time.monotonic()
        self.thread = threading.Thread(target=self.run, name='soak-evidence', daemon=True)
        self.thread.start()

    def attach(self, server, start_ticks):
        with self.mutex:
            self.sampler = lambda: observe.sample(server, start_ticks)

    def detach(self):
        with self.mutex:
            self.sampler = None

    def run(self):
        try:
            while not self.stopping.is_set():
                now = time.monotonic()
                require(now-self.last_tick <= 10, 'evidence sampling gap exceeds 10s')
                self.last_tick = now
                require(shutil.disk_usage(self.output).free >= 12*1024**3,
                        'evidence disk reserve below 12GiB')
                for log in self.logs.values():
                    log.poll()
                with self.mutex:
                    if self.sampler is not None:
                        row = self.sampler()
                        require(row['memory']['MemAvailable'] >= 20*1024**3,
                                'host available memory below 20GiB safety floor')
                        raw = json.dumps(row)+'\n'
                        self.sample_bytes += len(raw.encode())
                        require(self.sample_bytes <= 2*1024**3 and self.count < 90000,
                                'resource archive bound exceeded')
                        self.samples.write(raw)
                        self.count += 1
                self.stopping.wait(1)
        except BaseException as error:
            self.error = repr(error)
            self.on_failure()

    def check(self):
        require(self.error is None, 'evidence collector failed: '+str(self.error))
        require(time.monotonic()-self.last_tick <= 10, 'evidence collector stalled')

    def close(self):
        self.stopping.set()
        self.thread.join(5)
        require(not self.thread.is_alive(), 'evidence collector did not join')
        failures, copies = [], {}
        try:
            self.check()
        except BaseException as error:
            failures.append(repr(error))
        for name, log in self.logs.items():
            try:
                copies[name] = log.close()
            except BaseException as error:
                failures.append(repr(error))
        self.samples.close()
        pkg.save(self.output/'capture.json', dict(passed=not failures,
            errors=failures, logs=copies, resource_samples=self.count,
            resource_bytes=self.sample_bytes, sampling_interval_seconds=1,
            maximum_observation_gap_seconds=10))
        require(not failures, 'evidence collector incomplete')


def values(row):
    return dict(rss=row['parent']['VmRSS'], fds=row['parent']['fds'],
        threads=row['parent']['Threads'], anon=row['resources']['cgroup'][
            'observations']['memory.stat']['value']['anon'])


def envelope(rows):
    require(len(rows) > WARMUP, 'insufficient quiescent samples')
    require(all(set(row) == ENVELOPE.keys() and
                all(type(v) is int and v >= 0 for v in row.values()) for row in rows),
            'invalid quiescent resource sample')
    baseline = {key:max(r[key] for r in rows[:WARMUP]) for key in ENVELOPE}
    maximum = {key:max(r[key] for r in rows[WARMUP:]) for key in ENVELOPE}
    # Growth is a service acceptance failure, not missing environmental evidence.
    pkg.require(all(maximum[k] <= baseline[k]+ENVELOPE[k] for k in ENVELOPE),
                'quiescent resources exceeded frozen envelope')
    return dict(warmup=WARMUP, samples=len(rows), baseline=baseline,
                maximum=maximum, additive_envelope=ENVELOPE,
                physical_ram_peak_proved=False, indefinite_leak_freedom_proved=False)


def guard_archive(path, terminal):
    """A continuous reconstructed stream must include both lease boundaries."""
    require(terminal['status'] == 'COMPLETED', 'guard did not complete')
    first = last = previous = None
    observations = count = 0
    with Path(path).open('rb') as stream:
        while raw := stream.readline(8*1024**2+1):
            require(len(raw) <= 8*1024**2 and raw.endswith(b'\n'),
                    'guard line incomplete or oversized')
            row = json.loads(raw)
            require(type(row.get('monotonic')) in (int, float), 'guard time missing')
            if previous is not None:
                require(0 <= row['monotonic']-previous <= 10,
                        'guard observation time gap or regression')
            previous = row['monotonic']
            first = first or row
            last = row
            count += 1
            if row['event'] in ('preflight', 'monitor'):
                require(row['conflicts'] == [], 'guard conflict in archived stream')
                observations += 1
    require(first is not None and first['event'] == 'lease_acquired' and
            last['event'] == 'lease_released' and last['status'] == 'COMPLETED' and
            observations > 0, 'guard archive lacks complete lease boundaries')
    return dict(passed=True, records=count, observations=observations,
                seconds=last['monotonic']-first['monotonic'])
