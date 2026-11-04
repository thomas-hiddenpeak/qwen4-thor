"""Real project query boundaries for a whole-store handoff acceptance."""
from dataclasses import asdict
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


def boundary(case, output, hold=None):
    store.require(case in ("known", "unknown"), "unknown handoff case")
    point = "tool_result" if case == "known" else "request"
    def checkpoint(kind, number, ledger):
        if kind != point: return
        store.write_new(output / "boundary.json", store.encode(dict(pid=os.getpid(), kind=kind,
            number=number, task_id=ledger.meta["task_id"])))
        (hold or threading.Event().wait)()
    return checkpoint


if __name__ == "__main__":
    os.umask(0o077)
    root, profile, case, output = sys.argv[1:]; output = Path(output)
    value, binding = transport.profile(profile)
    name = store.task_name(binding["client_id"], case)
    spec = engine.specification(binding, QUESTION, max_requests=2, max_tools=1,
        max_tokens=512 if case == "known" else 64)
    _, created = store.create(root, name, spec); store.require(created, "initial task exists")

    class Records(engine.ProjectRecords):
        def execute(self, invocation):
            result = super().execute(invocation)
            with (output / "read-observations.jsonl").open("a") as receipt:
                receipt.write(json.dumps(dict(invocation=asdict(invocation), result=result), ensure_ascii=False) + "\n")
                receipt.flush(); os.fsync(receipt.fileno())
            return result

    with store.open_task(root, name) as ledger:
        engine.run(ledger, binding, transport.Client(value), records=Records(ROOT),
                   checkpoint=boundary(case, output))
    raise RuntimeError("expected application fault boundary was not reached")
