"""Asynchronous NVML observation with bounded half-open recovery, no model action."""
import json
from pathlib import Path
import secrets
import sys
import time

from tools.serve_recovery import jobs, ledger
from tools.serve_service import nvml_policy

ROOT = Path(__file__).resolve().parents[2]


def decode(raw, request_id, returncode, reason=None, unreaped=False):
    errors, rows, malformed = [], [], False
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b'\n'):
            malformed = True
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            malformed = True
            continue
        if type(row) is not dict or row.get('request_id') != request_id:
            malformed = True
            continue
        rows.append(row)
        if row.get('event') == 'failure':
            errors.append(row.get('error'))
    result = dict(status='ERROR', error=dict(type='ProtocolError',
                  message='invalid NVML worker result', nvml_code=None))
    if unreaped:
        result = dict(status='CLEANUP_FAILED')
    elif reason == 'timeout':
        result = dict(status='TIMEOUT')
    elif reason is None:
        finals = [row for row in rows if row.get('event') == 'result']
        if len(finals) == 1 and rows and finals[0] is rows[-1]:
            final = finals[0]
            if final.get('status') == 'ERROR' and returncode == 1:
                result = dict(status='ERROR', error=final.get('error'),
                              shutdown_error=final.get('shutdown_error'))
            elif (returncode == 0 and final.get('status') == 'OK' and
                  type(final.get('device_count')) is int and
                  1 <= final['device_count'] <= 32 and
                  type(final.get('pids')) is list and len(final['pids']) <= 4096 and
                  all(ledger.integer(pid, minimum=2) and pid < 2**31
                      for pid in final['pids']) and final.get('error') is None and
                  final.get('shutdown_error') is None):
                result = dict(status='OK', pids=final['pids'])
    # Preserve explicit device errors even when later output/shutdown fails.
    result['observed_errors'] = sorted(errors, key=lambda error:
        nvml_policy.error_decision(error).action == 'fatal', reverse=True)[:2]
    if malformed or len(rows) > 256 or len(errors) > 2 or reason == 'output_limit':
        result['protocol_error'] = 'worker_contract'
    return result


class Monitor:
    RETRIES = (5, 10, 20, 40, 60)

    def __init__(self, lease_fd, *, factory=jobs.Job, clock=time.monotonic):
        self.lease_fd, self.factory, self.clock = lease_fd, factory, clock
        self.job, self.request_id = None, None
        self.next_at = 0
        self.failures = self.consecutive = self.successes = 0
        self.half_open = False
        self.fatal = False
        self.visibility = 'UNKNOWN'
        self.category = 'NOT_QUERIED'
        self.pids = None
        self.completed = None
        self.decision = None
        self.history = []

    def snapshot(self):
        return dict(visibility=self.visibility, category=self.category,
            failures=self.failures, successes=self.successes, fatal=self.fatal,
            half_open=self.half_open, worker_pid=None if self.job is None else
            self.job.process.pid, worker_unreaped=self.job is not None and self.job.unreaped,
            pids=self.pids, completed_monotonic=self.completed,
            next_query_monotonic=self.next_at)

    def can_start(self):
        return (not self.fatal and self.next_at is not None and
                self.clock() >= self.next_at and self.job is None)

    def collect(self, result):
        decision = nvml_policy.classify(result)
        self.decision, self.category, self.completed = decision, decision.category, self.clock()
        self.history = (self.history+[dict(category=decision.category,
                                         monotonic=self.completed)])[-16:]
        if decision.category == 'OK':
            self.visibility, self.pids = 'AVAILABLE', sorted(set(result['pids']))
            self.consecutive = 0
            self.successes += 1
            self.next_at = self.completed+5
            self.half_open = False
            return
        self.visibility, self.pids = 'UNKNOWN', None
        self.failures += 1
        self.consecutive += 1
        self.fatal = self.fatal or decision.action == 'fatal'
        if decision.action != 'retry':
            self.next_at = None
        elif self.consecutive <= len(self.RETRIES):
            self.next_at = self.completed+self.RETRIES[self.consecutive-1]
        else:
            self.next_at = self.completed+300
            self.half_open = True

    def tick(self, *, allow_query=True):
        if self.job is not None:
            complete = self.job.tick()
            if self.job.unreaped:
                if self.category != 'WORKER_UNREAPED' and not self.fatal:
                    self.collect(decode(bytes(self.job.output), self.request_id,
                        self.job.process.returncode, self.job.reason, unreaped=True))
                return
            if not complete:
                # A complete device-fault event is actionable even if shutdown
                # is still pending. Other partial observations are not facts.
                partial = decode(bytes(self.job.output), self.request_id, None)
                if not self.fatal and nvml_policy.classify(partial).action == 'fatal':
                    self.collect(partial)
                return
            result = decode(bytes(self.job.output), self.request_id,
                            self.job.process.returncode, self.job.reason)
            self.job.close()
            self.job = None
            if not self.fatal:
                self.collect(result)
        if not allow_query or not self.can_start():
            return
        self.visibility, self.pids = 'UNKNOWN', None
        self.request_id = secrets.token_hex(16)
        try:
            self.job = self.factory([sys.executable, '-B',
                str(ROOT/'tools/evalscope/nvml_process_query.py'),
                '--worker', self.request_id], lease_fd=self.lease_fd, timeout=5,
                clock=self.clock)
        except Exception as error:
            self.collect(dict(status='ERROR', error=dict(type=type(error).__name__,
                message='collector startup failed', nvml_code=getattr(error, 'value', None))))

    def cancel(self):
        if self.job is not None:
            self.job.kill('stopping')

    def refresh_after_cleanup(self):
        if self.visibility == 'AVAILABLE':
            # A successful observation taken before retirement cannot prove
            # that the old context disappeared. Do not reset failure backoff.
            self.visibility, self.pids = 'UNKNOWN', None
            self.next_at = self.clock()
