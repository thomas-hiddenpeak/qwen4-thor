"""Frozen, read-only environment telemetry for the zero-hit stage.

Preparation calls discover_sources() and freezes its JSON result. The stage
calls configure_sources(manifest), then installs sample as legacy.sample_memory.
Importing this module does not discover or sample anything. No NVML/CUDA calls,
frequency changes, cache changes, or subprocesses are made here.
"""

import errno
import json
import os
from pathlib import Path
import re
import select
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/evalscope'))
import gpu_exclusive

INTERVAL_SECONDS = 1.0
MAX_OWNED_PIDS = 2048
MAX_Q4T_THREADS = 128
MAX_SOURCES = 512
EXIT_CONFIRM_TASK_MS = 50
EXIT_CONFIRM_SAMPLE_MS = 100
PROC_SOURCES = (
    ('meminfo', '/proc/meminfo'),
    ('cpu_stat', '/proc/stat'),
    ('pressure_cpu', '/proc/pressure/cpu'),
    ('pressure_io', '/proc/pressure/io'),
    ('pressure_memory', '/proc/pressure/memory'),
    ('vmstat', '/proc/vmstat'),
)
GPU_DOMAINS = ('gpu-gpc-0', 'gpu-nvd-0', 'bwmgr')
GPU_FIELDS = ('cur_freq', 'min_freq', 'max_freq', 'governor')
CPU_FIELDS = ('scaling_cur_freq', 'scaling_min_freq', 'scaling_max_freq',
              'scaling_governor')
_configured = None


class EnvironmentAbort(RuntimeError):
    pass


def _source_allowed(label, path):
    if (label, path) in PROC_SOURCES:
        return True
    if any(label == f'{domain}.{field}' and
           path == f'/sys/class/devfreq/{domain}/{field}'
           for domain in GPU_DOMAINS for field in GPU_FIELDS):
        return True
    cpu = re.fullmatch(r'/sys/devices/system/cpu/cpufreq/(policy\d+)/'
                       r'(scaling_cur_freq|scaling_min_freq|scaling_max_freq|'
                       r'scaling_governor)', path)
    if cpu and label == f'cpu.{cpu[1]}.{cpu[2]}':
        return True
    thermal = re.fullmatch(r'/sys/class/thermal/(thermal_zone\d+)/(type|temp)',
                           path)
    return bool(thermal and label == f'{thermal[1]}.{thermal[2]}')


def discover_sources():
    """List paths/labels and readability only; do not read sensor values.

    Readability is an access check, not proof that a powered-off device accepts
    reads. Missing/unreadable paths remain explicitly UNAVAILABLE. Every listed
    readable source becomes mandatory after freeze: even an EIO on its first
    sample is an environment failure, never a synthetic zero frequency.
    """
    sources, missing = [], []

    def add(label, path):
        path = Path(path)
        if path.is_file() and os.access(path, os.R_OK):
            sources.append(dict(label=label, path=str(path)))
        else:
            missing.append(dict(label=label, path=str(path),
                                status='UNAVAILABLE',
                                reason='missing_or_not_readable_at_discovery'))

    for label, path in PROC_SOURCES:
        add(label, path)
    for domain in GPU_DOMAINS:
        for field in GPU_FIELDS:
            add(f'{domain}.{field}',
                f'/sys/class/devfreq/{domain}/{field}')
    for root, pattern, prefix, fields in (
        (Path('/sys/devices/system/cpu/cpufreq'), 'policy*', 'cpu.', CPU_FIELDS),
        (Path('/sys/class/thermal'), 'thermal_zone*', '', ('type', 'temp')),
    ):
        directories = sorted(p for p in root.glob(pattern)
                             if re.fullmatch(r'(policy|thermal_zone)\d+', p.name)
                             and p.is_dir())
        if not directories:
            missing.append(dict(label=prefix + pattern, path=str(root),
                                status='UNAVAILABLE',
                                reason='no_matching_directories_at_discovery'))
        for directory in directories:
            for field in fields:
                add(f'{prefix}{directory.name}.{field}', directory / field)
    if len(sources) + len(missing) > MAX_SOURCES:
        raise EnvironmentAbort('source discovery exceeds frozen safety cap')
    available = {row['label'] for row in sources}
    return dict(schema=1, sources=sources, missing=missing,
                gpu_current_frequency={
                    domain: ('DISCOVERED_READABLE' if domain + '.cur_freq'
                             in available else 'UNAVAILABLE')
                    for domain in GPU_DOMAINS},
                values_read_at_discovery=False,
                interval_seconds=INTERVAL_SECONDS,
                max_owned_pids=MAX_OWNED_PIDS,
                max_q4t_threads=MAX_Q4T_THREADS,
                read_policy='all discovered sources required during sampling')


