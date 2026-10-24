#!/usr/bin/env python3
"""Bounded CPU-only observation of an owned child's /proc/io exit race.

Fixed pass: 64 same-UID children, alternating one thread and eight total
threads (32 each), each exiting 5 ms after a parent handshake. No imports of
monitor runners and no model, HTTP, CUDA, NVML, sensors, or foreign PID reads.
The first io EACCES preserves every pre-error field plus immediate rechecks,
then uses the already-open pidfd to witness this exact child's exit. A live
state alone never proves exit. Every child is reaped, even after interruption.

This is a reproduction tool, not production telemetry logic or a performance
test. An absent R/S -> io EACCES -> live recheck -> exit window is explicitly
NOT_REPRODUCED; the fixed pass never retries or changes parameters to find it.
"""
import argparse
from collections import Counter
import errno
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import threading
import time
import traceback


ROOT = Path(__file__).resolve().parents[2]
CHILDREN = 64
GLOBAL_SECONDS = 60.0
PER_CHILD_SECONDS = 0.9
EXIT_DELAY_SECONDS = 0.005
WITNESS_SECONDS = 0.05
MAX_WITNESS_READS = 128
MAX_FILE_BYTES = 65536
TOTAL_THREADS = 8
CLEANUP_SIGNALS = {signal.SIGINT, signal.SIGTERM}


def now_ns():
    return time.monotonic_ns()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def identity(raw):
    values = raw.rsplit(')', 1)[1].split()
    return dict(state=values[0], ppid=int(values[1]), start_ticks=int(values[19]))


def status_values(raw):
    result = {}
    for line in raw.splitlines():
        if line.startswith(('Uid:', 'Threads:', 'State:')):
            key, value = line.split(':', 1)
            result[key] = value.strip()
    return result


