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
                      directory_fd=directory_fd, errno=error.errno)
        error.telemetry_context = detail
        error.add_note('telemetry pinned proc read: ' + json.dumps(detail, sort_keys=True))
        raise


def _transient(error):
    return isinstance(error, OSError) and error.errno in (errno.ENOENT, errno.ESRCH)


def _read_task(directory_fd, counts, expected_start=None, with_io=False, context=None):
    """Read through a pinned proc directory, validating identity twice."""
    initial = _read_at(directory_fd, 'stat', counts, context)
    identity = _stat_identity(initial)
    if expected_start is not None and identity['start_ticks'] != expected_start:
        return dict(observation='PID_REUSED', expected_start_ticks=expected_start,
                    observed_start_ticks=identity['start_ticks'])
    row = dict(observation='OK', start_ticks=identity['start_ticks'], stat=initial)
    try:
        for name in ('status', 'schedstat') + (('io',) if with_io else ()):
            row[name] = _read_at(directory_fd, name, counts, context)
        final = _read_at(directory_fd, 'stat', counts, context)
        if _stat_identity(final)['start_ticks'] != identity['start_ticks']:
            return dict(observation='PID_REUSED_DURING_READ',
                        expected_start_ticks=identity['start_ticks'])
        row['stat_after'] = final
    except OSError as error:
        detail = getattr(error, 'telemetry_context', {})
        if (with_io and error.errno == errno.EACCES and
                detail.get('relative_file') == 'io'):
            # A new numeric PID is never followed. Only this already pinned
            # proc directory may prove that the same task has actually exited.
            try:
                after_error = _read_at(directory_fd, 'stat', counts, context)
                after_identity = _stat_identity(after_error)
            except OSError as recheck_error:
                error.add_note('pinned identity recheck failed: ' + repr(recheck_error))
            else:
                if (after_identity['start_ticks'] == identity['start_ticks'] and
                        after_identity['state'] in ('Z', 'X')):
                    row.update(observation='EXITED_DURING_IO_READ',
                               stat_after_error=after_error,
                               terminal_error=dict(detail, error=repr(error)),
                               terminal_error_traceback=traceback.format_exc())
                    return row
        if not _transient(error):
            raise
        # Thread/process exit is normal. Keep only data read through this
        # pinned proc directory; never follow a reused numeric PID/TID.
        row.update(observation='VANISHED_DURING_READ', error=str(error))
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
    for pid in owned:
        identity = records[pid]
        try:
            fd = os.open(f'/proc/{pid}', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as error:
            if not _transient(error):
                raise
            result[pid] = dict(identity=identity, observation='VANISHED')
            continue
        try:
            try:
                row = _read_task(fd, counts, identity['start_ticks'], with_io=True,
                                 context=dict(pid=pid, expected_start_ticks=identity['start_ticks']))
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
                  'directory_scan_attempts')
    total_counts = dict.fromkeys(count_keys, 0)
    counts = None
    sample_started = None
    attempts = flushed_samples = setup_file_reads = 0
    previous_cycle_seconds = previous_write_flush_seconds = None
    cycle_seconds_total = write_flush_seconds_total = 0.0
    sampler_failure = None
    sampler_failure_context = sampler_failure_traceback = None
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
                              directory_scan_attempts=0)
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