def configure_sources(manifest):
    """Validate and bind a frozen JSON source list without accessing paths."""
    global _configured
    if not isinstance(manifest, dict) or manifest.get('schema') != 1:
        raise ValueError('unsupported telemetry source manifest')
    if (manifest.get('values_read_at_discovery') is not False or
            manifest.get('interval_seconds') != INTERVAL_SECONDS or
            manifest.get('max_owned_pids') != MAX_OWNED_PIDS or
            manifest.get('max_q4t_threads') != MAX_Q4T_THREADS):
        raise ValueError('telemetry manifest limits/policy differ')
    sources, missing = manifest.get('sources'), manifest.get('missing')
    if not isinstance(sources, list) or not isinstance(missing, list):
        raise ValueError('telemetry manifest requires sources and missing lists')
    if len(sources) + len(missing) > MAX_SOURCES:
        raise ValueError('telemetry source manifest exceeds safety cap')
    labels, paths, bound = set(), set(), []
    for row in sources:
        if not isinstance(row, dict) or set(row) != {'label', 'path'}:
            raise ValueError('malformed telemetry source')
        label, path = row['label'], row['path']
        if (not isinstance(label, str) or not isinstance(path, str) or
                not _source_allowed(label, path) or label in labels or
                path in paths):
            raise ValueError('invalid or duplicate telemetry source')
        labels.add(label)
        paths.add(path)
        bound.append((label, path))
    # The legacy resource ledger must never silently lose system RAM or CPU
    # accounting. Optional pressure/sysfs capabilities may be unavailable.
    if not {'meminfo', 'cpu_stat', 'vmstat'} <= labels:
        raise ValueError('mandatory proc telemetry sources unavailable')
    for row in missing:
        if (not isinstance(row, dict) or row.get('status') != 'UNAVAILABLE' or
                not isinstance(row.get('label'), str) or
                not isinstance(row.get('path'), str) or
                not isinstance(row.get('reason'), str) or
                row['label'] in labels):
            raise ValueError('malformed unavailable telemetry source')
        labels.add(row['label'])
    current = {
        domain: ('DISCOVERED_READABLE' if domain + '.cur_freq' in
                 {label for label, _ in bound} else 'UNAVAILABLE')
        for domain in GPU_DOMAINS}
    if manifest.get('gpu_current_frequency') != current:
        raise ValueError('GPU frequency availability does not match sources')
    # Store only immutable values; later caller edits cannot change the plan.
    frozen = (tuple(bound), json.dumps(missing, sort_keys=True),
              json.dumps(current, sort_keys=True))
    if _configured is not None and _configured != frozen:
        raise ValueError('telemetry source manifest already configured')
    _configured = frozen


