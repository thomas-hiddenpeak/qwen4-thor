"""Bounded, owned Nsight 2026.1 interactive control; no work at import time.

The caller owns target liveness, PID/executable/receipt checks, report export,
diagnostic acceptance and the outer GPU lease. Control success is never target
exit or trace completeness. No implicit retries, session discovery, or signals
to numerically discovered targets are performed here.
"""
import ctypes
import errno
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time

START_OPTIONS = (
    '--capture-range=cudaProfilerApi', '--capture-range-end=none',
    '--flush-on-cudaprofilerstop=true', '--sample=none', '--cpuctxsw=none',
    '--stop-on-exit=true')
LAUNCH_OPTIONS = (
    '--trace=cuda,nvtx', '--wait=all', '--show-output=true',
    '--inherit-environment=true')
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


def contract():
    return dict(schema=1, mode='interactive_start_launch_stop',
        start_options=list(START_OPTIONS), launch_options=list(LAUNCH_OPTIONS),
        stop_options=['--keep=0'], shutdown_options=['--kill=none'],
        start_timeout_seconds=30, stop_timeout_seconds=45,
        shutdown_timeout_seconds=15, control_cleanup_seconds=5,
        control_kill_wait_seconds=5, poll_seconds=.05, retries=0,
        adopted_reap_timeout_seconds=5,
        owner='step_driver_pid_start_ticks_subreaper', ancestry_limit=64,
        session_name='q4t-suffix-PID-START_TICKS-RANDOM_HEX16-LABEL',
        stop_requires_nonempty_report=True,
        target_exit_is_independent=True, shutdown_is_not_collection_success=True,
        actual_target_warnings_block=True)


