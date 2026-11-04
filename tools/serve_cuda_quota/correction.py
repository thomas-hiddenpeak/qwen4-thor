"""Keep short CUDA children waitable until the guard has observed their identity."""
import argparse
import ctypes
import fcntl
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_cuda_quota import stage as s

BASE = s.BASE
OUT = BASE/'execution-v2'
LEASE = BASE/'lease-v2'
SOURCES = ('tools/serve_cuda_quota/correction.py',
           'tools/serve_cuda_quota/test_correction.py',
           'docs/SERVE_CUDA_QUOTA_CORRECTION_2026-11-04.md')


def proc(pid):
    data = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return dict(pid=pid, state=data[0], ppid=int(data[1]), pgid=int(data[2]),
                start_ticks=int(data[19]))


def wait_without_reaping(process, timeout):
    fd = os.pidfd_open(process.pid)
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            # The descriptor still refers to this exact child, never a reused PID.
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            process.wait(timeout=5)
            raise TimeoutError('owned child exceeded deadline')
        waited = os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOWAIT)
        s.require(waited.si_pid == process.pid, 'waitid child mismatch')
        code = waited.si_status if waited.si_code == os.CLD_EXITED else -waited.si_status
        s.require(proc(process.pid)['state'] == 'Z', 'child must remain waitable')
        return code
    finally:
        os.close(fd)


def complete_lines(path):
    raw = Path(path).read_text()
    lines = raw.split('\n')
    # The final append may be in progress; do not use an uncommitted partial row.
    return [json.loads(line) for line in lines[:-1] if line]


def acknowledgement(monitors, queries, latest_start):
    for monitor in reversed(monitors):
        if monitor.get('event') != 'monitor':
            continue
        if monitor.get('conflicts') or monitor.get('unreadable_processes'):
            raise RuntimeError('guard conflict/unreadable process')
        for query in reversed(queries):
            if query.get('status') != 'OK':
                raise RuntimeError('guard query failure')
            rows = [json.loads(row) for row in query['stdout'].splitlines()]
            if not rows:
                continue
            begin, end = rows[0]['monotonic'], rows[-1]['monotonic']
            if (rows[0]['event'] == 'begin' and rows[-1]['event'] == 'result' and
                    latest_start <= begin <= end <= monitor['monotonic']):
                return dict(monitor=monitor, query_begin=begin, query_end=end,
                            request_id=query['request_id'])
    return None


def validate_zombies(started, current, parent, group):
    s.require(len(started) == len(current) == 4, 'four retained child identities')
    for before, now in zip(started, current):
        s.require(now['state'] == 'Z' and now['pid'] == before['pid'] and
            now['start_ticks'] == before['start_ticks'] and
            now['ppid'] == parent and now['pgid'] == group,
            'retained child identity mismatch')


def source_hashes():
    return {name:s.sha(ROOT/name) for name in SOURCES}


def cpu():
    folder = BASE/'correction-cpu'
    folder.mkdir()
    original = source_hashes()
    for name in SOURCES:
        path = folder/'source'/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT/name).read_bytes())
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromName('tools.serve_cuda_quota.test_correction'))
    (folder/'tests.log').write_text(stream.getvalue())
    s.save(folder/'result.json', dict(passed=result.wasSuccessful(), count=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), sources=original,
        model_gpu_http_started=False))
    s.require(result.wasSuccessful() and result.testsRun == 6 and
              original == source_hashes(), 'correction CPU gate')
    print(stream.getvalue(), flush=True)


def prepare():
    plan = s.check()
    original = s.read(BASE/'lease/exit.json')
    s.require(original['status'] == 'ENV_ABORT' and
        original['reason'] == 'competing workload during lease' and
        s.read(BASE/'execution/result.json')['passed'], 'preserve original failure')
    cpu_result = s.read(BASE/'correction-cpu/result.json')
    s.require(cpu_result['passed'] and cpu_result['count'] == 6 and
              cpu_result['sources'] == source_hashes(), 'correction CPU identity')
    s.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit reviewed correction before freezing')
    s.save(BASE/'correction-plan.json', dict(
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        original_plan=s.identity(BASE/'plan.json'), original_guard=s.identity(BASE/'lease/exit.json'),
        original_result=s.identity(BASE/'execution/result.json'), sources=source_hashes(),
        probe=plan['probe'], modes=list(s.MODES), per_probe_seconds=15,
        driver_seconds=90, acknowledgement_seconds=8, total_probe_budget=8,
        cpu_result=s.identity(BASE/'correction-cpu/result.json')))


