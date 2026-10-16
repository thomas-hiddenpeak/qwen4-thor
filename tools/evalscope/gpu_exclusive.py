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
            argv = (directory / 'cmdline').read_bytes().split(b'\0')
            argv = [v.decode(errors='replace') for v in argv if v]
            result[int(directory.name)] = dict(
                ppid=int(fields[1]), pgid=int(fields[2]), start_ticks=int(fields[19]),
                argv=argv, executable=os.readlink(directory / 'exe'))
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


def inspect(owner_pgid=None, owned_history=None):
    compute = nvidia_pids()  # Errors fail closed; never interpreted as idle.
    # Read process groups after the driver query so newly created owned GPU
    # children are not mistaken for unrelated processes from a stale snapshot.
    records = processes()
    # Keep observed process identities, not a PID-only allowlist. A live,
    # reused or unreadable PID is never exempted based on history.
    history = owned_history if owned_history is not None else {}
    for pid, record in records.items():
        if owner_pgid is not None and record.get('pgid') == owner_pgid:
            history[pid] = record.copy()
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
    retired_pids = {r['pid'] for r in retired}
    conflicts = []
    for pid in records.keys() | compute:
        if pid in retired_pids:
            continue
        record = records.get(pid, {})
        if owner_pgid is not None and record.get('pgid') == owner_pgid:
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


def stop_owned(child, grace):
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
    # Reparented descendants may outlive the runner; retain the lease until gone.
    deadline = time.monotonic() + grace
    while True:
        remaining = [pid for pid, r in processes().items()
                     if r.get('pgid') == child.pid]
        if not remaining:
            return
        if time.monotonic() >= deadline:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise RuntimeError(f'owned process group did not drain: {remaining}')
        time.sleep(0.1)


def run_guard(command, out, lock_path, interval=1.0, grace=30.0):
    out.mkdir(parents=True, exist_ok=False)
    status = {'status': 'INITIALIZING', 'command': command,
              'lock_path': str(lock_path), 'poll_interval_seconds': interval,
              'poll_scope': 'cooperative flock plus /proc and NVIDIA compute polling',
              'performance_acceptance': False, 'child_returncode': None}
    child = None
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
            if child is not None:
                try:
                    stop_owned(child, grace)
                except Exception as error:
                    status['status'] = 'ENV_ABORT'
                    status['cleanup_error'] = repr(error)
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
