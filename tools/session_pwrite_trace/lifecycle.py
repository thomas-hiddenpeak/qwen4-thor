"""Conservative streaming lifecycle audit. Ambiguity never becomes zero time."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import struct

RECORD = struct.Struct('<QQQII12Q')


class ContractError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise ContractError(reason)


def signed(value):
    return value if value < 2**63 else value-2**64


def union_ns(intervals, low=0, high=2**64-1):
    total, end = 0, low
    for start, stop in sorted(intervals):
        require(stop >= start, 'reversed interval')
        start, stop = max(low, start), min(high, stop)
        if stop > max(start, end):
            total += stop-max(start, end)
        end = max(end, stop)
    return total


class Files:
    def __init__(self):
        self.fds = {}

    def open(self, task, fd, generation):
        key = (task, fd)
        require(key not in self.fds, 'open reused live fd without close')
        self.fds[key] = generation

    def close(self, task, fd):
        self.fds.pop((task, fd), None)

    def dup(self, task, source, target):
        require((task, source) in self.fds, 'dup unknown file')
        self.fds[(task, target)] = self.fds[(task, source)]

    def get(self, task, fd):
        require((task, fd) in self.fds, 'pwrite file generation unknown')
        return self.fds[(task, fd)]

    def syscall(self, task, generation, nr, ret, args):
        if ret < 0:
            return
        if nr in (56, 437):
            self.open(task, ret, generation)
        elif nr == 57:
            self.close(task, args[0])
        elif nr in (23, 24) or (nr == 25 and args[1] in (0, 1030)):
            # Inherited descriptors may be duplicated before any observed
            # open. Keep them explicitly unknown unless a later pwrite uses it.
            if (task, args[0]) in self.fds:
                self.dup(task, args[0], ret)
            else:
                self.close(task, ret)
        elif nr == 436 and not args[2] & 4:  # CLOSE_RANGE_CLOEXEC
            for owner, fd in list(self.fds):
                if owner == task and args[0] <= fd <= args[1]:
                    self.close(task, fd)


class Request:
    def __init__(self, generation, owner, call, flags, size):
        self.generation, self.owner, self.call = generation, owner, call
        self.flags, self.size, self.remaining = flags, size, size
        self.issue_time = None
        self.insert_time = None
        self.intervals, self.queues = [], []
        self.device, self.completion = [], []
        self.setup_count = self.nvme_count = self.cq_count = 0
        self.cid = self.identity = None
        self.done = False
        self.attempts = 0
        self.aborted = 0
        self.last_cq = None

    def issue(self, ts, size):
        require(not self.done and self.issue_time is None, 'duplicate issue')
        require(size == self.remaining, 'issue remaining byte mismatch')
        self.issue_time = ts
        self.attempts += 1
        if self.insert_time is not None:
            require(ts >= self.insert_time, 'issue before insert')
            self.queues.append((self.insert_time, ts))
            self.insert_time = None

    def requeue(self, ts):
        require(self.issue_time is not None, 'requeue without issue')
        require(ts >= self.issue_time, 'requeue before issue')
        self.intervals.append((self.issue_time, ts))
        self.issue_time = None
        self.last_cq = None
        self.aborted += 1

    def complete(self, ts, size, error):
        require(not error, 'device completion error')
        require(self.issue_time is not None and not self.done,
                'completion without live issue')
        require((0 < size <= self.remaining) or
                (size == self.remaining == self.size == 0 and self.flags & 255 == 2),
                'invalid partial completion bytes')
        require(ts >= self.issue_time, 'completion before issue')
        self.remaining -= size
        if not self.remaining:
            self.intervals.append((self.issue_time, ts))
            self.done = True

    def merge(self):
        # block_rq_merge exposes the disappearing request, not its survivor.
        # Do not invent a survivor by matching a sector or recycled pointer.
        raise ContractError('merge survivor unknown; attribution refused')


def records(path):
    with Path(path).open('rb') as stream:
        while True:
            raw = stream.read(RECORD.size)
            if not raw:
                return
            require(len(raw) == RECORD.size, 'truncated record')
            ts, task, key, kind, cpu, *data = RECORD.unpack(raw)
            yield dict(ts=ts, task=task, key=key, kind=kind, cpu=cpu, d=data)


class Audit:
    def __init__(self):
        self.files = Files()
        self.calls, self.threads, self.requests = {}, {}, {}
        self.pointers = {}
        self.cids = {}
        self.setup_pending = {}
        self.counts = Counter()
        self.rows = []
        self.background = Counter()
        self.interrupted = Counter()
        self.completed = 0
        self.last_timestamp = 0
        self.inversions = 0

    def consume(self, e):
        ts, task, key, kind, cpu, d = (e[x] for x in
                                      ('ts', 'task', 'key', 'kind', 'cpu', 'd'))
        self.counts[kind] += 1
        self.inversions += ts < self.last_timestamp
        self.last_timestamp = ts
        if kind == 6:
            self.files.syscall(task >> 32, key, d[0], signed(d[1]), d[2:5])
        elif kind == 1:
            require(task not in self.threads and key not in self.calls,
                    'overlapping or duplicate pwrite')
            generation = self.files.get(task >> 32, d[0])
            self.threads[task] = key
            self.calls[key] = dict(start=ts, task=task, fd=d[0], offset=d[1],
                requested=d[2], file_generation=generation, written=0,
                requests=0, live=set(), io=[], queue=[], device=[],
                completion=[], fs=[], fs_pending=[], sleeps=[], wake=[],
                sleep_pending=None, wake_pending=None, sleep_state=0, insert_count=0, missing_wake=0,
                reads=0, stack_ids=[], events=Counter())
        elif kind == 2:
            require(key in self.calls, 'exit without pwrite')
            c = self.calls[key]
            require(not c['live'], 'pwrite exits with incomplete owned request')
            require(not c['fs_pending'] and c['sleep_pending'] is None,
                    'unpaired filesystem/scheduler event')
            ret = signed(d[0])
            require(ret == c['requested'] == c['written'],
                    'pwrite/owned block byte closure failure')
            require(ts >= c['start'], 'negative syscall interval')
            row = {x: c[x] for x in ('task', 'fd', 'offset', 'requested',
                'file_generation', 'written', 'requests', 'reads', 'insert_count')}
            row.update(call=key, start_ns=c['start'], end_ns=ts, wall_ns=ts-c['start'],
                       stack_ids=c['stack_ids'], fs_event_counts=dict(c['events']))
            for label in ('io', 'queue', 'device', 'completion', 'fs', 'sleeps', 'wake'):
                row[label+'_union_ns'] = union_ns(c[label], c['start'], ts)
            # Intersections use inclusion/exclusion, not sum of concurrent I/O.
            row['fs_io_overlap_ns'] = (row['fs_union_ns']+row['io_union_ns']-
                union_ns(c['fs']+c['io'], c['start'], ts))
            row['outside_fs_or_io_ns'] = row['wall_ns']-union_ns(
                c['fs']+c['io'], c['start'], ts)
            row['missing_wake_intervals'] = c['missing_wake']
            row['wake_known_lower_bound_ns'] = row['wake_union_ns']
            if c['missing_wake']: row['wake_union_ns'] = None
            self.rows.append(row)
            del self.calls[key]
            del self.threads[task]
        elif kind in (3, 4, 5):
            require(key in self.calls, 'scheduler event without call')
            c = self.calls[key]
            if kind == 3:
                require(c['sleep_pending'] is None, 'duplicate switch out')
                c['sleep_pending'] = ts
                c['sleep_state'] = d[1]
                c['stack_ids'].append(dict(id=signed(d[2]), state=d[1]))
            elif kind == 5:
                require(c['sleep_pending'] is not None, 'wake without sleep')
                if c['wake_pending'] is None:
                    c['wake_pending'] = ts
            else:
                require(c['sleep_pending'] is not None, 'switch in without out')
                if c['sleep_state'] and c['wake_pending'] is None:
                    c['missing_wake'] += 1
                c['sleeps'].append((c['sleep_pending'], ts))
                if c['wake_pending'] is not None:
                    c['wake'].append((c['wake_pending'], ts))
                c['sleep_pending'] = c['wake_pending'] = None
        elif kind in (10, 11, 12, 13, 14, 20, 21, 22):
            rk = (key, d[0])
            if rk not in self.requests:
                require(key not in self.pointers, 'request pointer reused while live')
                r = Request(d[0], d[1], d[2], d[5], d[3])
                self.requests[rk], self.pointers[key] = r, rk
                if r.owner:
                    require(r.call in self.calls, 'owned request without call')
                    self.calls[r.call]['live'].add(rk)
            r = self.requests[rk]
            require((r.owner, r.call) == (d[1], d[2]), 'request ownership changed')
            c = self.calls.get(r.call) if r.owner else None
            if kind == 10:
                require(r.insert_time is None, 'duplicate insert')
                r.insert_time = ts
                if c: c['insert_count'] += 1
            elif kind == 11:
                r.issue(ts, d[3])
            elif kind == 13:
                r.requeue(ts)
                if r.identity is not None:
                    self.cids.pop(r.identity, None)
            elif kind == 14:
                if c: r.merge()
                self.background['merged_unknown'] += 1
                del self.requests[rk], self.pointers[key]
            elif kind == 20:
                r.setup_count += 1
                r.cid = (d[7] >> 16) & 65535
                context = (task, cpu)
                require(context not in self.setup_pending, 'ambiguous setup pair')
                self.setup_pending[context] = rk
            elif kind == 22:
                require(r.issue_time is not None, 'CQ before issue')
                r.cq_count += 1
                r.last_cq = ts
                r.device.append((r.issue_time, ts))
            elif kind == 21:
                require(r.last_cq is not None, 'NVMe complete without CQ')
                r.nvme_count += 1
                if r.identity is not None:
                    self.cids.pop(r.identity, None)
                r.completion.append((r.last_cq, ts))
                self.interrupted[d[11]] += 1
            else:
                r.complete(ts, d[7], d[8])
                if r.done:
                    if c:
                        require(r.identity is not None, 'owned setup identity absent')
                        require(r.nvme_count == r.cq_count and
                                r.setup_count == r.nvme_count+r.aborted,
                                'owned NVMe lifecycle incomplete')
                        c['requests'] += 1
                        if r.flags & 255 == 1: c['written'] += r.size
                        elif r.flags & 255 == 0: c['reads'] += 1
                        else: raise ContractError('owned non-read/write request')
                        for name, values in [('io', r.intervals), ('queue', r.queues),
                                             ('device', r.device),
                                             ('completion', r.completion)]:
                            c[name].extend(values)
                        c['live'].remove(rk)
                    else:
                        self.background[str(r.flags & 255)] += r.size
                    self.completed += 1
                    del self.requests[rk], self.pointers[key]
        elif kind == 15:
            self.background['unobserved_start_events'] += 1
        elif kind == 30:
            rk = self.setup_pending.pop((task, cpu), None)
            require(rk in self.requests, 'formatted setup without raw setup')
            r = self.requests[rk]
            payload = struct.pack('<12Q', *d)
            ctrl, queue = struct.unpack_from('<ii', payload, 32)
            cid = struct.unpack_from('<H', payload, 44)[0]
            require(cid == r.cid, 'setup CID mismatch')
            r.identity = (ctrl, queue, cid)
            require(r.identity not in self.cids, 'CID reused while command live')
            self.cids[r.identity] = rk
        elif 33 <= kind <= 41 and task in self.threads:
            c = self.calls[self.threads[task]]
            c['events'][kind] += 1
            if kind == 33: c['fs_pending'].append(ts)
            if kind == 34:
                require(c['fs_pending'], 'map exit without enter')
                c['fs'].append((c['fs_pending'].pop(), ts))

    def finish(self):
        require(not self.calls and not self.threads, 'incomplete pwrite')
        require(not any(self.requests.get(k) and self.requests[k].owner
                        for k in self.setup_pending.values()),
                'incomplete owned setup correlation')
        require(not any(r.owner for r in self.requests.values()),
                'incomplete owned request')
        return dict(passed=True, calls=self.rows, event_counts=dict(self.counts),
                    completed_requests=self.completed,
                    incomplete_background_requests=len(self.requests),
                    background_unknown_setups=len(self.setup_pending),
                    background_bytes=dict(self.background),
                    interrupted_normal_types=dict(self.interrupted),
                    timestamp_inversions=self.inversions)


def audit(directory):
    directory = Path(directory)
    reader = Audit()
    for e in records(directory/'events.bin'):
        reader.consume(e)
    result = reader.finish()
    exit_row = json.loads((directory/'exit.json').read_text())
    require(exit_row['status'] == 'COLLECTED_REQUIRES_AUDIT', 'collector failed')
    require(exit_row['counters'][0] == 0 and not any(exit_row['counters'][2:6]),
            'active/lost/map/stack/read counter')
    require(sum(reader.counts.values()) == exit_row['events'] ==
            exit_row['counters'][1], 'flush count mismatch')
    programs = json.loads((directory/'programs.json').read_text())
    misses = {p['name']: p['recursion_misses'] for p in programs}
    require(not any(n for k, n in misses.items() if k not in ('normal_31', 'normal_32', 'woke')),
            'essential probe recursion miss')
    result['recursion_misses'] = misses
    result['owned_sleep_wakeup_closed'] = not any(c['missing_wake_intervals'] for c in result['calls'])
    result['global_wakeup_misses_are_not_assigned_to_owned_calls'] = misses['woke']
    result['secondary_gaps'] = {
        str(k): reader.counts[k]-reader.counts[k+10] for k in (21, 22)}
    for kind, name in [(21, 'normal_31'), (22, 'normal_32')]:
        gap = result['secondary_gaps'][str(kind)]
        require(0 <= gap <= misses[name], 'unexplained secondary count gap')
    result['scope'] = 'OWNED_MAIN_CHAIN_VALID_SECONDARY_RECURSION_GAPS_REPORTED'
    return result


if __name__ == '__main__':
    import sys
    print(json.dumps(audit(sys.argv[1]), indent=2))
