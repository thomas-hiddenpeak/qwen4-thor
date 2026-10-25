"""One documented repeat:1:sync collection; no callback or manual-stop fallback.

The application-thread post-cudaProfilerStop receipt is the completion barrier.
Generated stdout only binds a report name. Neither proves trace completeness.
"""
import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import time

SPEC = importlib.util.spec_from_file_location(
    'nsys_synchronous_manual', Path(__file__).with_name('interactive.py'))
manual = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manual)
START_OPTIONS = tuple('--capture-range-end=repeat:1:sync'
    if value == '--capture-range-end=none' else value for value in manual.START_OPTIONS)
LAUNCH_OPTIONS = manual.LAUNCH_OPTIONS
make_name = manual.make_name
MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_RECEIPT_BYTES = 65536


def contract():
    result = manual.contract()
    result.update(schema=3, mode='interactive_api_sync_stop',
        start_options=list(START_OPTIONS), stop_options=[], stop_allowed=False,
        stop_requires_nonempty_report=False, callback=False, capture_ranges=1,
        report_wait_timeout_seconds=45, report_wait_poll_seconds=.05,
        launch_log_max_bytes=MAX_LOG_BYTES, binding_receipt_max_bytes=MAX_RECEIPT_BYTES,
        launch_log='Exact owned regular stdout file passed to launch, not a guessed filename',
        completion='Documented sync plus same owned live target post-cudaProfilerStop success receipt',
        target_receipt_sources=['fixture-ready', 'model-capture-end'],
        report_binding='Unique Generated absolute path and exactly one owned nonempty regular report',
        binding_receipt='synchronous-report-ready.json', report_ready_is_trace_completeness=False,
        success_order=['target_api_stop_return', 'report_binding', 'shutdown_kill_none',
                       'independent_target_exit', 'reap', 'close'],
        failure_shutdown_cancels=True, manual_stop_fallback=False)
    return result


def _path(value):
    path = manual._absolute(value, 'owned path')
    if os.path.normpath(str(path)) != str(path):
        raise ValueError('owned path must be normalized')
    return path


def _regular(path, *, maximum=None, read=False, allow_growth=False):
    before = Path(path).lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('expected a regular file before opening')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        current = Path(path).lstat()
        if (not stat.S_ISREG(info.st_mode) or
                (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino) or
                (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino) or
                maximum is not None and info.st_size > maximum):
            raise ValueError('expected a bounded stable regular file')
        row = dict(path=str(path), device=info.st_dev, inode=info.st_ino,
                   bytes=info.st_size, mtime_ns=info.st_mtime_ns)
        data = None
        if read:
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                data = stream.read(info.st_size)
            if len(data) != info.st_size:
                raise ValueError('file shrank during read')
        after = Path(path).lstat()
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('file identity changed after reading')
        stable = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if not allow_growth and not stable(before) == stable(info) == stable(after):
            raise ValueError('immutable file changed while inspecting it')
        return (row, data) if read else row
    finally:
        os.close(fd)


def _target_receipt(value):
    if (not isinstance(value, dict) or
            any(type(value.get(k)) is not int or value[k] <= 0 for k in ('pid', 'start_ticks')) or
            type(value.get('profiler_stop_error')) is not int or value['profiler_stop_error'] != 0 or
            value.get('source') not in ('fixture-ready', 'model-capture-end') or
            not isinstance(value.get('raw'), dict)):
        raise ValueError('invalid normalized post-API-stop target receipt')
    raw = value['raw']
    if value['source'] == 'fixture-ready':
        if (type(raw.get('pid')) is not int or raw['pid'] != value['pid'] or
                raw.get('passed') is not True or raw.get('completed_steps') != 32 or
                any(type(raw.get(k)) is not int or raw[k] != 0 for k in
                    ('profiler_start_error', 'profiler_stop_error', 'final_sync_error'))):
            raise ValueError('fixture-ready raw receipt disagrees with API-stop success')
    elif (raw.get('event') != 'end' or raw.get('pid') != str(value['pid']) or
          raw.get('error') != '0' or type(value.get('expected_input_tokens')) is not int or
          value['expected_input_tokens'] <= 0 or
          raw.get('input_tokens') != str(value['expected_input_tokens'])):
        raise ValueError('model capture-end raw receipt disagrees with API-stop success')
    return value


def _generated(log, output, expected_log):
    identity, data = _regular(log, maximum=MAX_LOG_BYTES, read=True, allow_growth=True)
    if (identity['device'], identity['inode']) != (expected_log['device'], expected_log['inode']):
        raise ValueError('launch log identity changed')
    # An unterminated last line may still be in flight; never accept its path.
    lines = [line.rstrip('\r\n') for line in data.decode('utf-8').splitlines(keepends=True)
             if line.endswith('\n')]
    markers = [i for i, line in enumerate(lines) if line == 'Generated:']
    if not markers:
        return None
    if len(markers) != 1:
        raise ValueError('expected exactly one Generated block')
    paths = []
    for line in lines[markers[0] + 1:]:
        if not line.strip():
            if paths:
                break
            continue
        if not line[0].isspace():
            if not paths:
                raise ValueError('Generated block lacks an indented report path')
            break
        paths.append(line.strip())
    if not paths:
        return None
    if len(paths) != 1:
        raise ValueError('Generated block has multiple report paths')
    report = _path(paths[0])
    if report.parent != output or report.suffix != '.nsys-rep':
        raise ValueError('Generated report is outside the exact owned output directory')
    return report