class _CountedProcPath:
    """Path facade for the existing ownership scanner's exact read counts.

    gpu_exclusive.processes reads stat/cmdline and resolves exe for ownership.
    Those raw records are retained only for owned processes. This local facade
    counts its reads without monkeypatching Path or other sampling threads.
    """

    def __init__(self, path, counts):
        self.path, self.counts = path, counts

    @property
    def name(self):
        return self.path.name

    def __truediv__(self, name):
        return _CountedProcPath(self.path / name, self.counts)

    def __fspath__(self):
        # The imported scanner uses this protocol only for os.readlink(exe).
        if self.path.name == 'exe':
            self.counts['ownership_readlink_attempts'] += 1
        return str(self.path)

    def iterdir(self):
        self.counts['directory_scan_attempts'] += 1
        for path in self.path.iterdir():
            yield _CountedProcPath(path, self.counts)

    def read_text(self):
        self.counts['ownership_file_read_attempts'] += 1
        return self.path.read_text()

    def read_bytes(self):
        self.counts['ownership_file_read_attempts'] += 1
        return self.path.read_bytes()


def _stat_identity(raw):
    fields = raw.rsplit(')', 1)[1].split()
    return dict(state=fields[0], ppid=int(fields[1]),
                start_ticks=int(fields[19]))


def _read_at(directory_fd, name, counts, context=None):
    counts['owned_file_read_attempts'] += 1
    operation = 'open'
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=directory_fd)
        operation = 'fdopen/read/close'
        with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as stream:
            return stream.read()
    except OSError as error:
        detail = dict(context or {}, relative_file=name, operation=operation,
                      directory_fd=directory_fd, errno=error.errno,
                      error_monotonic_ns=time.monotonic_ns())
        error.telemetry_context = detail
        error.add_note('telemetry pinned proc read: ' + json.dumps(detail, sort_keys=True))
        raise


def _transient(error):
    return isinstance(error, OSError) and error.errno in (errno.ENOENT, errno.ESRCH)


def _poll_exit_fd(pidfd, timeout_ms):
    """Observe one already-owned pidfd; never reap, signal, or follow a PID."""
    started = time.monotonic_ns()
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    events = poller.poll(timeout_ms)
    ended = time.monotonic_ns()
    result = dict(started_ns=started, finished_ns=ended, timeout_ms=timeout_ms,
                  events=[list(item) for item in events])
    if any(fd != pidfd or flags & (select.POLLERR | select.POLLNVAL)
           for fd, flags in events):
        error = OSError(errno.EIO, 'invalid owned pidfd poll result')
        error.telemetry_poll = result
        raise error
    result['ready'] = any(flags & (select.POLLIN | select.POLLHUP)
                          for _, flags in events)
    if events and not result['ready']:
        error = OSError(errno.EIO, 'unexpected owned pidfd poll flags')
        error.telemetry_poll = result
        raise error
    return result


def _recheck_task(directory_fd, counts, context):
    """Retain the raw identity even when it is live, changed, or unreadable."""
    result = dict(started_ns=time.monotonic_ns())
    try:
        raw = _read_at(directory_fd, 'stat', counts, context)
        result['stat'] = raw
        result['identity'] = _stat_identity(raw)
    except (OSError, ValueError, IndexError) as error:
        result.update(error=repr(error), errno=getattr(error, 'errno', None),
                      context=getattr(error, 'telemetry_context', None))
    result['finished_ns'] = time.monotonic_ns()
    return result


