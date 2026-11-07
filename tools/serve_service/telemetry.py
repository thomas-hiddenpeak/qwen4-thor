"""Explicit managed-service NVML tolerance; never a performance monitor.

The shared flock and known process conflicts remain enforced. An unavailable
compute observation is UNKNOWN, not an empty or cached GPU process list.
"""
import json
import time

from tools.serve_service import package as pkg
from tools.serve_service.service import guard


class Unavailable(RuntimeError):
    """Only a known NVML availability failure may take the process-only path."""


class Monitor:
    RETRY_SECONDS = (5, 10, 20, 40, 60)

    def __init__(self, output, *, query=None, clock=time.monotonic,
                 wall_clock=time.time, log_bytes=4*1024**2):
        self.output = output
        self.query = query or guard.nvml_process_query.query
        self.clock, self.wall_clock = clock, wall_clock
        self.log_bytes = log_bytes
        self.worker = None
        self.current_compute = set()
        self.state = dict(version=1, policy='nvml-service-degraded-v1',
            visibility='UNKNOWN', pids=None, last_success=None,
            failed_queries=0, consecutive_failures=0, recovered_gaps=0,
            gap_started_monotonic=None, completed_gap_seconds=0,
            next_query_monotonic=None, suspended=False,
            suspension_reason=None, last_query_status=None,
            last_query_completed_monotonic=None, last_query_completed_unix=None)

    @property
    def had_gaps(self):
        return self.state['failed_queries'] != 0

    def track_worker(self, worker):
        pkg.require(self.worker is None or self.worker.poll() is not None,
                    'previous NVML worker still alive')
        self.worker = worker

    def snapshot(self):
        started = self.state['gap_started_monotonic']
        return dict(self.state, snapshot_unix=self.wall_clock(),
            snapshot_monotonic=self.clock(),
            current_gap_seconds=0 if started is None else self.clock()-started,
            query_worker_pid=None if self.worker is None else self.worker.pid,
            query_worker_reaped=self.worker is None or self.worker.poll() is not None,
            performance_acceptance=False)

    def publish(self):
        value = self.snapshot()
        pkg.save(self.output/'compute-monitor.json', value)
        return value

    def compute(self):
        state = self.state
        now = self.clock()
        if state['suspended'] or (state['next_query_monotonic'] is not None and
                                 now < state['next_query_monotonic']):
            raise Unavailable('NVML query suspended or in backoff')
        query_started = now
        state.update(visibility='UNKNOWN', pids=None, last_query_status='IN_PROGRESS')
        result = self.query(timeout=5, on_worker=self.track_worker)
        with guard.open_log(self.output/'nvml-queries.jsonl', 'a',
                            self.log_bytes) as stream:
            stream.write(json.dumps(result)+'\n')
        now = self.clock()
        state['last_query_status'] = result['status']
        state['last_query_completed_monotonic'] = now
        state['last_query_completed_unix'] = self.wall_clock()
        if result['status'] == 'OK':
            pids = result['pids']
            pkg.require(type(pids) is list and all(type(pid) is int and pid > 0
                        for pid in pids), 'invalid successful NVML observation')
            self.current_compute.update(pids)
            if state['gap_started_monotonic'] is not None:
                state['completed_gap_seconds'] += now-state['gap_started_monotonic']
                state['recovered_gaps'] += 1
            state.update(visibility='AVAILABLE', pids=pids,
                last_success=dict(monotonic=now, unix=self.wall_clock(), pids=pids),
                gap_started_monotonic=None, consecutive_failures=0,
                next_query_monotonic=None)
            return set(pids)
        # Protocol corruption and Python/logging errors are not vendor outages.
        pkg.require(result['status'] in ('ERROR', 'TIMEOUT', 'CLEANUP_FAILED'),
                    'invalid NVML worker protocol: '+str(result['status']))
        state['failed_queries'] += 1
        state['consecutive_failures'] += 1
        if state['gap_started_monotonic'] is None:
            state['gap_started_monotonic'] = query_started
        state.update(visibility='UNKNOWN', pids=None)
        count = state['consecutive_failures']
        if result['status'] == 'CLEANUP_FAILED' or count > len(self.RETRY_SECONDS):
            state.update(suspended=True, suspension_reason=result['status']
                if result['status'] == 'CLEANUP_FAILED' else 'RETRY_LIMIT',
                next_query_monotonic=None)
        else:
            state['next_query_monotonic'] = now+self.RETRY_SECONDS[count-1]
        raise Unavailable('NVML process query: '+result['status'])

    def process_only(self, owner_pgid, history):
        records = guard.processes()
        owned = guard.owned_processes(records, owner_pgid, history)
        conflicts, unconfirmed = [], []
        # Preserve any complete compute observation made earlier in THIS scan
        # (inspect can query twice to verify an exiting owned context). Never
        # reuse last_success across scans or turn an unconfirmed exit into idle.
        for pid in records.keys() | self.current_compute:
            if pid in owned:
                continue
            record = records.get(pid, {})
            if pid in self.current_compute and pid not in records and pid in history:
                unconfirmed.append(pid)
            elif pid in self.current_compute or guard.gpu_command(record):
                conflicts.append(dict(pid=pid, **record,
                    nvidia_compute_context=True if pid in self.current_compute else None))
        return dict(conflicts=conflicts, retired_owned_contexts=[],
            unconfirmed_owned_contexts=unconfirmed,
            unreadable_processes=sum(r.get('unreadable', False)
                                     for r in records.values()))

    def __call__(self, owner_pgid=None, owned_history=None):
        self.current_compute = set()
        history = owned_history if owned_history is not None else {}
        try:
            observation = guard.inspect(owner_pgid, history, compute_query=self.compute)
        except Unavailable:
            if owner_pgid is None:
                self.publish()
                raise RuntimeError('NVML unavailable before managed launch')
            observation = self.process_only(owner_pgid, history)
        observation['compute_monitor'] = self.publish()
        return observation

    def close(self):
        # Query already attempted kill + bounded wait on timeout. Retain its
        # Popen object to reap later without another query, wait or bare PID kill.
        # This is NOT a post-shutdown GPU-context observation.
        self.state.update(visibility='UNKNOWN', pids=None,
                          post_cleanup_gpu_visibility='NOT_QUERIED')
        return self.publish()