def read_at(directory_fd, name, counts):
    """Preserve operation, exact error and raw timestamps without re-opening PID."""
    counts[name] += 1
    record = dict(relative_file=name, started_monotonic_ns=now_ns())
    descriptor = None
    operation = 'open'
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC,
                             dir_fd=directory_fd)
        operation = 'read'
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, min(4096, MAX_FILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise RuntimeError('owned proc field exceeds fixed byte limit')
        record.update(status='OK', raw=b''.join(chunks).decode('utf-8', 'replace'),
                      bytes=size)
    except OSError as error:
        record.update(status='ERROR', operation=operation, errno=error.errno,
                      error=repr(error))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        record['finished_monotonic_ns'] = now_ns()
    if record['status'] == 'OK':
        if name == 'stat':
            record['identity'] = identity(record['raw'])
        elif name == 'status':
            record['selected_fields'] = status_values(record['raw'])
    return record


def poll_pidfd(pidfd, timeout_ms=0):
    started = now_ns()
    poller = select.poll()
    poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
    events = poller.poll(timeout_ms)
    require(all(fd == pidfd for fd, _ in events), 'unexpected poll descriptor')
    mask = 0
    for _, event in events:
        mask |= event
    require(not mask & (select.POLLERR | select.POLLNVAL),
            'owned pidfd poll failed: ' + str(mask))
    return dict(started_monotonic_ns=started, finished_monotonic_ns=now_ns(),
                timeout_ms=timeout_ms, event_mask=mask,
                exit_ready=bool(mask & select.POLLIN))


def check_same(record, start_ticks):
    if record['status'] == 'OK':
        require(record['identity']['start_ticks'] == start_ticks,
                'pinned proc identity changed')


def child_main(ready_write, go_read, threaded):
    """Child side owns no output; os._exit also ends the seven worker threads."""
    code = 0
    try:
        if threaded:
            barrier = threading.Barrier(TOTAL_THREADS)
            parked = threading.Event()

            def worker():
                barrier.wait()
                parked.wait()

            for _ in range(TOTAL_THREADS - 1):
                threading.Thread(target=worker, daemon=True).start()
            barrier.wait()
        os.write(ready_write, b'R')
        require(os.read(go_read, 1) == b'G', 'missing parent go handshake')
        time.sleep(EXIT_DELAY_SECONDS)
    except BaseException:
        code = 2
    os._exit(code)


def await_ready(fd, deadline_ns):
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    while now_ns() < deadline_ns:
        remaining_ms = max(1, min(20, (deadline_ns - now_ns()) // 1000000))
        for _, mask in poller.poll(remaining_ms):
            require(not mask & (select.POLLERR | select.POLLNVAL),
                    'child readiness pipe failed')
            require(os.read(fd, 1) == b'R', 'child exited before ready handshake')
            return
    raise TimeoutError('child readiness exceeded fixed per-child deadline')


def witness_after_denial(directory_fd, pidfd, start_ticks, counts, deadline_ns):
    """Immediate stat first mirrors the failed monitor's existing recheck."""
    after = read_at(directory_fd, 'stat', counts)
    check_same(after, start_ticks)
    first_poll = poll_pidfd(pidfd)
    status = read_at(directory_fd, 'status', counts)
    end = min(deadline_ns, now_ns() + int(WITNESS_SECONDS * 1e9))
    transitions = []
    last_state = after.get('identity', {}).get('state')
    last_poll = first_poll
    observations = 0
    final_stat = after
    while now_ns() < end and observations < MAX_WITNESS_READS:
        if last_poll['exit_ready'] and last_state in ('Z', 'X'):
            break
        current = read_at(directory_fd, 'stat', counts)
        check_same(current, start_ticks)
        observations += 1
        current_state = current.get('identity', {}).get('state')
        if current_state != last_state or current['status'] != 'OK':
            transitions.append(current)
        last_state, final_stat = current_state, current
        last_poll = poll_pidfd(pidfd)
    # No extra sampling or parameter change after the fixed read allowance.
    # One bounded pidfd wait may observe exact-child exit without proc races.
    if not last_poll['exit_ready'] and now_ns() < end:
        timeout_ms = max(0, (end - now_ns()) // 1000000)
        last_poll = poll_pidfd(pidfd, timeout_ms)
        final_stat = read_at(directory_fd, 'stat', counts)
        check_same(final_stat, start_ticks)
    return dict(stat_after_error=after, pidfd_immediately_after_stat=first_poll,
                status_after_error=status, transition_reads=observations,
                state_transitions=transitions, final_pidfd_poll=last_poll,
                final_stat=final_stat, witness_deadline_monotonic_ns=end,
                all_stat_reads_used_same_pinned_directory=True)


def own_child(index, global_deadline_ns):
    start = now_ns()
    deadline = min(start + int(PER_CHILD_SECONDS * 1e9), global_deadline_ns)
    threaded = index % 2 == 1
    result = dict(index=index + 1,
                  shape='eight_total_threads' if threaded else 'single_thread',
                  expected_threads=TOTAL_THREADS if threaded else 1,
                  started_monotonic_ns=start, deadline_monotonic_ns=deadline,
                  io_denial=None, cleanup={}, observation_error=None)
    counts = Counter()
    ready_read = ready_write = go_read = go_write = None
    directory_fd = pidfd = child = None
    reaped = False
    try:
        ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
        go_read, go_write = os.pipe2(os.O_CLOEXEC)
        # Do not allow interruption between fork returning a new owned PID
        # and recording it for finally cleanup.
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, CLEANUP_SIGNALS)
        try:
            child = os.fork()
            if child == 0:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
                    os.close(ready_read)
                    os.close(go_write)
                    child_main(ready_write, go_read, threaded)
                except BaseException:
                    os._exit(2)
            result['pid'] = child
        finally:
            if child != 0:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        os.close(ready_write)
        ready_write = None
        os.close(go_read)
        go_read = None
        # Open the pidfd before releasing the child, then pin /proc. An
        # unreaped direct child cannot be replaced by an unrelated reused PID.
        pidfd = os.pidfd_open(child, 0)
        directory_fd = os.open(f'/proc/{child}',
                                 os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        await_ready(ready_read, deadline)
        initial = read_at(directory_fd, 'stat', counts)
        initial_status = read_at(directory_fd, 'status', counts)
        require(initial['status'] == initial_status['status'] == 'OK',
                'initial pinned process fields unavailable')
        start_ticks = initial['identity']['start_ticks']
        require(initial['identity']['ppid'] == os.getpid(),
                'child is not directly owned by this process')
        require(os.fstat(directory_fd).st_uid == os.geteuid(),
                'child proc directory effective UID differs')
        uids = list(map(int, initial_status['selected_fields']['Uid'].split()))
        require(uids[:3] == list(os.getresuid()) and uids[3] == os.geteuid(),
                'child UID identity differs')
        require(int(initial_status['selected_fields']['Threads']) ==
                result['expected_threads'], 'child thread shape differs')
        result.update(start_ticks=start_ticks, initial_stat=initial,
                      initial_status=initial_status, uid_fields=uids,
                      pinned_directory_inode=os.fstat(directory_fd).st_ino,
                      pinned_directory_fd=directory_fd, pidfd=pidfd,
                      pidfd_opened_before_go=True)
        result['go_monotonic_ns'] = now_ns()
        os.write(go_write, b'G')
        os.close(go_write)
        go_write = None
        attempts = 0
        while now_ns() < deadline:
            attempts += 1
            before = read_at(directory_fd, 'stat', counts)
            check_same(before, start_ticks)
            status = read_at(directory_fd, 'status', counts)
            schedstat = read_at(directory_fd, 'schedstat', counts)
            require(all(record['status'] == 'OK'
                        for record in (before, status, schedstat)),
                    'non-io owned proc field failed')
            io_record = read_at(directory_fd, 'io', counts)
            if io_record['status'] == 'ERROR':
                require(io_record['errno'] == errno.EACCES,
                        'unexpected io errno: ' + str(io_record['errno']))
                denial = dict(attempt=attempts, stat_before_io=before,
                              status_before_io=status, schedstat=schedstat,
                              io_error=io_record)
                denial.update(witness_after_denial(directory_fd, pidfd,
                    start_ticks, counts, deadline))
                result['io_denial'] = denial
                break
            result['last_successful_io_snapshot'] = dict(
                attempt=attempts, stat_before_io=before, status_before_io=status,
                schedstat=schedstat, io=io_record)
            # Keep a zombie unreaped through the final io attempt. If this
            # kernel permits zombie io, report absence rather than fabricating
            # the expected permission failure.
            state = before.get('identity', {}).get('state')
            poll = poll_pidfd(pidfd)
            if state in ('Z', 'X') and poll['exit_ready']:
                result['readable_terminal_io'] = True
                break
        result['io_attempts'] = attempts
        result['end_observation_pidfd_poll'] = poll_pidfd(pidfd)
        result['observation_timed_out'] = now_ns() >= deadline
    except BaseException as error:
        result['observation_error'] = dict(type=type(error).__name__,
            error=repr(error), traceback=traceback.format_exc())
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            result['interrupted'] = True
    finally:
        # Cleanup affects only this unreaped direct child. Never enumerate or
        # signal foreign processes, even if the observation itself failed.
        cleanup_start = now_ns()
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, CLEANUP_SIGNALS)
        if child is not None and child > 0:
            try:
                if pidfd is None:
                    pidfd = os.pidfd_open(child, 0)
                poll = poll_pidfd(pidfd)
                result['cleanup']['pidfd_before_reap'] = poll
                if not poll['exit_ready']:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                    result['cleanup']['sent_owned_sigkill'] = True
                waited, wait_status = os.waitpid(child, 0)
                require(waited == child, 'waitpid returned another child')
                reaped = True
                result['cleanup'].update(reaped=True, waited_pid=waited,
                    wait_status=wait_status,
                    exit_code=os.waitstatus_to_exitcode(wait_status))
            except BaseException as error:
                result['cleanup']['error'] = dict(type=type(error).__name__,
                    error=repr(error), traceback=traceback.format_exc())
                # Reconfirm direct-child ownership without reaping before a
                # numeric-PID fallback. This single-threaded parent is its
                # only reaper; signals remain blocked through waitpid.
                if not reaped:
                    try:
                        exited = os.waitid(os.P_PID, child,
                            os.WEXITED | os.WNOHANG | os.WNOWAIT)
                        if exited is None:
                            try:
                                os.kill(child, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            result['cleanup']['fallback_direct_child_signal'] = True
                        while True:
                            try:
                                waited, wait_status = os.waitpid(child, 0)
                                reaped = True
                                require(waited == child, 'cleanup waitpid mismatch')
                                result['cleanup'].update(reaped=True,
                                    waited_pid=waited, wait_status=wait_status,
                                    exit_code=os.waitstatus_to_exitcode(wait_status))
                                break
                            except InterruptedError:
                                continue
                    except BaseException as cleanup_error:
                        result['cleanup']['fallback_error'] = repr(cleanup_error)
        for descriptor in (directory_fd, pidfd, ready_read, ready_write,
                           go_read, go_write):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as close_error:
                    result['cleanup'].setdefault('close_errors', []).append(repr(close_error))
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        except KeyboardInterrupt:
            result['interrupted'] = True
        result['cleanup']['started_monotonic_ns'] = cleanup_start
        result['cleanup']['finished_monotonic_ns'] = now_ns()
        result['file_read_attempts'] = dict(counts)
        result['finished_monotonic_ns'] = now_ns()
        result['elapsed_ns'] = result['finished_monotonic_ns'] - start
        result['per_child_deadline_exceeded'] = result['finished_monotonic_ns'] > deadline
    return result


def summarize(rows):
    values = Counter()
    for row in rows:
        values[row['shape']] += 1
        denial = row['io_denial']
        if not denial:
            continue
        values['io_eacces'] += 1
        before = denial['stat_before_io'].get('identity', {})
        after = denial['stat_after_error'].get('identity', {})
        final = denial['final_stat'].get('identity', {})
        same = (before.get('start_ticks') == after.get('start_ticks') ==
                final.get('start_ticks') == row['start_ticks'])
        if before.get('state') in ('R', 'S'):
            values['initial_live_io_eacces'] += 1
        if after.get('state') in ('R', 'S'):
            values['live_immediate_stat_recheck'] += 1
        terminal = final.get('state') in ('Z', 'X')
        ready = denial['final_pidfd_poll']['exit_ready']
        if same and terminal and ready:
            values['same_identity_terminal_pidfd_confirmed'] += 1
        if (same and before.get('state') in ('R', 'S') and
                after.get('state') in ('R', 'S') and terminal and ready):
            values['live_recheck_then_exact_child_exit'] += 1
    return dict(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.is_relative_to(ROOT / '.q4t-work') and not output.exists() and
            output.parent.is_dir(), 'use a new file in an existing .q4t-work directory')
    require(hasattr(os, 'fork') and hasattr(os, 'pidfd_open') and
            hasattr(signal, 'pidfd_send_signal') and hasattr(os, 'getresuid') and
            hasattr(signal, 'pthread_sigmask') and hasattr(os, 'waitid'),
            'Linux fork/pidfd/getresuid/signal-mask/waitid support is required')

    def interrupted(signum, _frame):
        raise KeyboardInterrupt('owned-child reproduction interrupted: ' + str(signum))

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    started = now_ns()
    deadline = started + int(GLOBAL_SECONDS * 1e9)
    rows, error = [], None
    result = dict(schema=1, tool_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        parameters=dict(max_children=CHILDREN, global_seconds=GLOBAL_SECONDS,
            per_child_seconds=PER_CHILD_SECONDS,
            exit_delay_seconds=EXIT_DELAY_SECONDS,
            witness_seconds=WITNESS_SECONDS, witness_read_limit=MAX_WITNESS_READS,
            alternating_shapes=['single_thread', 'eight_total_threads'],
            target_each_shape=CHILDREN // 2, total_threads=TOTAL_THREADS,
            max_file_bytes=MAX_FILE_BYTES, stop_on_first_reproduction=False),
        parent=dict(pid=os.getpid(), uid=os.getuid(), euid=os.geteuid(),
                    resuid=list(os.getresuid())),
        started_monotonic_ns=started, deadline_monotonic_ns=deadline,
        children=rows, no_gpu_http_model_or_foreign_process_access=True)
    try:
        for index in range(CHILDREN):
            # Reserve one child's bounded interval for observation plus
            # cleanup; do not spawn into an already exhausted wall budget.
            if deadline - now_ns() < int(1e9):
                result['stopped_for_global_budget'] = True
                break
            row = own_child(index, deadline)
            rows.append(row)
            if (row['observation_error'] is not None or row.get('interrupted') or
                    row['cleanup'].get('error') or
                    row['cleanup'].get('exit_code') != 0 or
                    row['per_child_deadline_exceeded']):
                result['stopped_for_child_failure'] = True
                break
    except BaseException as exception:
        error = dict(type=type(exception).__name__, error=repr(exception),
                     traceback=traceback.format_exc())
    finally:
        finished = now_ns()
        counts = summarize(rows)
        complete = (len(rows) == CHILDREN and error is None and
                    finished <= deadline and
                    all(r['observation_error'] is None and not r.get('interrupted') and
                        not r['cleanup'].get('error') and not r['cleanup'].get('close_errors') and
                        r['cleanup'].get('reaped') and
                        r['cleanup'].get('exit_code') == 0 and
                        not r['per_child_deadline_exceeded'] for r in rows))
        result.update(finished_monotonic_ns=finished, elapsed_ns=finished - started,
            global_deadline_exceeded=finished > deadline, complete_fixed_pass=complete,
            error=error, counts=counts,
            live_recheck_window=('REPRODUCED' if counts.get(
                'live_recheck_then_exact_child_exit', 0) else 'NOT_REPRODUCED'),
            any_io_eacces=counts.get('io_eacces', 0) > 0,
            all_created_children_reaped=all(r['cleanup'].get('reaped')
                                           for r in rows if 'pid' in r),
            limits=[
                'Sequential proc and pidfd observations are not an atomic kernel snapshot.',
                'An R/S state is not exit proof; the pinned start_ticks, terminal state, '
                'pidfd readiness and final direct-child wait status are retained separately.',
                'The controlled fixed-delay child resembles process teardown, not evalscope '
                'application behavior. Failure to reproduce is not proof the kernel window is absent.',
                'Cleanup may run past a deadline to reap an owned child; any overrun is reported '
                'and invalidates complete_fixed_pass. No new child is launched after budget stop.',
                'EACCES results here do not authorize ignoring live/unknown process permission errors.',
            ])
        with output.open('x') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    print(json.dumps(dict(output=str(output), complete=complete, children=len(rows),
                         counts=counts, live_recheck_window=result['live_recheck_window'])))
    return 0 if complete else 1


if __name__ == '__main__':
    raise SystemExit(main())
