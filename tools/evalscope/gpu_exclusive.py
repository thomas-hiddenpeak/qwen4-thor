"""Hold the repository-wide GPU lease and monitor noncooperating workloads.

The flock prevents cooperating sessions from overlapping. Process/NVIDIA polls
are supplementary and cannot prove absence of sub-poll or invisible workloads.
An environment abort never kills an unrelated process or implies a model failure.
"""
import argparse
import csv
import contextlib
import datetime
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import time

import nvml_process_query
from bounded_log import open_log

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMPUTE_BACKEND = 'nvml'


def common_lock():
    common = Path(subprocess.check_output(
        ['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
        cwd=ROOT, text=True).strip())
    return common.parent / '.q4t-work/gpu-exclusive.lock'


def processes(proc=Path('/proc')):
    result = {}
    for directory in proc.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            # comm may itself contain spaces and parentheses.
            fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            record = dict(state=fields[0], ppid=int(fields[1]),
                          pgid=int(fields[2]), start_ticks=int(fields[19]),
                          argv=[], executable='')
            # Exiting/zombie tasks retain stat (including ownership) after
            # exe/cmdline disappear. Do not discard that identity as if the
            # whole PID vanished between the driver and proc snapshots.
            result[int(directory.name)] = record
            try:
                argv = (directory / 'cmdline').read_bytes().split(b'\0')
                record['argv'] = [v.decode(errors='replace') for v in argv if v]
                record['executable'] = os.readlink(directory / 'exe')
            except (FileNotFoundError, ProcessLookupError):
                record['metadata_missing'] = True
            except PermissionError:
                record['metadata_unreadable'] = True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # NVIDIA query below additionally checks visible compute contexts.
            result[int(directory.name)] = {'unreadable': True}
    return result


def nvidia_pids():
    run = subprocess.run(
        ['nvidia-smi', '--query-compute-apps=pid', '--format=csv,noheader,nounits'],
        capture_output=True, text=True, timeout=5, check=True)
    pids = set()
    for row in csv.reader(io.StringIO(run.stdout)):
        if row and row[0].strip():
            pids.add(int(row[0].strip()))
    return pids


def gpu_command(record):
    argv = record.get('argv', [])
    name = Path(record.get('executable', '')).name
    return bool(argv) and (
        (name.startswith('q4t') and ('serve' in argv or 'test' in name)) or
        (name == 'nsys' and any(v in argv[1:3] for v in ('launch', 'profile'))))


def nvml_pids(log_path, log_max_bytes=None):
    result = nvml_process_query.query(timeout=5)
    with open_log(log_path, 'a', log_max_bytes) as log:
        log.write(json.dumps(result) + '\n')
    if result['status'] != 'OK':
        raise RuntimeError('NVML process query: ' + result['status'] +
                           '; details in ' + str(log_path))
    return set(result['pids'])


def owned_processes(records, owner_pgid, history):
    """Prove ownership by current ancestry or a previously observed identity."""
    if owner_pgid is None:
        return set()
    owned = {pid for pid, r in records.items()
             if r.get('pgid') == owner_pgid or
             (r.get('start_ticks') is not None and pid in history and
              r['start_ticks'] == history[pid].get('start_ticks'))}
    changed = True
    while changed:
        changed = False
        for pid, r in records.items():
            parent = r.get('ppid')
            if pid in owned or parent not in owned:
                continue
            parent_start = records[parent].get('start_ticks')
            if (parent_start is not None and r.get('start_ticks') is not None
                    and r['start_ticks'] >= parent_start):
                owned.add(pid)
                changed = True
    for pid in owned:
        history[pid] = records[pid].copy()
    return owned


def signal_identity(pid, identity, signum):
    """Signal a pidfd only after rechecking the observed start time."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        except FileNotFoundError:
            return
        if int(fields[19]) == identity['start_ticks']:
            signal.pidfd_send_signal(fd, signum)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def require_pidfd_support():
    """Prove this interpreter/kernel can safely signal owned processes."""
    missing = [name for module, name in [(os, 'pidfd_open'),
                                        (signal, 'pidfd_send_signal')]
               if not callable(getattr(module, name, None))]
    if missing:
        raise RuntimeError('GPU guard requires callable ' + ', '.join(missing) +
                           '; select a Python interpreter with pidfd support')
    try:
        fd = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(fd, 0)
        finally:
            os.close(fd)
    except OSError as error:
        raise RuntimeError('GPU guard pidfd capability probe failed: ' +
                           str(error)) from error


def inspect(owner_pgid=None, owned_history=None, compute_query=None):
    compute_query = compute_query or nvidia_pids
    compute = compute_query()  # Errors fail closed; never interpreted as idle.
    # Read process groups after the driver query so newly created owned GPU
    # children are not mistaken for unrelated processes from a stale snapshot.
    records = processes()
    # Keep PID/start-time identities, not a PID-only allowlist. Reused or
    # unreadable PIDs never inherit a previous process ownership proof.
    history = owned_history if owned_history is not None else {}
    owned = owned_processes(records, owner_pgid, history)
    candidates = {pid for pid in compute if pid not in records and pid in history}
    retired = []
    if candidates:
        # NVIDIA may report a context whose owned process exited before /proc
        # was read. Require a fresh query AND /proc snapshot to confirm exit.
        refreshed_compute = compute_query()
        refreshed_records = processes()
        for pid in candidates:
            if pid not in refreshed_compute and pid not in refreshed_records:
                retired.append(dict(pid=pid, identity=history[pid],
                                    reason='owned exit confirmed by fresh queries'))
            elif pid in refreshed_records:
                records[pid] = refreshed_records[pid]
        compute |= refreshed_compute
        records.update(refreshed_records)
    owned = owned_processes(records, owner_pgid, history)
    retired_pids = {r['pid'] for r in retired}
    conflicts = []
    for pid in records.keys() | compute:
        if pid in retired_pids:
            continue
        record = records.get(pid, {})
        if pid in owned:
            continue
        if pid in compute or gpu_command(record):
            conflicts.append(dict(pid=pid, **record,
                                  nvidia_compute_context=pid in compute))
    return {'conflicts': conflicts, 'retired_owned_contexts': retired,
            'unreadable_processes': sum(r.get('unreadable', False)
                                        for r in records.values())}


def stamp():
    return {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'monotonic': time.monotonic(), 'pid': os.getpid()}


def stop_owned(child, grace, history=None):
    history = history if history is not None else {}
    owned_processes(processes(), child.pid, history)
    # A private process group was created by us. Never signal a conflict PID.
    if child.poll() is None:
        child.send_signal(signal.SIGINT)
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    # Detached descendants (e.g. Nsight setsid targets) may outlive the
    # original group. Keep the lease while draining identities we proved.
    deadline = time.monotonic() + grace
    killed = False
    signaled = set()
    while True:
        records = processes()
        owned = owned_processes(records, child.pid, history)
        unreadable = [pid for pid in history if records.get(pid, {}).get('unreadable')]
        if unreadable:
            raise RuntimeError(f'cannot verify owned cleanup identities: {unreadable}')
        remaining = [pid for pid in owned if records[pid].get('state') != 'Z']
        if not remaining:
            return
        for pid in remaining:
            identity = history[pid]
            key = (pid, identity['start_ticks'], killed)
            if key not in signaled:
                signal_identity(pid, identity, signal.SIGKILL if killed else signal.SIGTERM)
                signaled.add(key)
        if time.monotonic() >= deadline:
            if killed:
                raise RuntimeError(f'owned descendants did not drain: {remaining}')
            killed = True
            deadline = time.monotonic() + grace
        time.sleep(0.1)


def borrowed_lock(fd, lock_path):
    """Duplicate an already exclusive lease; caller owns lifetime and recovery.

    Never unlock this open-file-description. A manager must retain another
    reference until its independent cleanup has completed.
    """
    if fd <= 2:
        raise ValueError('lease fd must not be a standard stream')
    supplied = os.fstat(fd)
    expected = lock_path.stat()
    if (supplied.st_dev, supplied.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError('lease fd does not identify the repository lock')
    if not stat.S_ISREG(supplied.st_mode):
        raise ValueError('lease fd must identify a regular file')
    if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR:
        raise ValueError('lease fd must be read/write')
    with lock_path.open('r+') as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise ValueError('lease must already be exclusively locked')
    # An independent FD for an inode held by someone else cannot acquire EX.
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return os.fdopen(os.dup(fd), 'r+')


def run_guard(command, out, lock_path, interval=1.0, grace=30.0, lease_fd=None,
              compute_backend=DEFAULT_COMPUTE_BACKEND, log_max_bytes=None,
              inherit_lease=False, managed_monitor=None, inherited_fds=()):
    if compute_backend not in ('nvidia-smi', 'nvml'):
        raise ValueError('unknown compute backend')
    if managed_monitor is not None and (
            not inherit_lease or compute_backend != 'nvml'):
        raise ValueError('managed monitoring requires inherited NVML lease')
    if (type(inherited_fds) not in (tuple, list) or len(inherited_fds) > 8 or
            any(type(fd) is not int or fd <= 2 for fd in inherited_fds) or
            len(set(inherited_fds)) != len(inherited_fds) or
            (inherited_fds and not inherit_lease)):
        raise ValueError('explicit inherited descriptors require a managed lease')
    for fd in inherited_fds:
        os.fstat(fd)  # Reject closed descriptors before any workload/monitor.
    out.mkdir(parents=True, exist_ok=False)
    scan = inspect
    if compute_backend == 'nvml':
        def scan(*args):
            return inspect(*args, compute_query=lambda: nvml_pids(
                out / 'nvml-queries.jsonl', log_max_bytes))
    if managed_monitor is not None:
        scan = managed_monitor
    status = {'status': 'INITIALIZING', 'command': command,
              'compute_backend': compute_backend,
              'lock_path': str(lock_path), 'poll_interval_seconds': interval,
              'poll_scope': 'cooperative flock plus /proc and NVIDIA compute polling',
              'performance_acceptance': False, 'child_returncode': None,
              'external_lease': lease_fd is not None}
    if managed_monitor is not None:
        status['monitor_policy'] = managed_monitor.state['policy']
    if log_max_bytes is not None:
        if type(log_max_bytes) is not int or log_max_bytes < 65536:
            raise ValueError('monitor log limit must be at least 64KiB')
        status['retention'] = dict(bytes_per_file=log_max_bytes, generations=2,
                                  complete_history_retained=False)
    child = None
    owned_history = {}
    acquired = False
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    event_log = (contextlib.nullcontext(managed_monitor.event_log(
                    out / 'events.jsonl', log_max_bytes))
                 if managed_monitor is not None else
                 open_log(out / 'events.jsonl', 'w', log_max_bytes))
    with contextlib.ExitStack() as stack, event_log as events:
        def record(event, **values):
            events.write(json.dumps(dict(stamp(), event=event, **values)) + '\n')
            events.flush()
        try:
            lock = stack.enter_context(
                lock_path.open('a+') if lease_fd is None else
                borrowed_lock(lease_fd, lock_path))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                status['status'] = 'LEASE_BUSY'
                lock.seek(0)
                status['holder_snapshot'] = lock.read()
                return 75
            # A recovery supervisor creates this durable marker before any
            # configuration write and removes it only after verified restore.
            # Check under the lease, including externally supplied leases.
            recovery = lock_path.with_name(lock_path.name + '.recovery-required')
            if os.path.lexists(recovery):
                status['status'] = 'RECOVERY_REQUIRED'
                status['reason'] = 'unfinished configuration recovery'
                status['recovery_marker'] = str(recovery)
                record('recovery_required', marker=str(recovery))
                return 75
            acquired = True
            lease = dict(stamp(), command=command, output=str(out), state='held')
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps(lease) + '\n')
            lock.flush()
            record('lease_acquired')
            require_pidfd_support()
            record('cleanup_capabilities', pidfd=True)
            snapshot = scan()
            record('preflight', **snapshot)
            if snapshot['conflicts']:
                status['status'] = 'ENV_ABORT'
                status['reason'] = 'competing workload before launch'
                return 75
            child_options = {}
            if inherit_lease:
                # Only explicit managed services inherit the open-file-
                # description. Their backend keeps it until its own exit, so
                # controller SIGKILL cannot expose an apparently free lease.
                child_options = dict(pass_fds=tuple(sorted({lock.fileno(), *inherited_fds})),
                    env=dict(os.environ, Q4T_MANAGED_LEASE_FD=str(lock.fileno())))
                status['lease_inherited_by_child'] = True
                status['explicit_inherited_fd_count'] = len(inherited_fds)
            child = subprocess.Popen(command, cwd=ROOT, start_new_session=True,
                                     **child_options)
            record('child_started', child_pid=child.pid)
            owned_history = {}
            while True:
                snapshot = scan(child.pid, owned_history)
                record('monitor', **snapshot)
                if snapshot['conflicts']:
                    status['status'] = 'ENV_ABORT'
                    status['reason'] = 'competing workload during lease'
                    return 75
                code = child.poll()
                if code is not None:
                    status['child_returncode'] = code
                    status['status'] = 'COMPLETED' if code == 0 else 'COMMAND_FAILED'
                    if (code == 0 and managed_monitor is not None and
                            managed_monitor.had_gaps):
                        status['status'] = 'COMPLETED_WITH_MONITOR_GAPS'
                    return 0 if code == 0 else 1
                time.sleep(interval)
        except (Exception, KeyboardInterrupt) as error:
            status['status'] = 'ENV_ABORT'
            status['reason'] = repr(error)
            return 75
        finally:
            cleanup_failed = False
            if child is not None:
                try:
                    stop_owned(child, grace, owned_history)
                except Exception as error:
                    status['status'] = 'ENV_ABORT'
                    status['cleanup_error'] = repr(error)
                    cleanup_failed = True
                status['child_returncode'] = child.returncode
            if managed_monitor is not None:
                try:
                    status['compute_monitor'] = managed_monitor.close()
                    if not status['compute_monitor']['query_worker_reaped']:
                        raise RuntimeError('managed NVML query worker not reaped')
                except Exception as error:
                    status['status'] = 'ENV_ABORT'
                    status['monitor_cleanup_error'] = repr(error)
                    cleanup_failed = True
            status['finished'] = stamp()
            if managed_monitor is None:
                (out / 'exit.json').write_text(json.dumps(status, indent=2) + '\n')
            if acquired:
                lock.seek(0)
                lock.truncate()
                state = 'external_cleanup_pending' if lease_fd is not None else 'released'
                lock.write(json.dumps(dict(stamp(), state=state,
                                           output=str(out))) + '\n')
                lock.flush()
                record('lease_guard_finished' if lease_fd is not None else
                       'lease_released', status=status['status'])
            if managed_monitor is not None:
                # Close/last-event I/O can introduce the first history gap.
                # Freeze the receipt after those attempts, still under flock.
                status['compute_monitor'] = managed_monitor.snapshot()
                if status['status'] == 'COMPLETED' and managed_monitor.had_gaps:
                    status['status'] = 'COMPLETED_WITH_MONITOR_GAPS'
                (out / 'exit.json').write_text(json.dumps(status, indent=2) + '\n')
            if cleanup_failed:
                return 75


def main():
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('guard interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compute-backend', choices=['nvidia-smi', 'nvml'],
                        default=DEFAULT_COMPUTE_BACKEND,
                        help='explicit compute PID query backend; no fallback')
    parser.add_argument('--lease-fd', type=int,
                        help='inherited exclusive lease; external owner handles recovery')
    parser.add_argument('--inherit-lease', action='store_true',
                        help='pass the held lease to an owned managed phase')
    parser.add_argument('--inherit-fd', type=int, action='append', default=[],
                        help='explicit bounded descriptor handoff to the managed child')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a command after -- is required')
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ('build', '.q4t-work')):
        parser.error('output must be under build/ or .q4t-work/')
    result = run_guard(command, out, common_lock(), lease_fd=args.lease_fd,
                       compute_backend=args.compute_backend,
                       inherit_lease=args.inherit_lease, inherited_fds=args.inherit_fd)
    # A cleanup failure in finally must override an otherwise successful return.
    if json.loads((out / 'exit.json').read_text())['status'] == 'ENV_ABORT':
        result = 75
    raise SystemExit(result)


if __name__ == '__main__':
    main()
