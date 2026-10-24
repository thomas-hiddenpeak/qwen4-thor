"""One bounded native CPU check of exiting-task I/O observation.

Exactly 32 one-thread and 32 eight-thread children are attempted on success.
No model, CUDA, sensor, foreign-process scan, or retry/coverage extension.
"""
import argparse
from collections import defaultdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
MONITOR = ROOT / 'tools/mtp_suffix_zero_hit/telemetry.py'
SPEC = importlib.util.spec_from_file_location('native_exit_pending_monitor', MONITOR)
telemetry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(telemetry)
THREAD_COUNTS = [1] * 32 + [8] * 32
MAX_CHILDREN = 64
TIMEOUT_SECONDS = 60
CLEANUP_RESERVE_SECONDS = 5


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def child_main(threads, ready_write, go_read):
    """Create the fixed thread group, then leave only after the parent's go."""
    try:
        gate = threading.Barrier(threads)
        never = threading.Event()
        def worker():
            gate.wait()
            never.wait()
        for _ in range(threads - 1):
            threading.Thread(target=worker, daemon=True).start()
        gate.wait()
        os.write(ready_write, b'R')
        if os.read(go_read, 1) != b'G':
            os._exit(3)
        os._exit(0)
    except BaseException:
        os._exit(2)


def before_deadline(deadline, message):
    if time.monotonic() >= deadline:
        raise TimeoutError(message)


def await_ready(fd, deadline):
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP)
    while True:
        before_deadline(deadline, 'native child ready budget exhausted')
        events = poller.poll(min(50, max(0, int((deadline-time.monotonic()) * 1000))))
        if not events:
            continue
        if any(number != fd or flags & (select.POLLERR | select.POLLNVAL)
               for number, flags in events):
            raise RuntimeError('invalid owned child ready pipe events: ' + repr(events))
        if os.read(fd, 1) != b'R':
            raise RuntimeError('owned child did not send ready')
        return