def _io_exit_confirmation(directory_fd, counts, context, identity,
                          exit_fd, live_bound, exit_budget, state):
    """Classify only a retained io denial with same-task terminal evidence.

    A subsequent pidfd exit proves a bounded terminal observation, not the
    cause of EACCES or the task's exit state at the earlier error timestamp.
    """
    immediate = _recheck_task(directory_fd, counts, context)
    state['rechecks'] = [immediate]
    after = immediate.get('identity')
    if after is not None:
        if after['start_ticks'] != identity['start_ticks']:
            state['decision'] = 'IDENTITY_CHANGED'
            return None
        if after['state'] in ('Z', 'X'):
            state['decision'] = 'SAME_IDENTITY_TERMINAL_STAT'
            return 'EXITED_DURING_IO_READ'
    elif immediate.get('errno') not in (errno.ENOENT, errno.ESRCH):
        state['decision'] = 'IDENTITY_RECHECK_FAILED'
        return None
    if exit_fd is None or not live_bound:
        state['decision'] = 'NO_PRIOR_LIVE_PIDFD_BINDING'
        return None
    if exit_budget is None:
        state['decision'] = 'NO_EXIT_WAIT_BUDGET'
        return None
    remaining_ns = max(0, exit_budget['remaining_ns'])
    task_deadline_ns = (state['error_observed_ns'] +
                        EXIT_CONFIRM_TASK_MS * 1000000)
    state['exit_confirmation_deadline_ns'] = task_deadline_ns
    available_ns = min(remaining_ns, max(0, task_deadline_ns - time.monotonic_ns()))
    timeout_ms = min(EXIT_CONFIRM_TASK_MS, available_ns // 1000000)
    if timeout_ms <= 0:
        state['decision'] = 'EXIT_WAIT_BUDGET_EXHAUSTED'
        return None
    started = time.monotonic_ns()
    try:
        counts['pidfd_wait_attempts'] = counts.get('pidfd_wait_attempts', 0) + 1
        confirmation = _poll_exit_fd(exit_fd, timeout_ms)
        state['exit_confirmation'] = confirmation
    except OSError as error:
        state['exit_confirmation'] = dict(
            error=repr(error), errno=error.errno,
            poll=getattr(error, 'telemetry_poll', None))
        state['decision'] = 'EXIT_POLL_FAILED'
        return None
    finally:
        spent = time.monotonic_ns() - started
        exit_budget['remaining_ns'] = max(0, remaining_ns - spent)
        exit_budget['spent_ns'] += spent
        state['exit_wait_spent_ns'] = spent
        state['sample_exit_wait_remaining_ns'] = exit_budget['remaining_ns']
    if time.monotonic_ns() > task_deadline_ns or spent > remaining_ns:
        state['decision'] = 'EXIT_CONFIRMATION_BUDGET_OVERRUN'
        return None
    if not confirmation['ready']:
        state['decision'] = 'LIVE_TASK_EXIT_TIMEOUT'
        return None
    final = _recheck_task(directory_fd, counts, context)
    state['rechecks'].append(final)
    if time.monotonic_ns() > task_deadline_ns:
        state['decision'] = 'EXIT_CONFIRMATION_BUDGET_OVERRUN'
        return None
    after = final.get('identity')
    if after is not None:
        if after['start_ticks'] != identity['start_ticks']:
            state['decision'] = 'IDENTITY_CHANGED_AFTER_EXIT_POLL'
            return None
        if after['state'] not in ('Z', 'X'):
            state['decision'] = 'EXIT_POLL_CONTRADICTS_LIVE_STAT'
            return None
    elif final.get('errno') not in (errno.ENOENT, errno.ESRCH):
        state['decision'] = 'FINAL_IDENTITY_RECHECK_FAILED'
        return None
    state['decision'] = 'BOUND_PIDFD_EXIT_CONFIRMED_AFTER_IO_DENIAL'
    state['io_missing'] = True
    state['cause_of_permission_error_proven'] = False
    return 'TERMINATED_AFTER_IO_DENIAL'


def _read_task(directory_fd, counts, expected_start=None, with_io=False,
               context=None, exit_fd=None, exit_budget=None):
    """Read a pinned task; every failed path preserves the acquired state."""
    started = time.monotonic_ns()
    row, identity, binding = {}, None, None
    live_bound = False
    try:
        initial = _read_at(directory_fd, 'stat', counts, context)
        row['stat'] = initial
        identity = _stat_identity(initial)
        if expected_start is not None and identity['start_ticks'] != expected_start:
            return dict(observation='PID_REUSED', expected_start_ticks=expected_start,
                        observed_start_ticks=identity['start_ticks'], stat=initial)
        row = dict(observation='OK', start_ticks=identity['start_ticks'], stat=initial)
        if exit_fd is not None:
            counts['pidfd_binding_polls'] = counts.get('pidfd_binding_polls', 0) + 1
            binding = _poll_exit_fd(exit_fd, 0)
            # pidfd_open precedes procdir open. A nonterminal pidfd after the
            # initial stat excludes stitching a retired pidfd to a reused PID.
            live_bound = not binding['ready']
            row['pidfd_binding'] = binding
        for name in ('status', 'schedstat') + (('io',) if with_io else ()):
            row[name] = _read_at(directory_fd, name, counts, context)
        final = _read_at(directory_fd, 'stat', counts, context)
        row['stat_after'] = final
        if _stat_identity(final)['start_ticks'] != identity['start_ticks']:
            return dict(observation='PID_REUSED_DURING_READ',
                        expected_start_ticks=identity['start_ticks'],
                        partial_read=row, stat_after=final)
        row['stat_after'] = final
    except (OSError, ValueError, IndexError) as error:
        detail = getattr(error, 'telemetry_context', {})
        state = dict(read_started_ns=started,
                     error_observed_ns=detail.get('error_monotonic_ns',
                                                  time.monotonic_ns()),
                     initial_identity=identity, partial_read=dict(row),
                     original_error=dict(detail, error=repr(error)),
                     live_pidfd_bound=live_bound, pidfd_binding=binding,
                     pidfd_binding_error=getattr(error, 'telemetry_poll', None),
                     rechecks=[], decision='UNCLASSIFIED_FATAL_READ')
        classification = None
        if (identity is not None and with_io and
                getattr(error, 'errno', None) == errno.EACCES and
                detail.get('relative_file') == 'io'):
            classification = _io_exit_confirmation(
                directory_fd, counts, context, identity, exit_fd, live_bound,
                exit_budget, state)
        state['finished_ns'] = time.monotonic_ns()
        error.telemetry_task_state = state
        # Attach to the original error, never substitute a recheck error/errno.
        error.add_note('telemetry task failure state: ' + json.dumps(state, sort_keys=True))
        if classification is not None:
            row.update(observation=classification,
                       terminal_error=dict(detail, error=repr(error)),
                       terminal_error_traceback=traceback.format_exc(),
                       terminal_task_state=state)
            if state['rechecks'][0].get('stat') is not None:
                row['stat_after_error'] = state['rechecks'][0]['stat']
            return row
        if not _transient(error):
            raise
        # No numerical PID is followed when an owned task disappears.
        row.update(observation='VANISHED_DURING_READ', error=str(error),
                   read_error_state=state)
    return row


def _thread_totals(threads):
    totals = dict(observed_threads=0, runtime_ns=0, runqueue_ns=0, timeslices=0,
                  utime_ticks=0, stime_ticks=0, minor_faults=0, major_faults=0,
                  voluntary_ctxt_switches=0, nonvoluntary_ctxt_switches=0)
    for row in threads.values():
        if row.get('observation') != 'OK':
            continue
        runtime, runqueue, slices = map(int, row['schedstat'].split())
        fields = row['stat'].rsplit(')', 1)[1].split()
        totals['observed_threads'] += 1
        for key, value in (('runtime_ns', runtime), ('runqueue_ns', runqueue),
                           ('timeslices', slices), ('utime_ticks', int(fields[11])),
                           ('stime_ticks', int(fields[12])),
                           ('minor_faults', int(fields[7])),
                           ('major_faults', int(fields[9]))):
            totals[key] += value
        status = dict(line.split(':', 1) for line in row['status'].splitlines()
                      if ':' in line)
        for key in ('voluntary_ctxt_switches', 'nonvoluntary_ctxt_switches'):
            if key not in status:
                raise EnvironmentAbort('q4t thread context switch fields missing')
            totals[key] += int(status[key])
    totals['scope'] = ('Cumulative counters of successfully observed live threads; '
                       'not per-interval deltas, not an atomic process snapshot. '
                       'Vanished threads are retained separately and not summed.')
    return totals


def _owned_snapshot(records, owner_pid, owner_start, history):
    root = records.get(owner_pid)
    if not root or root.get('start_ticks') != owner_start:
        raise EnvironmentAbort('sampler owner identity unavailable/changed')
    if any(records.get(pid, {}).get('unreadable') for pid in history):
        raise EnvironmentAbort('previously owned PID identity became unreadable')
    owned = {owner_pid}
    # Previously observed descendants remain owned if reparented after a
    # parent exits. Never reuse historical ownership for a different start.
    owned.update(pid for pid, start in history.items()
                 if records.get(pid, {}).get('start_ticks') == start)
    while True:
        added = {pid for pid, row in records.items()
                 if row.get('ppid') in owned and
                 isinstance(row.get('start_ticks'), int) and
                 row['start_ticks'] >= records[row['ppid']]['start_ticks']}
        previous = len(owned)
        owned.update(added)
        if len(owned) > MAX_OWNED_PIDS:
            raise EnvironmentAbort('more than 2048 owned processes')
        if len(owned) == previous:
            break
    for pid in owned:
        if records[pid].get('metadata_unreadable'):
            raise EnvironmentAbort('owned process executable metadata unreadable')
        history[pid] = records[pid]['start_ticks']
    if len(history) > MAX_OWNED_PIDS:
        raise EnvironmentAbort('owned process identity history exceeds 2048 PIDs')
    return sorted(owned)


def _sample_processes(records, owned, counts):
    result, thread_count = {}, 0
    exit_budget = dict(remaining_ns=EXIT_CONFIRM_SAMPLE_MS * 1000000, spent_ns=0)
    for pid in owned:
        identity = records[pid]
        pidfd = None
        try:
            counts['pidfd_open_attempts'] = counts.get('pidfd_open_attempts', 0) + 1
            pidfd = os.pidfd_open(pid)
            fd = os.open(f'/proc/{pid}', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as error:
            if pidfd is not None:
                os.close(pidfd)
            if not _transient(error):
                error.telemetry_task_state = dict(
                    pid=pid, expected_start_ticks=identity['start_ticks'],
                    errno=error.errno, monotonic_ns=time.monotonic_ns(),
                    decision='PIDFD_OR_PROC_BINDING_FAILED', error=repr(error))
                error.add_note('owned task binding: ' +
                               json.dumps(error.telemetry_task_state))
                raise
            result[pid] = dict(identity=identity, observation='VANISHED')
            continue
        try:
            try:
                row = _read_task(fd, counts, identity['start_ticks'], with_io=True,
                                 context=dict(pid=pid, expected_start_ticks=identity['start_ticks']),
                                 exit_fd=pidfd, exit_budget=exit_budget)
            except OSError as error:
                if not _transient(error):
                    raise
                row = dict(observation='VANISHED', error=str(error))
            row['identity'] = identity
            result[pid] = row
            executable = Path(identity.get('executable', '')).name
            if row['observation'] != 'OK' or not executable.startswith('q4t'):
                continue
            task_fd = None
            try:
                task_fd = os.open('task', os.O_RDONLY | os.O_DIRECTORY |
                                  os.O_CLOEXEC, dir_fd=fd)
                counts['directory_scan_attempts'] += 1
                tids = sorted(int(name) for name in os.listdir(task_fd)
                              if name.isdigit())
                thread_count += len(tids)
                if thread_count > MAX_Q4T_THREADS:
                    raise EnvironmentAbort('more than 128 owned q4t threads')
                threads = {}
                for tid in tids:
                    thread_fd = None
                    try:
                        thread_fd = os.open(str(tid), os.O_RDONLY | os.O_DIRECTORY |
                                            os.O_CLOEXEC, dir_fd=task_fd)
                        threads[tid] = _read_task(thread_fd, counts,
                            context=dict(pid=pid, tid=tid, parent_start_ticks=identity['start_ticks']))
                    except OSError as error:
                        if not _transient(error):
                            raise
                        threads[tid] = dict(observation='VANISHED', error=str(error))
                    finally:
                        if thread_fd is not None:
                            os.close(thread_fd)
                row['threads'] = threads
                row['thread_totals'] = _thread_totals(threads)
            except OSError as error:
                if not _transient(error):
                    raise
                row['threads_observation'] = 'VANISHED_DURING_DIRECTORY_READ'
            finally:
                if task_fd is not None:
                    os.close(task_fd)
        finally:
            os.close(fd)
            os.close(pidfd)
    return result, thread_count


def sample(output, stop, failures, heartbeat):
    """legacy.sample_memory signature; failures cause the stage ENV_ABORT.

    A complete flushed row refreshes heartbeat. Stable frozen sources failing
    at any point abort; short-lived owned processes/threads are marked vanished.
    Blocking kernel reads have no fabricated timeout here: the parent driver's
    stale-heartbeat and whole-stage owned-process timeout remain authoritative.
    """
    sampler_started, sampler_wall = time.monotonic(), time.time()
    count_keys = ('source_file_read_attempts', 'ownership_file_read_attempts',
                  'ownership_readlink_attempts', 'owned_file_read_attempts',
                  'directory_scan_attempts', 'pidfd_open_attempts',
                  'pidfd_binding_polls', 'pidfd_wait_attempts')
    total_counts = dict.fromkeys(count_keys, 0)
    counts = None
    sample_started = None
    attempts = flushed_samples = setup_file_reads = 0
    previous_cycle_seconds = previous_write_flush_seconds = None
    cycle_seconds_total = write_flush_seconds_total = 0.0
    sampler_failure = None
    sampler_failure_context = sampler_failure_traceback = None
    sampler_failure_task_state = None
    try:
        if _configured is None:
            raise EnvironmentAbort('telemetry sources must be frozen/configured')
        sources, missing_json, gpu_json = _configured
        owner_pid = os.getpid()
        setup_file_reads += 1
        owner_start = _stat_identity(Path('/proc/self/stat').read_text())['start_ticks']
        history = {}
        with Path(output).open('x') as stream:
            while not stop.is_set():
                if counts is not None:
                    for key, value in counts.items():
                        total_counts[key] += value
                started, wall = time.monotonic(), time.time()
                sample_started = started
                attempts += 1
                counts = dict(source_file_read_attempts=0,
                              ownership_file_read_attempts=0,
                              ownership_readlink_attempts=0,
                              owned_file_read_attempts=0,
                              directory_scan_attempts=0, pidfd_open_attempts=0,
                              pidfd_binding_polls=0, pidfd_wait_attempts=0)
                row = dict(unix_seconds=wall, monotonic=started,
                           source_values={}, unavailable=json.loads(missing_json),
                           gpu_current_frequency=json.loads(gpu_json))
                for label, path in sources:
                    counts['source_file_read_attempts'] += 1
                    try:
                        raw = Path(path).read_text()
                        if not raw.strip():
                            raise EnvironmentAbort(f'empty frozen source: {label}')
                    except OSError as error:
                        raise EnvironmentAbort(f'frozen source {label} ({path}): '
                                               f'{error}') from error
                    row['source_values'][label] = dict(path=path, status='OK', raw=raw)
                    if label == 'meminfo':
                        row['meminfo'] = raw  # Legacy unified RAM ledger contract.
                records = gpu_exclusive.processes(
                    proc=_CountedProcPath(Path('/proc'), counts))
                owned = _owned_snapshot(records, owner_pid, owner_start, history)
                row['processes'], row['q4t_threads_listed'] = _sample_processes(
                    records, owned, counts)
                row['owner'] = dict(pid=owner_pid, start_ticks=owner_start)
                row['owned_identity_history'] = dict(history)
                row['file_read_attempts'] = sum(counts[key] for key in (
                    'source_file_read_attempts', 'ownership_file_read_attempts',
                    'owned_file_read_attempts'))
                row['read_counts'] = counts
                ended = time.monotonic()
                row.update(sample_finished_monotonic=ended,
                           sample_finished_unix_seconds=time.time(),
                           sample_duration_seconds=ended - started,
                           sample_duration_scope='source collection and ownership '
                               'scan; excludes JSON serialization/write/flush',
                           previous_cycle_duration_seconds=previous_cycle_seconds,
                           previous_write_flush_seconds=previous_write_flush_seconds,
                           interval_overrun_seconds=max(0.0, ended - started -
                                                        INTERVAL_SECONDS))
                write_started = time.monotonic()
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                stream.flush()
                flushed = time.monotonic()
                flushed_samples += 1
                previous_cycle_seconds = flushed - started
                previous_write_flush_seconds = flushed - write_started
                cycle_seconds_total += previous_cycle_seconds
                write_flush_seconds_total += previous_write_flush_seconds
                # The next row records this whole-cycle cost. Preserve the
                # final cycle in heartbeat and the finally summary as well;
                # its serialization/flush cost has no following sample row.
                heartbeat.update(last_sample=flushed,
                                 last_cycle_duration_seconds=previous_cycle_seconds,
                                 last_write_flush_seconds=previous_write_flush_seconds)
                stop.wait(max(0.0, started + INTERVAL_SECONDS - time.monotonic()))
    except BaseException as error:
        sampler_failure = 'ENV_ABORT telemetry: ' + repr(error)
        sampler_failure_context = getattr(error, 'telemetry_context', None)
        sampler_failure_task_state = getattr(error, 'telemetry_task_state', None)
        sampler_failure_traceback = traceback.format_exc()
        failures.append(sampler_failure)
        stop.set()
    finally:
        # Count the final attempt even if a source read or flush failed. The
        # preceding attempts were accumulated at the next loop entry.
        if counts is not None:
            for key, value in counts.items():
                total_counts[key] += value
        ended = time.monotonic()
        summary = dict(
            schema=1, sampler_status='ENV_ABORT' if failures else 'STOPPED',
            scope='Sampler accounting only; not the inference stage outcome. '
                  'Summary is written before the sampler thread returns.',
            output=str(Path(output)),
            started_monotonic=sampler_started,
            started_unix_seconds=sampler_wall,
            finished_monotonic=ended, finished_unix_seconds=time.time(),
            sampler_elapsed_seconds=ended - sampler_started,
            sample_attempts=attempts, flushed_samples=flushed_samples,
            unflushed_attempts=attempts - flushed_samples,
            final_successful_cycle_duration_seconds=previous_cycle_seconds,
            final_successful_write_flush_seconds=previous_write_flush_seconds,
            successful_cycle_duration_total_seconds=cycle_seconds_total,
            successful_write_flush_total_seconds=write_flush_seconds_total,
            final_failed_attempt_elapsed_seconds=(
                ended - sample_started if sample_started is not None and
                attempts > flushed_samples else None),
            final_attempt_read_counts=counts,
            total_read_counts=total_counts,
            setup_file_read_attempts=setup_file_reads,
            total_file_read_attempts=setup_file_reads + sum(total_counts[key]
                for key in ('source_file_read_attempts',
                            'ownership_file_read_attempts',
                            'owned_file_read_attempts')),
            file_count_scope='Content read attempts including failed reads and '
                             'ownership scan; readlink/directory scans separate.',
            sampler_failure=sampler_failure, failures_at_exit=list(failures),
            sampler_failure_context=sampler_failure_context,
            sampler_failure_traceback=sampler_failure_traceback,
            sampler_failure_task_state=sampler_failure_task_state,
            exit_confirmation_policy=dict(per_task_ms=EXIT_CONFIRM_TASK_MS,
                per_sample_ms=EXIT_CONFIRM_SAMPLE_MS, io_retries=0, zero_fill=False),
            last_flushed_heartbeat=heartbeat.get('last_sample'),
            source_count=len(_configured[0]) if _configured is not None else None)
        try:
            path = Path(output).with_name('telemetry-summary.json')
            with path.open('x') as stream:
                json.dump(summary, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
        except BaseException as error:
            failures.append('ENV_ABORT telemetry summary: ' + repr(error))
            stop.set()