def check():
    original = s.check()
    plan = s.read(BASE/'correction-plan.json')
    s.require(plan['sources'] == source_hashes() and plan['probe'] == original['probe'] and
        plan['modes'] == list(s.MODES) and plan['total_probe_budget'] == 8 and
        plan['per_probe_seconds'] == 15 and plan['driver_seconds'] == 90 and
        plan['acknowledgement_seconds'] == 8, 'correction scope/identity drift')
    for key in ('original_plan', 'original_guard', 'original_result', 'cpu_result'):
        s.require(s.identity(plan[key]['path']) == plan[key], 'original evidence drift')
    return plan


def execute():
    plan = check()
    OUT.mkdir()
    started_at = time.monotonic()
    parent = os.getppid()
    s.require(b'tools/evalscope/gpu_exclusive.py' in
        Path(f'/proc/{parent}/cmdline').read_bytes(), 'production GPU guard parent')
    libc = ctypes.CDLL(None, use_errno=True)
    s.require(libc.prctl(1, signal.SIGKILL, 0, 0, 0) == 0 and os.getppid() == parent,
              'driver parent-death binding')
    original_plan = s.read(BASE/'plan.json')
    lock = Path(original_plan['lock']['path'])
    with lock.open('rb') as stream:
        st = os.fstat(stream.fileno())
        s.require(st.st_dev == original_plan['lock']['device'] and
                  st.st_ino == original_plan['lock']['inode'], 'original lock inode')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise RuntimeError('exclusive GPU lease absent')
    s.require(s.read(lock)['pid'] == parent, 'lease parent identity')
    s.save(OUT/'begin.json', dict(monotonic=started_at, pid=os.getpid(), pgid=os.getpgrp(),
        parent=parent, plan_sha256=s.sha(BASE/'correction-plan.json')))
    children, identities, completed = [], [], []
    try:
        for mode in s.MODES:
            s.require(time.monotonic()-started_at < 90, 'driver deadline')
            folder = OUT/mode
            folder.mkdir()
            argv = [plan['probe']['path'], mode, str(os.getpid())]
            before = time.monotonic()
            with (folder/'stdout.jsonl').open('xb') as stdout, \
                 (folder/'stderr.log').open('xb') as stderr:
                process = subprocess.Popen(argv, cwd=ROOT, stdout=stdout, stderr=stderr)
                children.append(process)  # Keep Popen alive; do not poll()/wait() yet.
                info = dict(proc(process.pid), argv=argv, monotonic=before,
                            registered_monotonic=time.monotonic())
                identities.append(info)
                s.save(folder/'started.json', info)
                code = wait_without_reaping(process, 15)
            elapsed = time.monotonic()-before
            s.save(folder/'exit-waitable.json', dict(returncode=code, elapsed_seconds=elapsed,
                                                    identity=proc(process.pid)))
            s.require(code == 0 and elapsed <= 15, 'native probe failure: '+mode)
            result = s.parse_probe(mode, (folder/'stdout.jsonl').read_text())
            s.save(folder/'result.json', result)
            completed.append(result)
        deadline = time.monotonic()+8
        ack = None
        while time.monotonic() < deadline:
            validate_zombies(identities, [proc(child.pid) for child in children],
                             os.getpid(), os.getpgrp())
            ack = acknowledgement(complete_lines(LEASE/'events.jsonl'),
                                  complete_lines(LEASE/'nvml-queries.jsonl'),
                                  identities[-1]['registered_monotonic'])
            if ack:
                break
            time.sleep(0.05)
        s.require(ack is not None, 'guard did not observe retained identities')
        s.save(OUT/'guard-acknowledgement.json', dict(ack, retained=[proc(p.pid) for p in children],
            explanation='all child /proc identities existed throughout the later NVML query and guard scan'))
        for mode, child in zip(s.MODES, children):
            s.require(child.wait(timeout=1) == 0, 'reap result')
            s.require(not Path(f'/proc/{child.pid}').exists(), 'child reaping incomplete')
            s.save(OUT/mode/'reaped.json', dict(pid=child.pid, returncode=child.returncode,
                                              monotonic=time.monotonic()))
        check()
        s.require(time.monotonic()-started_at < 90, 'driver deadline')
        s.save(OUT/'result.json', dict(passed=True, probes=completed,
            elapsed_seconds=time.monotonic()-started_at, all_children_reaped=True,
            original_env_abort_preserved=True, model_starts=0, http_requests=0,
            serve_integrated=False, physical_ram_hard_limit=False))
    except BaseException as error:
        s.save(OUT/'failed.json', dict(error=repr(error), completed=completed,
                                    elapsed_seconds=time.monotonic()-started_at))
        raise
    finally:
        # On an exceptional path the parent-death binding / outer guard owns
        # still-running cleanup. Reap only already exited children here.
        for child in children:
            child.poll()
    print('CORRECTED_CUDA_PROBES_PASS', len(completed), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['cpu','prepare','check','execute'])
    args = parser.parse_args()
    globals()[args.action]()