def run_child(index, threads, deadline, cleanup_deadline):
    row = dict(index=index, requested_threads=threads, started_monotonic=time.monotonic(),
               pending_observed=False, read_attempts=0, reaped=False, cleanup_errors=[])
    ready_read, ready_write = os.pipe()
    go_read, go_write = os.pipe()
    pipes = {ready_read, ready_write, go_read, go_write}
    pid = pidfd = procfd = None
    manager = telemetry._ExitPending()
    counts = defaultdict(int)
    resolved = []
    status = None
    try:
        before_deadline(deadline, 'native fixed work budget exhausted')
        pid = os.fork()
        if pid == 0:
            os.close(ready_read)
            os.close(go_write)
            child_main(threads, ready_write, go_read)
            os._exit(2)
        row['pid'] = pid
        for fd in (ready_write, go_read):
            os.close(fd)
            pipes.remove(fd)
        await_ready(ready_read, deadline)
        row['ready_monotonic'] = time.monotonic()
        # The still-blocked child cannot exit normally before go. Production
        # _read_task performs the same-pidfd nonterminal binding after stat.
        pidfd = os.pidfd_open(pid)
        procfd = os.open(f'/proc/{pid}', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        context = dict(pid=pid)
        raw = telemetry._read_at(procfd, 'stat', counts, context)
        identity = telemetry._stat_identity(raw)
        row['start_ticks'] = identity['start_ticks']
        context['expected_start_ticks'] = identity['start_ticks']
        budget = dict(remaining_ns=100_000_000, spent_ns=0)
        pre = telemetry._read_task(procfd, counts, identity['start_ticks'], with_io=True,
            context=context, exit_fd=pidfd, exit_budget=budget, pending=manager)
        row['pre_go'] = pre
        if pre.get('observation') != 'OK' or pre.get('pidfd_binding', {}).get('ready'):
            raise RuntimeError('pre-go native identity/read was not live and valid')
        fields = dict(line.split(':', 1) for line in pre['status'].splitlines() if ':' in line)
        row['observed_threads_before_go'] = int(fields['Threads'])
        if row['observed_threads_before_go'] != threads:
            raise RuntimeError('native child thread count differs from fixed scenario')
        row['go_monotonic'] = time.monotonic()
        os.write(go_write, b'G')
        os.close(go_write)
        pipes.remove(go_write)
        # Observe actual production decisions, retaining the final OK row and
        # the first non-OK result. No repeated child is created to seek coverage.
        while True:
            before_deadline(deadline, 'native exit observation budget exhausted')
            current = telemetry._read_task(procfd, counts, identity['start_ticks'], with_io=True,
                context=context, exit_fd=pidfd, exit_budget=budget, pending=manager)
            row['read_attempts'] += 1
            row['last_read'] = current
            resolved.extend(manager.resolve(counts))
            if current.get('observation') != 'OK':
                row['first_non_ok'] = current
                if current.get('observation') not in (
                        'EXITING_IO_UNAVAILABLE', 'EXITED_DURING_IO_READ',
                        'TERMINATED_AFTER_IO_DENIAL', 'VANISHED_DURING_READ'):
                    raise RuntimeError('unexpected native identity/read observation')
                row['pending_observed'] = current.get('observation') == 'EXITING_IO_UNAVAILABLE'
                break
            terminal = telemetry._poll_exit_fd(pidfd, 0)
            if terminal['ready']:
                row['exit_readiness'] = terminal
                break
        # Wait only for this already-created child. Manager confirmation uses
        # poll(0); a missing I/O field is never retried or zero-filled.
        while True:
            before_deadline(deadline, 'native child reap/confirmation budget exhausted')
            resolved.extend(manager.resolve(counts))
            got, current_status = os.waitpid(pid, os.WNOHANG)
            if got == pid:
                status = current_status
                row['reaped'] = True
                break
            time.sleep(.001)
        resolved.extend(manager.resolve(counts))
        row['exit_readiness_after_reap'] = telemetry._poll_exit_fd(pidfd, 0)
        if not row['exit_readiness_after_reap']['ready']:
            raise RuntimeError('bound native pidfd is not terminal after waitpid')
        row['resolved_events'] = resolved
        row['pending_after_reap'] = manager.snapshot()
        if manager.entries:
            raise RuntimeError('pending task remains after owned child waitpid')
        if row['pending_observed'] and not resolved:
            raise RuntimeError('native pending admission has no production resolved event')
        row['returncode'] = os.waitstatus_to_exitcode(status)
        if row['returncode'] != 0:
            raise RuntimeError('owned native child failed')
        row['completed'] = True
    except BaseException as error:
        row.update(completed=False, failure=repr(error), traceback=traceback.format_exc(),
                   telemetry_context=getattr(error, 'telemetry_context', None),
                   telemetry_task_state=getattr(error, 'telemetry_task_state', None))
    finally:
        if pid not in (None, 0) and not row['reaped']:
            try:
                # This is our unreaped direct child, so its numerical PID
                # cannot be recycled. Prefer the already-bound kernel handle.
                if pidfd is not None:
                    try:
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                row['forced_cleanup'] = True
                while True:
                    got, status = os.waitpid(pid, os.WNOHANG)
                    if got == pid:
                        row['reaped'] = True
                        row['cleanup_returncode'] = os.waitstatus_to_exitcode(status)
                        break
                    before_deadline(cleanup_deadline, 'owned native child cleanup exceeded 60 seconds')
                    time.sleep(.001)
            except BaseException as error:
                row['cleanup_errors'].append(repr(error))
        try:
            row['pending_before_close'] = manager.snapshot()
            manager.close()
        except BaseException as error:
            row['cleanup_errors'].append('pending close: ' + repr(error))
        for fd in list(pipes) + [procfd, pidfd]:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as error:
                    row['cleanup_errors'].append('owned FD close: ' + repr(error))
        row['read_counts'] = dict(counts)
        row['exit_wait_budget'] = locals().get('budget')
        row['finished_monotonic'] = time.monotonic()
    return row


def main():
    def interrupted(signum, _frame):
        raise KeyboardInterrupt('native owned check interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    rows_path = output.with_name(output.stem + '-rows.jsonl')
    if output.exists() or rows_path.exists() or not output.parent.is_dir():
        raise ValueError('output paths must be new files in an existing directory')
    started = time.monotonic()
    deadline = started + TIMEOUT_SECONDS
    work_deadline = deadline - CLEANUP_RESERVE_SECONDS
    monitor_sha = sha(MONITOR)
    proof = dict(schema=1, tool_sha256=sha(Path(__file__)), monitor_sha256=monitor_sha,
        max_children=MAX_CHILDREN, timeout_seconds=TIMEOUT_SECONDS,
        cleanup_reserve_seconds=CLEANUP_RESERVE_SECONDS, thread_counts=THREAD_COUNTS,
        completed_children=0, children_started=0, child_counts_by_threads={'1': 0, '8': 0},
        coverage_reproduced=False, cleanup_complete=True, all_owned_reaped=True,
        failures=[], rows=str(rows_path), started_monotonic=started,
        scope='Native production monitor decisions for fixed CPU children only; no CUDA, sensors, HTTP, foreign process inspection, or coverage retries.')
    try:
        with rows_path.open('x') as stream:
            for index, threads in enumerate(THREAD_COUNTS):
                before_deadline(work_deadline, 'native fixed work budget exhausted')
                row = run_child(index, threads, work_deadline, deadline)
                proof['children_started'] += int(row.get('pid') is not None)
                proof['completed_children'] += int(row.get('completed', False))
                proof['child_counts_by_threads'][str(threads)] += int(row.get('completed', False))
                proof['coverage_reproduced'] |= row['pending_observed']
                proof['all_owned_reaped'] &= row['reaped'] or row.get('pid') is None
                proof['cleanup_complete'] &= not row['cleanup_errors'] and proof['all_owned_reaped']
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                stream.flush()
                if not row.get('completed') or row['cleanup_errors']:
                    raise RuntimeError('native child observation/cleanup failed at fixed index ' + str(index))
        if sha(MONITOR) != monitor_sha:
            raise RuntimeError('production monitor identity changed during native run')
    except BaseException as error:
        proof['failures'].append(dict(error=repr(error), traceback=traceback.format_exc()))
    finally:
        proof['elapsed_seconds'] = time.monotonic() - started
        proof['rows_sha256'] = sha(rows_path) if rows_path.exists() else None
        proof['passed'] = (proof['completed_children'] == MAX_CHILDREN and
            proof['elapsed_seconds'] <= TIMEOUT_SECONDS and not proof['failures'] and
            proof['cleanup_complete'] and proof['all_owned_reaped'])
        with output.open('x') as stream:
            json.dump(proof, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        print(json.dumps(dict(output=str(output), passed=proof['passed'],
            completed_children=proof['completed_children'],
            coverage_reproduced=proof['coverage_reproduced'], sha256=sha(output))))
    return 0 if proof['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
