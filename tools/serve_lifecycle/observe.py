"""One-hour resource envelope and identity-bound child exit observations."""
import json
from pathlib import Path
import time

from tools.serve_lifecycle import stage
from tools.serve_pressure import observe as prior

ObservationAbort = prior.ObservationAbort


def sample(server, start):
    try:
        value = prior.sample(server, start)
        if value['process_memory']['sampling_errors']:
            raise RuntimeError('process observation errors')
        return value
    except Exception as error:
        raise ObservationAbort('resource evidence unavailable: '+str(error)) from error


class Monitor(prior.Monitor):
    def run(self):
        try:
            while not self.stop.is_set():
                value = sample(self.server, self.expected_start)
                self.output.write(json.dumps(value)+'\n')
                self.count += 1
                self.stop.wait(1)
        except BaseException as error:
            self.error = repr(error)
            self.output.write(json.dumps(dict(observer_error=self.error))+'\n')


def plateau(rows):
    stage.h.require(len(rows) == stage.CYCLES, 'incomplete fixed cycles')
    values = [dict(rss=r['parent']['VmRSS'], fds=r['parent']['fds'], threads=r['parent']['Threads'],
        anon=r['resources']['cgroup']['observations']['memory.stat']['value']['anon']) for r in rows]
    stage.h.require(all(type(v) is int and v >= 0 for r in values for v in r.values()),
                    'missing or invalid resource observations')
    baseline = {k:max(r[k] for r in values[:stage.WARMUP]) for k in stage.ENVELOPE}
    maxima = {k:max(r[k] for r in values[stage.WARMUP:]) for k in stage.ENVELOPE}
    stage.h.require(all(maxima[k] <= baseline[k]+stage.ENVELOPE[k] for k in baseline),
                    'quiescent resource growth exceeds frozen envelope')
    return dict(warmup_cycles=stage.WARMUP, measured_cycles=stage.CYCLES-stage.WARMUP,
        baseline=baseline, maxima=maxima, additive_envelope=stage.ENVELOPE,
        physical_ram_peak_proved=False, indefinite_leak_freedom_proved=False)


def child_states(children, *, proc=Path('/proc')):
    values = []
    for pid, start in children.items():
        try:
            raw = (proc/str(pid)/'stat').read_text()
        except (FileNotFoundError, ProcessLookupError):
            values.append(dict(pid=int(pid), start_ticks=start, state='absent', running=False))
            continue
        fields = raw.rsplit(')', 1)[1].split()
        actual = int(fields[19])
        if actual != start:
            # The old identity is gone; never signal the recycled PID.
            values.append(dict(pid=int(pid), start_ticks=start, state='pid_reused',
                               running=False, new_start_ticks=actual))
        else:
            values.append(dict(pid=int(pid), start_ticks=start, process_state=fields[0],
                state='zombie' if fields[0] == 'Z' else 'present', running=fields[0] != 'Z'))
    return values


def wait_children(children, folder, *, timeout=10):
    started, values = time.monotonic(), []
    try:
        while True:
            try:rows = child_states(children)
            except (OSError, ValueError, IndexError) as error:
                raise ObservationAbort('child exit observation failed: '+repr(error)) from error
            values.append(dict(monotonic=time.monotonic(), children=rows))
            if all(not row['running'] for row in rows):return rows
            stage.h.require(time.monotonic()-started < timeout, 'original worker remains live')
            time.sleep(.05)
    finally:
        stage.h.save(folder/'child-exit-observations.json', dict(samples=values,
            observed_before_scope_cleanup=True, controller_signalled_workers=False,
            zombie_means_address_space_exited_not_parent_reaped=True))
