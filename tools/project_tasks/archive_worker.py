"""Explicit retention operation fault boundary; never opens an HTTP client."""
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import retention as r, store

BOUNDARIES = ("metadata_before_commit", "operation_started", "payload_moved",
              "payload_file_removed", "payload_removed", "key_published",
              "operation_finished")


def boundary(output, target):
    store.require(target in BOUNDARIES, "unknown retention checkpoint")
    def checkpoint(kind, operation):
        if kind != target:
            return
        store.write_new(output, store.encode(dict(pid=os.getpid(), kind=kind,
            operation_sha256=store.digest(operation), plan_sha256=operation["plan_sha256"])))
        while True:
            time.sleep(.1)
    return checkpoint


if __name__ == "__main__":
    os.umask(0o077)
    root, key, plan, checksum, marker, target = sys.argv[1:]
    signal.alarm(45)
    value = r.read(Path(plan))
    r.apply(Path(root), value["binding"], key, Path(plan), checksum,
            checkpoint=boundary(Path(marker), target))
