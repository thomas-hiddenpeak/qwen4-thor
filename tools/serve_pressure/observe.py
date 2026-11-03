"""Continuous and quiescent observations; deliberately no physical RAM sum."""
import json
from pathlib import Path
import threading
import time

from tools.serve_pressure.pressure import memory
from tools.serve_backpressure.verify import memory_snapshot
from resource_metrics import collect_resources


def check_oom(events):
    keys = ('oom', 'oom_kill', 'oom_group_kill')
    if any(key not in events for key in keys):
        raise RuntimeError('required OOM counter missing')
    if any(events[key] for key in keys):
        raise RuntimeError('owned cgroup OOM observed')


def status_fields(raw):
    values = {}
    for line in raw.splitlines():
        key, _, value = line.partition(':')
        if key in ('VmRSS', 'VmHWM', 'Threads'):
            values[key] = int(value.split()[0]) * (1024 if key != 'Threads' else 1)
    return values


def sample(server, expected_start):
    started = time.monotonic()
    snapshot = memory_snapshot(server.pid)
    if snapshot['process_start_ticks'].get(str(server.pid)) != expected_start:
        raise RuntimeError('model PID identity changed')
    resources = collect_resources(server.pid, expected_cgroup=server.cgroup)
    if (resources['binding']['expected_matches_actual'] is not True or
            not resources['cgroup']['consistent_identity']):
        raise RuntimeError('model cgroup identity changed or unavailable')
    observations = resources['cgroup']['observations']
    for name in ('memory.current', 'memory.peak', 'memory.events', 'memory.stat'):
        if observations[name]['value'] is None:
            raise RuntimeError('required memory counter missing: ' + name)
    events = observations['memory.events']['value']
    check_oom(events)
    raw = snapshot['processes'][str(server.pid)]
    fields = status_fields(raw)
    if not {'VmRSS', 'Threads'} <= fields.keys():
        raise RuntimeError('live parent RSS/threads missing')
    return dict(started=started, ended=time.monotonic(), memory=memory(),
        process_memory=snapshot, resources=resources,
        parent=dict(**fields, fds=len(list(Path(f'/proc/{server.pid}/fd').iterdir()))),
        scope='separate overlapping observations, non-atomic; no physical RAM sum')


class Monitor:
    def __init__(self, server, expected_start, folder):
        self.server, self.expected_start = server, expected_start
        self.output = (folder / 'continuous.jsonl').open('x', buffering=1)
        self.stop = threading.Event()
        self.error = None
        self.count = 0
        self.thread = threading.Thread(target=self.run, name='resource-observer', daemon=True)
        self.thread.start()

    def run(self):
        try:
            while not self.stop.is_set():
                row = sample(self.server, self.expected_start)
                self.output.write(json.dumps(row) + '\n')
                self.count += 1
                self.stop.wait(1)
        except BaseException as error:
            self.error = repr(error)
            self.output.write(json.dumps(dict(observer_error=self.error)) + '\n')

    def check(self):
        if self.error:
            raise RuntimeError('ENV_ABORT: observer failed: ' + self.error)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('observer did not stop')
        self.output.close()
        self.check()


def plateau(rows):
    if len(rows) != 64:
        raise RuntimeError('incomplete fixed mixed cycles')
    def values(row):
        return dict(rss=row['parent']['VmRSS'], fds=row['parent']['fds'],
                    threads=row['parent']['Threads'],
                    anon=row['resources']['cgroup']['observations'][
                        'memory.stat']['value']['anon'])
    warmup = [values(row) for row in rows[:8]]
    limits = dict(rss=64*1024**2, anon=128*1024**2, fds=2, threads=2)
    baseline = {key: max(row[key] for row in warmup) for key in limits}
    maxima = {key: max(values(row)[key] for row in rows[8:]) for key in limits}
    if any(maxima[key] > baseline[key] + limits[key] for key in limits):
        raise RuntimeError('quiescent resource growth exceeds frozen envelope')
    return dict(warmup_cycles=8, measured_cycles=56, baseline=baseline,
                maxima=maxima, additive_envelope=limits,
                physical_ram_peak_proved=False, indefinite_leak_freedom_proved=False)
