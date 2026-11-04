"""Explicit CPU/live file-operation crash worker; no HTTP or model execution."""
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import backup as b, store


def boundary(marker, target):
    store.require(target in ("copy_chunk", "file_copied", "before_seal", "snapshot_sealed", "placement_before_commit",
        "placement_committed", "candidate_ready", "fence_before_publish", "source_fenced", "destination_published"), "unknown backup fault boundary")
    def checkpoint(kind, value):
        if kind != target: return
        store.write_new(marker, store.encode(dict(pid=os.getpid(), kind=kind, value=value)))
        while True: time.sleep(.1)
    return checkpoint


if __name__ == "__main__":
    os.umask(0o077); signal.alarm(45)
    action, source, output_or_digest, marker, point = sys.argv[1:]
    hook = boundary(Path(marker), point)
    if action == "backup": b.snapshot(Path(source), Path(output_or_digest), checkpoint=hook)
    elif action == "restore": b.restore_apply(Path(source), output_or_digest, checkpoint=hook)
    else: raise ValueError("worker action")
