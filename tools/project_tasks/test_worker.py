"""CPU-only real child; blocks at a selected durable boundary until killed."""
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks.test_tasks import FakeClient, CountedRecords, BINDING
from tools.project_tasks import engine, store


def main():
    os.umask(0o077)
    root, name, project, target, marker = sys.argv[1:6]
    def checkpoint(kind, number, ledger):
        if kind == target:
            store.write_new(Path(marker), store.encode(dict(pid=os.getpid(), kind=kind, number=number)))
            threading.Event().wait()
    with store.open_task(root, name) as ledger:
        if len(sys.argv) == 7:
            ledger.db.execute("BEGIN IMMEDIATE")
            ledger.db.execute("INSERT INTO events VALUES (1,'not_committed','{}','x','y')")
            checkpoint("transaction", 1, ledger)
        result = engine.run(ledger, BINDING, FakeClient(), records=CountedRecords(project), checkpoint=checkpoint)
        store.write_new(Path(marker), store.encode(dict(pid=os.getpid(), result=result)))


if __name__ == "__main__":
    main()
