"""Explicit managed-service NVML tolerance; never a performance monitor.

The shared flock and known process conflicts remain enforced. An unavailable
compute observation is UNKNOWN, not an empty or cached GPU process list.
"""
import json
import secrets
import time

from tools.serve_service import package as pkg
from tools.serve_service import monitor_control, nvml_policy
from tools.serve_service.service import guard


class Unavailable(RuntimeError):
    """Only a known NVML availability failure may take the process-only path."""


class Monitor:
    RETRY_SECONDS = (5, 10, 20, 40, 60)

    def __init__(self, output, *, query=None, clock=time.monotonic,
                 wall_clock=time.time, log_bytes=4*1024**2,
                 resume_path=None, stopping=lambda: False):
        self.output = output
        self.query = query or guard.nvml_process_query.query
        self.clock, self.wall_clock = clock, wall_clock
        self.log_bytes = log_bytes
        self.worker = None
        self.cleanup_unknown = False
        self.resume_path, self.stopping = resume_path, stopping
        self.resume_probe = False
        self.current_compute = set()
        self.state = dict(version=2, policy=monitor_control.PROFILE,
            generation=secrets.token_hex(16), closed=False, fatal=False,
            visibility='UNKNOWN', pids=None, last_success=None,
            failed_queries=0, consecutive_failures=0, recovered_gaps=0,
            gap_started_monotonic=None, completed_gap_seconds=0,
            next_query_monotonic=None, suspended=False,
            suspension_reason=None, last_query_status=None,
            last_query_completed_monotonic=None, last_query_completed_unix=None,
            query_category=None, last_failure=None, failure_counts={},
            suspension_epoch=0, resume_not_before_monotonic=None,
            resume_limit=monitor_control.RESUME_LIMIT,
            resume_cooldown_seconds=monitor_control.COOLDOWN_SECONDS,
            resume_attempts=0, last_resume_request=None, last_resume=None)

    @property
    def had_gaps(self):
        return self.state['failed_queries'] != 0

    def track_worker(self, worker):
        pkg.require(self.worker is None or self.worker.poll() is not None,
                    'previous NVML worker still alive')
        self.worker = worker
        self.cleanup_unknown = False

    def worker_reaped(self):
        return (not self.cleanup_unknown if self.worker is None
                else self.worker.poll() is not None)

    def snapshot(self):
        started = self.state['gap_started_monotonic']
        return dict(self.state, snapshot_unix=self.wall_clock(),
            snapshot_monotonic=self.clock(),
            current_gap_seconds=0 if started is None else self.clock()-started,
            query_worker_pid=None if self.worker is None else self.worker.pid,
            query_worker_reaped=self.worker_reaped(),
            performance_acceptance=False)

    def publish(self):
        value = self.snapshot()
        pkg.save(self.output/'compute-monitor.json', value)
        return value

    def suspend(self, reason):
        self.state.update(suspended=True, suspension_reason=reason,
            next_query_monotonic=None,
            suspension_epoch=self.state['suspension_epoch']+1,
            resume_not_before_monotonic=self.clock()+monitor_control.COOLDOWN_SECONDS)

    def poll_resume(self):
        if self.resume_path is None or not self.resume_path.exists():
            return
        try:
            pkg.require(self.resume_path.stat().st_size <= 4096,
                        'resume request size limit')
            value = pkg.read(self.resume_path)
        except (ValueError, RuntimeError):
            self.state['last_resume_request'] = dict(
                status='REJECTED', reason='INVALID_REQUEST')
            return
        if not monitor_control.valid_request(value):
            self.state['last_resume_request'] = dict(
                status='REJECTED', reason='INVALID_REQUEST')
            return
        previous = self.state['last_resume_request'] or {}
        if previous.get('request_id') == value['request_id']:
            return
        state = self.state
        reason = ('STALE_GENERATION' if value['generation'] != state['generation']
                  else 'STALE_SUSPENSION' if
                  value['suspension_epoch'] != state['suspension_epoch'] else
                  monitor_control.denial(self.snapshot(), self.clock(),
                                         stopping=self.stopping()))
        state['last_resume_request'] = dict(value, status=
            'REJECTED' if reason else 'ACCEPTED', reason=reason)
        if reason:
            return
        state['resume_attempts'] += 1
        state['last_resume'] = dict(request_id=value['request_id'],
            attempt=state['resume_attempts'], status='QUERY_PENDING')
        state.update(suspended=False, suspension_reason=None,
                     next_query_monotonic=None)
        self.resume_probe = True
        # Persist consumption before querying. A concurrent/stale CLI request
        # cannot reset the budget or execute a second probe for this epoch.
        self.publish()

    def failed(self, decision, query_started):
        state, now = self.state, self.clock()
        state['failed_queries'] += 1
        state['consecutive_failures'] += 1
        if state['gap_started_monotonic'] is None:
            state['gap_started_monotonic'] = query_started
        state.update(visibility='UNKNOWN', pids=None,
            query_category=decision.category,
            last_failure=dict(category=decision.category, action=decision.action,
                              nvml_code=decision.nvml_code, monotonic=now))
        counts = state['failure_counts']
        counts[decision.category] = counts.get(decision.category, 0)+1
        if self.resume_probe:
            state['last_resume'].update(status='FAILED', category=decision.category)
        if decision.action == 'fatal':
            state['fatal'] = True
            self.suspend(decision.category)
            self.resume_probe = False
            self.publish()
            raise RuntimeError('NVML worker protocol/fault: '+decision.category)
        if (decision.action == 'suspend' or self.resume_probe or
                state['consecutive_failures'] > len(self.RETRY_SECONDS)):
            self.suspend(decision.category if decision.action == 'suspend' else
                         'RESUME_FAILED' if self.resume_probe else 'RETRY_LIMIT')
        else:
            state['next_query_monotonic'] = now+self.RETRY_SECONDS[
                state['consecutive_failures']-1]
        self.resume_probe = False
        raise Unavailable('NVML process query: '+decision.category)

    def compute(self):
        state = self.state
        now = self.clock()
        pkg.require(not state['fatal'], 'NVML fatal fault remains latched')
        if state['closed'] or self.stopping():
            state.update(visibility='UNKNOWN', pids=None)
            if self.resume_probe:
                state['last_resume']['status'] = 'CANCELLED'
                self.resume_probe = False
            raise Unavailable('monitor closed or service stopping')
        if state['suspended'] or (state['next_query_monotonic'] is not None and
                                 now < state['next_query_monotonic']):
            raise Unavailable('NVML query suspended or in backoff')
        if not self.worker_reaped():
            self.failed(nvml_policy.Decision('WORKER_UNREAPED', 'suspend'), now)
        query_started = now
        state.update(visibility='UNKNOWN', pids=None, last_query_status='IN_PROGRESS')
        result = self.query(timeout=5, on_worker=self.track_worker)
        with guard.open_log(self.output/'nvml-queries.jsonl', 'a',
                            self.log_bytes) as stream:
            stream.write(json.dumps(result)+'\n')
        now = self.clock()
        state['last_query_status'] = result.get('status') if type(result) is dict else None
        state['last_query_completed_monotonic'] = now
        state['last_query_completed_unix'] = self.wall_clock()
        decision = nvml_policy.classify(result)
        if (type(result) is dict and result.get('status') == 'CLEANUP_FAILED' and
                self.worker is None):
            self.cleanup_unknown = True
        if decision.action != 'fatal' and not self.worker_reaped():
            decision = nvml_policy.Decision('WORKER_UNREAPED', 'suspend')
        state['query_category'] = decision.category
        if decision.category == 'OK':
            pids = result['pids']
            self.current_compute.update(pids)
            if state['gap_started_monotonic'] is not None:
                state['completed_gap_seconds'] += now-state['gap_started_monotonic']
                state['recovered_gaps'] += 1
            state.update(visibility='AVAILABLE', pids=pids,
                last_success=dict(monotonic=now, unix=self.wall_clock(), pids=pids),
                gap_started_monotonic=None, consecutive_failures=0,
                next_query_monotonic=None)
            if self.resume_probe:
                state['last_resume']['status'] = 'RECOVERED'
            self.resume_probe = False
            return set(pids)
        self.failed(decision, query_started)

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
        if owner_pgid is not None:
            self.poll_resume()
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
        self.state.update(visibility='UNKNOWN', pids=None, closed=True,
                          post_cleanup_gpu_visibility='NOT_QUERIED')
        return self.publish()
