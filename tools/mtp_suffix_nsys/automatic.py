"""Automatic CUDA-API stop with an explicit report-ready callback.

The unchanged manual Session remains in interactive.py. This subclass never
issues nsys stop. The caller must keep its independently pinned target alive
through await_report() and successful kill=none shutdown, then own target exit.
"""
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import shlex
import stat
import sys
import time


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manual = _load('nsys_automatic_manual', 'interactive.py')
callback = _load('nsys_automatic_callback', 'report_ready.py')
START_OPTIONS = tuple('--capture-range-end=stop' if value == '--capture-range-end=none'
                      else value for value in manual.START_OPTIONS)
LAUNCH_OPTIONS = manual.LAUNCH_OPTIONS
make_name = manual.make_name


def contract():
    result = manual.contract()
    result.update(schema=2, mode='interactive_api_auto_stop',
        start_options=list(START_OPTIONS),
        callback_option='--after-report-ready=<shlex.join absolute Python helper argv>',
        callback_environment=list(callback.ENVIRONMENT_KEYS),
        callback_name_literal='undocumented; require nonempty and preserve actual value',
        callback_nonce_hex_characters=32, callback_max_bytes=callback.MAX_RECEIPT_BYTES,
        callback_receipt='report-ready-callback.json',
        callback_binding='exact session, nonce, report path and unchanged regular-file stat',
        callback_security_scope='No authentication against a malicious same-UID process',
        stop_options=[], stop_allowed=False, stop_requires_nonempty_report=False,
        report_wait_timeout_seconds=45, report_wait_poll_seconds=.05,
        report_ready_is_trace_completeness=False,
        success_order=['capture_end', 'report_ready', 'shutdown_kill_none',
                       'independent_target_exit', 'reap', 'close'],
        failure_shutdown_cancels=True, manual_stop_fallback=False)
    return result


class Session(manual.Session):
    def __init__(self, nsys, output, name, *, cwd, env, deadline, check=None):
        super().__init__(nsys, output, name, cwd=cwd, env=env, deadline=deadline, check=check)
        callback.absolute_path(self.output)
        self.report_ready_succeeded = False
        self._report_wait_attempted = False
        self._callback_nonce = secrets.token_hex(16)
        self._callback_path = self.output / 'report-ready-callback.json'
        self._expected_report = self.output / 'trace.nsys-rep'
        self._not_before_ns = None
        if self._callback_path.exists() or self._callback_path.is_symlink():
            raise ValueError('callback receipt must not preexist this session')

    def start(self, timeout=30):
        if self._closed or self._subreaper_attempted:
            raise RuntimeError('session start may only be attempted once')
        self._prepare_owner()
        helper = Path(__file__).with_name('report_ready.py').resolve()
        command_text = shlex.join([str(Path(sys.executable).resolve()), '-B', str(helper),
            '--session', self.name, '--nonce', self._callback_nonce,
            '--output', str(self._callback_path), '--report', str(self._expected_report)])
        self._not_before_ns = time.monotonic_ns()
        command = [str(self.nsys), 'start', *START_OPTIONS,
            '--after-report-ready=' + command_text,
            '--session-new=' + self.name, '--output=' + str(self.output / 'trace')]
        result = self._control('start', command, timeout)
        self.start_succeeded = True
        return result

    def stop(self, timeout=45, check=None):
        raise RuntimeError('manual stop is forbidden for automatic API-stop sessions')

    def _read_callback(self):
        fd = os.open(self._callback_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or
                    info.st_size > callback.MAX_RECEIPT_BYTES or info.st_uid != os.geteuid()):
                raise ValueError('callback receipt must be a bounded owned regular file')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(callback.MAX_RECEIPT_BYTES + 1)
            if len(raw) != info.st_size:
                raise ValueError('callback receipt changed during read')
            return json.loads(raw)
        finally:
            os.close(fd)

    def _validate_callback(self, value):
        if (not isinstance(value, dict) or value.get('schema') != 1 or
                value.get('kind') != 'nsys_after_report_ready' or
                value.get('session') != self.name or value.get('nonce') != self._callback_nonce or
                value.get('expected_report') != str(self._expected_report)):
            raise ValueError('callback run/session binding mismatch')
        environment = value.get('environment', {})
        if (not isinstance(environment, dict) or
                set(environment) != set(callback.ENVIRONMENT_KEYS) or
                environment.get('NSYS_SESSION_NAME') != self.name or
                environment.get('NSYS_REPORT_PATH') != str(self._expected_report) or
                not isinstance(environment.get('NSYS_CALLBACK_NAME'), str) or
                not environment['NSYS_CALLBACK_NAME'].strip()):
            raise ValueError('callback environment binding mismatch')
        stamp = value.get('observed_monotonic_ns')
        if (type(stamp) is not int or not self._not_before_ns <= stamp <= time.monotonic_ns() or
                type(value.get('callback_pid')) is not int or value['callback_pid'] <= 0):
            raise ValueError('callback identity/time receipt is invalid')
        if value.get('failure') is not None:
            raise RuntimeError('report callback failed: ' + str(value['failure']))
        current = callback.report_stat(self._expected_report)
        if current != value.get('report'):
            raise ValueError('report identity or size changed after callback')
        return current

    def await_report(self, timeout=45, check=None):
        if (self._closed or not self.start_succeeded or self.launch_process is None or
                self.shutdown_attempted or self._report_wait_attempted):
            raise RuntimeError('report wait requires one launched automatic session')
        if not math.isfinite(timeout) or not 0 < timeout <= 45:
            raise ValueError('report wait exceeds fixed bound')
        self._report_wait_attempted = True
        started = time.monotonic()
        end = min(self.deadline, started + timeout)
        result = dict(schema=1, mode='interactive_api_auto_stop', session=self.name,
            owner=self.owner, started_monotonic=started, deadline_monotonic=end,
            callback=None, report=None, report_ready_succeeded=False, failure=None,
            scope='Callback/report binding only; target exit and trace integrity are independent')

        def verify():
            self._owner_current()
            if self.check:
                self.check()
            if check:
                check()
            if time.monotonic() >= end:
                raise TimeoutError('automatic report-ready deadline exceeded')

        try:
            while True:
                verify()
                try:
                    result['callback'] = self._read_callback()
                except FileNotFoundError:
                    time.sleep(min(.05, max(0, end - time.monotonic())))
                    continue
                result['report'] = self._validate_callback(result['callback'])
                verify()
                self.report_ready_succeeded = result['report_ready_succeeded'] = True
                break
        except BaseException as error:
            result['failure'] = repr(error)
            raise
        finally:
            result['finished_monotonic'] = time.monotonic()
            manual._save(self.output / 'automatic-report-ready.json', result)
        return result

    def close(self):
        try:
            return super().close()
        finally:
            manual._save(self.output / 'automatic-close.json', dict(
                schema=1, session=self.name, report_ready_succeeded=self.report_ready_succeeded,
                stop_succeeded=self.stop_succeeded, shutdown_succeeded=self.shutdown_succeeded,
                scope='Closing or shutdown cannot make report collection successful'))