def _report(output, path):
    if output != output.resolve() or path.parent != output or path.suffix != '.nsys-rep':
        raise ValueError('report path is not inside the canonical owned directory')
    reports = [p for p in output.iterdir() if p.name.endswith('.nsys-rep')]
    if reports != [path]:
        raise ValueError('owned directory must contain exactly the bound report')
    result = _regular(path)
    if result['bytes'] <= 0:
        raise ValueError('report must be nonempty')
    return result


def bound_report(output):
    """Revalidate an existing binding without requiring a live target/session."""
    output = _path(output)
    _, data = _regular(output / 'synchronous-report-ready.json',
                       maximum=MAX_RECEIPT_BYTES, read=True)
    value = json.loads(data)
    if (not isinstance(value, dict) or value.get('schema') != 1 or
            value.get('mode') != 'interactive_api_sync_stop' or
            value.get('report_ready_succeeded') is not True or value.get('failure') is not None):
        raise ValueError('synchronous report binding did not succeed')
    _target_receipt(value.get('target_receipt'))
    log_record = value['launch_log']
    log = _path(log_record['path'])
    if log.parent != output:
        raise ValueError('binding references a foreign launch log')
    path = _path(value['report']['path'])
    if _generated(log, output, log_record) != path or _report(output, path) != value['report']:
        raise ValueError('report or Generated binding changed after acceptance')
    return path


class Session(manual.Session):
    def __init__(self, nsys, output, name, *, cwd, env, deadline, check=None):
        super().__init__(nsys, output, name, cwd=cwd, env=env, deadline=deadline, check=check)
        if self.output != self.output.resolve():
            raise ValueError('output must be the canonical owned directory')
        if any(p.name.endswith('.nsys-rep') for p in self.output.iterdir()):
            raise ValueError('report must not preexist this collection')
        binding = self.output / 'synchronous-report-ready.json'
        if binding.exists() or binding.is_symlink():
            raise ValueError('report binding must not preexist this session')
        self.report_path = None
        self.report_ready_succeeded = False
        self._report_wait_attempted = False
        self._launch_log = None

    def start(self, timeout=30):
        if self._closed or self._subreaper_attempted:
            raise RuntimeError('session start may only be attempted once')
        self._prepare_owner()
        result = self._control('start', [str(self.nsys), 'start', *START_OPTIONS,
            '--session-new=' + self.name, '--output=' + str(self.output / 'trace')], timeout)
        self.start_succeeded = True
        return result

    def launch(self, target, stdout, envvars=None):
        log = _path(stdout.name)
        info = os.fstat(stdout.fileno())
        record = _regular(log, maximum=MAX_LOG_BYTES)
        if (log.parent != self.output or not stat.S_ISREG(info.st_mode) or
                (record['device'], record['inode']) != (info.st_dev, info.st_ino)):
            raise ValueError('launch stdout must be the exact owned regular file')
        child = super().launch(target, stdout, envvars)
        self._launch_log = record
        return child

    def stop(self, timeout=45, check=None):
        raise RuntimeError('manual stop is forbidden for synchronous API-stop sessions')

    def await_report(self, target_receipt, timeout=45, check=None):
        if (self._closed or not self.start_succeeded or self.launch_process is None or
                self.shutdown_attempted or self._report_wait_attempted or self._launch_log is None):
            raise RuntimeError('report wait requires one launched synchronous session')
        if not math.isfinite(timeout) or not 0 < timeout <= 45:
            raise ValueError('report wait exceeds fixed bound')
        self._report_wait_attempted = True
        started = time.monotonic()
        end = min(self.deadline, started + timeout)
        result = dict(schema=1, mode='interactive_api_sync_stop', session=self.name,
            owner=self.owner, started_monotonic=started, deadline_monotonic=end,
            target_receipt=target_receipt, launch_log=self._launch_log, report=None,
            report_ready_succeeded=False, failure=None,
            scope='Sync application-thread return proves generation; Generated binds filename; trace coverage remains independent')

        def verify():
            self._owner_current()
            known = self._known_owned.get(target_receipt['pid'])
            current = manual.proc_identity(target_receipt['pid'])
            if (known is None or known['start_ticks'] != target_receipt['start_ticks'] or
                    current['start_ticks'] != target_receipt['start_ticks'] or
                    current['state'] in ('Z', 'X')):
                raise RuntimeError('API-stop target is not the same previously bound live process')
            if self.check:
                self.check()
            if check:
                check()
            if time.monotonic() >= end:
                raise TimeoutError('synchronous report binding deadline exceeded')

        try:
            _target_receipt(target_receipt)
            while True:
                verify()
                path = _generated(Path(self._launch_log['path']), self.output, self._launch_log)
                if path is None:
                    time.sleep(min(.05, max(0, end - time.monotonic())))
                    continue
                result['report'] = _report(self.output, path)
                verify()
                self.report_path = path
                self.report_ready_succeeded = result['report_ready_succeeded'] = True
                break
        except BaseException as error:
            result['failure'] = repr(error)
            raise
        finally:
            result['finished_monotonic'] = time.monotonic()
            manual._save(self.output / 'synchronous-report-ready.json', result)
        return result

    def close(self):
        try:
            return super().close()
        finally:
            manual._save(self.output / 'synchronous-close.json', dict(schema=1,
                session=self.name, report_ready_succeeded=self.report_ready_succeeded,
                stop_succeeded=self.stop_succeeded, shutdown_succeeded=self.shutdown_succeeded,
                scope='Cleanup does not repair a failed synchronous report binding'))
