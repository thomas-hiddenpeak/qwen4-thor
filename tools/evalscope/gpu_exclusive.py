"""Hold the repository-wide GPU lease and monitor noncooperating workloads.

The flock prevents cooperating sessions from overlapping. Process/NVIDIA polls
are supplementary and cannot prove absence of sub-poll or invisible workloads.
An environment abort never kills an unrelated process or implies a model failure.
"""
import argparse
import csv
import datetime
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


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


def inspect(owner_pgid=None, owned_history=None):
    compute = nvidia_pids()  # Errors fail closed; never interpreted as idle.
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
        refreshed_compute = nvidia_pids()
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


def run_guard(command, out, lock_path, interval=1.0, grace=30.0):
    out.mkdir(parents=True, exist_ok=False)
    status = {'status': 'INITIALIZING', 'command': command,
              'lock_path': str(lock_path), 'poll_interval_seconds': interval,
              'poll_scope': 'cooperative flock plus /proc and NVIDIA compute polling',
              'performance_acceptance': False, 'child_returncode': None}
    child = None
    owned_history = {}
    acquired = False
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+') as lock, (out / 'events.jsonl').open('w') as events:
        def record(event, **values):
            events.write(json.dumps(dict(stamp(), event=event, **values)) + '\n')
            events.flush()
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                status['status'] = 'LEASE_BUSY'
                lock.seek(0)
                status['holder_snapshot'] = lock.read()
                return 75
            acquired = True
            lease = dict(stamp(), command=command, output=str(out), state='held')
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps(lease) + '\n')
            lock.flush()
            record('lease_acquired')
            snapshot = inspect()
            record('preflight', **snapshot)
            if snapshot['conflicts']:
                status['status'] = 'ENV_ABORT'
                status['reason'] = 'competing workload before launch'
                return 75
            child = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
            record('child_started', child_pid=child.pid)
            owned_history = {}
            while True:
                snapshot = inspect(child.pid, owned_history)
                record('monitor', **snapshot)
                if snapshot['conflicts']:
                    status['status'] = 'ENV_ABORT'
                    status['reason'] = 'competing workload during lease'
                    return 75
                code = child.poll()
                if code is not None:
                    status['child_returncode'] = code
                    status['status'] = 'COMPLETED' if code == 0 else 'COMMAND_FAILED'
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
            status['finished'] = stamp()
            (out / 'exit.json').write_text(json.dumps(status, indent=2) + '\n')
            if acquired:
                lock.seek(0)
                lock.truncate()
                lock.write(json.dumps(dict(stamp(), state='released',
                                           output=str(out))) + '\n')
                lock.flush()
                record('lease_released', status=status['status'])
            if cleanup_failed:
                return 75


def main():
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('guard interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a command after -- is required')
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ('build', '.q4t-work')):
        parser.error('output must be under build/ or .q4t-work/')
    result = run_guard(command, out, common_lock())
    # A cleanup failure in finally must override an otherwise successful return.
    if json.loads((out / 'exit.json').read_text())['status'] == 'ENV_ABORT':
        result = 75
    raise SystemExit(result)


if __name__ == '__main__':
    main()