def make_name(label):
    """Mint one exact namespace for a single attempt, never search sessions."""
    if not isinstance(label, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', label):
        raise ValueError('invalid fixed session label')
    owner = proc_identity(os.getpid())
    return (f'q4t-suffix-{owner["pid"]}-{owner["start_ticks"]}-'
            f'{secrets.token_hex(8)}-{label}')


def proc_identity(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    head, tail = raw.rsplit(')', 1)
    fields = tail.split()
    actual_pid = int(head.split(' ', 1)[0])
    if actual_pid != pid:
        raise RuntimeError('proc stat PID mismatch')
    return dict(pid=pid, start_ticks=int(fields[19]), ppid=int(fields[1]),
                pgid=int(fields[2]), state=fields[0])


def _get_subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    value = ctypes.c_int()
    if libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(value), 0, 0, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return value.value


def _set_subreaper(value):
    if value not in (0, 1):
        raise ValueError('subreaper must be zero or one')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, value, 0, 0, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def _absolute(value, label):
    path = Path(value)
    if not path.is_absolute() or any(c in str(path) for c in ('\0', '\n', '%')):
        raise ValueError(label + ' must be an absolute literal path')
    return path


class Session:
    """A single collection in one exact named interactive session.

    start() must complete before launch(). The caller holds the target alive
    through stop(), then independently stops/reaps its target, calls shutdown(),
    reaps proven adopted identities and closes this object's subreaper scope.
    shutdown --kill=none cancels any unfinished collection; it never repairs a
    failed stop. All control commands and launch are attempted at most once.
    """

    def __init__(self, nsys, output, name, *, cwd, env, deadline, check=None):
        self.nsys = _absolute(nsys, 'Nsight executable')
        self.output = _absolute(output, 'receipt directory')
        self.cwd = _absolute(cwd, 'working directory')
        if not self.output.is_dir():
            raise ValueError('receipt directory must already exist')
        self.owner = proc_identity(os.getpid())
        prefix = f'q4t-suffix-{self.owner["pid"]}-{self.owner["start_ticks"]}-'
        if (not isinstance(name, str) or not name.startswith(prefix) or
                not re.fullmatch(r'q4t-suffix-[1-9][0-9]*-[0-9]+-[a-f0-9]{16}-[a-z0-9][a-z0-9-]{0,63}', name)):
            raise ValueError('session name must identify this step driver')
        if not math.isfinite(deadline):
            raise ValueError('finite absolute monotonic deadline required')
        self.name, self.env = name, dict(env)
        self.deadline, self.check = deadline, check
        self.launch_process = None
        self.start_succeeded = self.stop_succeeded = self.shutdown_succeeded = False
        self._attempted = set()
        self._spawned = set()
        self._popen_pids = set()
        self._known_owned = {}
        self._original_subreaper = None
        self._subreaper_attempted = False
        self._closed = False

    @property
    def start_attempted(self):
        return 'start' in self._attempted

    @property
    def shutdown_attempted(self):
        return 'shutdown' in self._attempted

    @property
    def can_shutdown(self):
        return not self._closed and 'start' in self._spawned and not self.shutdown_attempted

    def _owner_current(self):
        current = proc_identity(self.owner['pid'])
        if current['start_ticks'] != self.owner['start_ticks']:
            raise RuntimeError('stable step-driver identity changed')
        return current

    def _prepare_owner(self):
        self._subreaper_attempted = True
        record = dict(owner=self.owner, started_monotonic=time.monotonic(),
                      original_subreaper=None, verified_subreaper=None,
                      failure=None, scope='Only this step driver; not model runtime')
        try:
            self._owner_current()
            self._original_subreaper = _get_subreaper()
            record['original_subreaper'] = self._original_subreaper
            _set_subreaper(1)
            record['verified_subreaper'] = _get_subreaper()
            if record['verified_subreaper'] != 1:
                raise RuntimeError('subreaper set/get verification failed')
        except BaseException as error:
            record['failure'] = repr(error)
            raise
        finally:
            record['finished_monotonic'] = time.monotonic()
            _save(self.output / 'interactive-owner.json', record)

    def _control(self, verb, command, timeout, check=None, on_success=None):
        if verb in self._attempted:
            raise RuntimeError('control may only be attempted once: ' + verb)
        maximum = {'start': 30, 'stop': 45, 'shutdown': 15}[verb]
        if not math.isfinite(timeout) or not 0 < timeout <= maximum:
            raise ValueError('control timeout exceeds fixed bound')
        self._attempted.add(verb)
        started = time.monotonic()
        end = min(self.deadline, started + timeout)
        record = dict(verb=verb, session=self.name, command=command,
            owner=self.owner, started_monotonic=started, deadline_monotonic=end,
            finished_monotonic=None, pid=None, returncode=None, failure=None,
            forced_control_cleanup=False, cleanup_errors=[],
            scope='Control process only; not target exit or trace completeness')
        child = None
        _save(self.output / f'interactive-{verb}-command.json', command)
        try:
            self._owner_current()
            with (self.output / f'interactive-{verb}.log').open('x') as log:
                if self.check:
                    self.check()
                if check:
                    check()
                if time.monotonic() >= end:
                    raise TimeoutError('interactive control budget expired before spawn')
                child = subprocess.Popen(command, cwd=self.cwd, env=self.env,
                    stdout=log, stderr=subprocess.STDOUT)
                record['pid'] = child.pid
                self._popen_pids.add(child.pid)
                self._spawned.add(verb)
                while child.poll() is None:
                    if self.check:
                        self.check()
                    if check:
                        check()
                    if time.monotonic() >= end:
                        raise TimeoutError('interactive control exceeded deadline: ' + verb)
                    time.sleep(.05)
                if time.monotonic() > end:
                    raise TimeoutError('interactive control returned after deadline: ' + verb)
                if child.returncode != 0:
                    raise RuntimeError(f'interactive {verb} failed with {child.returncode}')
                if on_success:
                    record.update(on_success())
                if time.monotonic() > end:
                    raise TimeoutError('interactive receipt completed after deadline: ' + verb)
        except BaseException as error:
            record['failure'] = repr(error)
            raise
        finally:
            if child is not None and child.poll() is None:
                record['forced_control_cleanup'] = True
                try:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                except BaseException as error:
                    record['cleanup_errors'].append(repr(error))
            record['returncode'] = child.returncode if child else None
            record['finished_monotonic'] = time.monotonic()
            _save(self.output / f'interactive-{verb}-exit.json', record)
        return record

    def start(self, timeout=30):
        if self._closed or self._subreaper_attempted:
            raise RuntimeError('session start may only be attempted once')
        self._prepare_owner()
        command = [str(self.nsys), 'start', *START_OPTIONS,
                   '--session-new=' + self.name,
                   '--output=' + str(self.output / 'trace')]
        result = self._control('start', command, timeout)
        self.start_succeeded = True
        return result

    def launch(self, target, stdout, envvars=None):
        if (self._closed or not self.start_succeeded or
                'launch' in self._attempted or 'stop' in self._attempted or
                self.shutdown_attempted):
            raise RuntimeError('launch requires one successfully started session')
        if not target or not all(isinstance(v, str) and '\0' not in v for v in target):
            raise ValueError('nonempty literal argv required')
        _absolute(target[0], 'target executable')
        envvars = {} if envvars is None else dict(envvars)
        encoded = []
        for key, value in sorted(envvars.items()):
            if (not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', key) or
                    not isinstance(value, str) or
                    any(c in value for c in ('\0', '\n', ','))):
                raise ValueError('environment cannot be encoded by Nsight --env-var')
            encoded.append(key + '=' + value)
        command = [str(self.nsys), 'launch', *LAUNCH_OPTIONS,
                   '--session=' + self.name]
        if encoded:
            command.append('--env-var=' + ','.join(encoded))
        command.extend(target)
        self._attempted.add('launch')
        _save(self.output / 'interactive-launch-command.json', command)
        record = dict(session=self.name, owner=self.owner, pid=None,
            started_monotonic=time.monotonic(), failure=None,
            scope='Launch-control identity; caller independently binds target')
        try:
            self._owner_current()
            if self.check:
                self.check()
            if time.monotonic() >= self.deadline:
                raise TimeoutError('interactive launch budget expired')
            self.launch_process = subprocess.Popen(command, cwd=self.cwd,
                env=self.env, stdout=stdout, stderr=subprocess.STDOUT)
            record['pid'] = self.launch_process.pid
            self._popen_pids.add(self.launch_process.pid)
        except BaseException as error:
            record['failure'] = repr(error)
            raise
        finally:
            record['finished_monotonic'] = time.monotonic()
            _save(self.output / 'interactive-launch.json', record)
        return self.launch_process

    def stop(self, timeout=45, check=None):
        if (self._closed or not self.start_succeeded or self.launch_process is None
                or self.shutdown_attempted):
            raise RuntimeError('stop requires this session to have launched its target')
        result = self._control('stop', [str(self.nsys), 'stop',
            '--session=' + self.name, '--keep=0'], timeout, check,
            on_success=self._report_receipt)
        self.stop_succeeded = True
        return result

    def _report_receipt(self):
        report = self.output / 'trace.nsys-rep'
        info = report.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise RuntimeError('stop did not produce a nonempty regular report')
        return dict(report=dict(path=str(report), bytes=info.st_size,
            scope='Presence after successful stop only; export and completeness still required'))

    def shutdown(self, timeout=15):
        if not self.can_shutdown:
            raise RuntimeError('shutdown requires one spawned owned session start')
        result = self._control('shutdown', [str(self.nsys), 'shutdown',
            '--session=' + self.name, '--kill=none'], timeout)
        self.shutdown_succeeded = True
        return result

    def prove_descendant(self, pid):
        """Prove ancestry to the live stable owner, not the launch-control PID."""
        if self._closed or not self.start_succeeded or pid == self.owner['pid']:
            raise RuntimeError('target is not an active owned session descendant')
        current, chain, seen = pid, [], set()
        for _ in range(64):
            if current <= 1 or current in seen:
                raise RuntimeError('target ancestry does not reach stable step driver')
            seen.add(current)
            row = proc_identity(current)
            if chain and chain[-1]['start_ticks'] < row['start_ticks']:
                raise RuntimeError('target ancestor identity was recycled')
            if current == self.owner['pid']:
                if row['start_ticks'] != self.owner['start_ticks']:
                    raise RuntimeError('step-driver identity mismatch in ancestry')
                self._owner_current()
                # Recheck every identity before admitting it to owned history.
                for entry in chain:
                    if proc_identity(entry['pid'])['start_ticks'] != entry['start_ticks']:
                        raise RuntimeError('descendant identity changed during proof')
                self._known_owned.update({r['pid']: r for r in chain})
                return dict(owner=self.owner, target=chain[0], chain=chain)
            chain.append(row)
            current = row['ppid']
        raise RuntimeError('target ancestry exceeded frozen bound')

    def reap_adopted(self, timeout=5):
        """Only reap known terminal adopted children; never a Popen child."""
        if not math.isfinite(timeout) or not 0 <= timeout <= 5:
            raise ValueError('adopted reap timeout must be between zero and five seconds')
        if 'reap' in self._attempted:
            raise RuntimeError('adopted reap may only be attempted once')
        self._attempted.add('reap')
        self._reap_progress = dict(failure=None, known_identities=list(self._known_owned.values()))
        try:
            return self._reap_adopted(timeout)
        except BaseException as error:
            self._reap_progress['failure'] = repr(error)
            raise
        finally:
            self._reap_progress['finished_monotonic'] = time.monotonic()
            _save(self.output / 'interactive-reap.json', self._reap_progress)

    def _reap_adopted(self, timeout):
        self._owner_current()
        started = time.monotonic()
        end = min(self.deadline, started + timeout)
        pending = {p: r for p, r in self._known_owned.items() if p not in self._popen_pids}
        result = self._reap_progress
        result.update(reaped=[], remaining=[], not_children=[], vanished=[],
            excluded_popen_pids=sorted(self._popen_pids),
            started_monotonic=started, deadline_monotonic=end,
            scope='Only previously proved identities; no whole-system cleanup claim')
        while True:
            result['remaining'], result['not_children'] = [], []
            for pid, expected in sorted(pending.items()):
                try:
                    actual = proc_identity(pid)
                except (FileNotFoundError, ProcessLookupError):
                    result['vanished'].append(expected)
                    del pending[pid]
                    continue
                if actual['start_ticks'] != expected['start_ticks']:
                    raise RuntimeError('adopted PID reused; refusing waitpid')
                if actual['ppid'] != self.owner['pid']:
                    result['not_children'].append(actual)
                    result['remaining'].append(actual)
                    continue
                if actual['state'] not in ('Z', 'X'):
                    result['remaining'].append(actual)
                    continue
                try:
                    waited, status = os.waitpid(pid, os.WNOHANG)
                except OSError as error:
                    if error.errno != errno.ECHILD:
                        raise
                    result['not_children'].append(actual)
                    result['remaining'].append(actual)
                    continue
                if waited == pid:
                    result['reaped'].append(dict(actual, wait_status=status,
                        returncode=os.waitstatus_to_exitcode(status)))
                    del pending[pid]
                elif waited == 0:
                    result['remaining'].append(actual)
                else:
                    raise RuntimeError('waitpid returned an unexpected identity')
            if not pending or time.monotonic() >= end:
                break
            time.sleep(min(.05, max(0, end - time.monotonic())))
        result['finished_monotonic'] = time.monotonic()
        result['complete'] = not pending
        return result

    def close(self):
        """Restore the prior subreaper flag; never silently stop or kill."""
        if self._closed:
            raise RuntimeError('session close may only be attempted once')
        self._closed = True
        record = dict(owner=self.owner, original_subreaper=self._original_subreaper,
            restored_subreaper=None, start_succeeded=self.start_succeeded,
            stop_succeeded=self.stop_succeeded,
            shutdown_succeeded=self.shutdown_succeeded, failure=None,
            scope='Subreaper restoration only; not collection or target success')
        try:
            if self._original_subreaper is not None:
                self._owner_current()
                _set_subreaper(self._original_subreaper)
                record['restored_subreaper'] = _get_subreaper()
                if record['restored_subreaper'] != self._original_subreaper:
                    raise RuntimeError('subreaper restoration verification failed')
        except BaseException as error:
            record['failure'] = repr(error)
            raise
        finally:
            _save(self.output / 'interactive-close.json', record)
        return record
