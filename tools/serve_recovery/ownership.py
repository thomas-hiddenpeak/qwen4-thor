"""Ancestry/identity observations and owned cleanup, independent of NVML."""
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tools/evalscope'))
import gpu_exclusive as guard
from tools.serve_recovery import journal, ledger


def descendants(records, root_pid, root_start):
    ledger.require(records.get(root_pid, {}).get('start_ticks') == root_start,
                   'owned ancestry root changed')
    owned = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, record in records.items():
            parent = record.get('ppid')
            if pid in owned or parent not in owned:
                continue
            if record.get('start_ticks', -1) >= records[parent]['start_ticks']:
                owned.add(pid)
                changed = True
    ledger.require(len(owned) <= 4096, 'owned descendant observation bound')
    return owned


def snapshot():
    records = guard.processes()
    ledger.require(len(records) <= 65536, 'process observation bound')
    return records


def observe(root_pid, root_start, pids):
    records = snapshot()
    owned = descendants(records, root_pid, root_start)
    compute = set() if pids is None else set(pids)
    # UNKNOWN is preserved by the caller; an empty set here only skips an
    # unavailable source. It is never cold-start evidence of an empty GPU.
    return dict(conflicts=sorted(pid for pid in records if pid not in owned and
                (pid in compute or guard.gpu_command(records[pid]))),
                unconfirmed_compute=sorted(compute-records.keys()))


def cleanup(tick, *, exclude=(), term_seconds=30, kill_seconds=5):
    parent, start = os.getpid(), journal.process_start(os.getpid())
    excluded = set(exclude)
    began = time.monotonic()
    signalled = {}
    while time.monotonic()-began <= term_seconds+kill_seconds:
        tick()
        records = snapshot()
        owned = descendants(records, parent, start)-{parent}-excluded
        if not owned:
            return True
        for pid in owned:
            record = records[pid]
            sig = signal.SIGKILL if time.monotonic()-began >= term_seconds else signal.SIGTERM
            key = (pid, record['start_ticks'])
            try:
                fd = os.pidfd_open(pid)
                try:
                    if journal.process_start(pid) != record['start_ticks']:
                        continue
                    if signalled.get(key) != sig:
                        signal.pidfd_send_signal(fd, sig)
                        signalled[key] = sig
                    try:
                        os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG)
                    except ChildProcessError:
                        pass  # It will become waitable after adoption.
                finally:
                    os.close(fd)
            except (ProcessLookupError, FileNotFoundError):
                pass
        time.sleep(0.025)
    return False
