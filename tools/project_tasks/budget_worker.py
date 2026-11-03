"""Real application admission boundaries; never launches a service."""
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import engine, store, transport
from tools.project_tasks.live_worker import QUESTION


def boundary(case, output):
    def checkpoint(kind, number, ledger):
        if kind != "preflight_result":
            return
        # Even a broken negative fixture must not spend extra model attempts.
        if case == "negative":
            raise RuntimeError("over-budget task unexpectedly passed preflight")
        store.write_new(output / "boundary.json", store.encode(dict(
            pid=os.getpid(), number=number, kind=kind, task_id=ledger.meta["task_id"])))
        threading.Event().wait()
    return checkpoint


def main():
    os.umask(0o077)
    root, profile, case, output = sys.argv[1:]
    store.require(case in ("alpha", "negative"), "unknown boundary case")
    key = "same-key" if case == "alpha" else "above-limit"
    value, binding = transport.profile(profile)
    spec = engine.specification(binding, QUESTION, max_requests=2, max_tools=1, max_tokens=512)
    name = store.task_name(binding["client_id"], key)
    _, created = store.create(root, name, spec)
    store.require(created, "initial boundary task already exists")
    with store.open_task(root, name) as ledger:
        result = engine.run(ledger, binding, transport.Client(value), checkpoint=boundary(case, Path(output)))
    store.require(case == "negative" and result["state"] == "PREFLIGHT_REJECTED", "fault boundary not reached")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
