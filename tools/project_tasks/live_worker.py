"""Acceptance-only application process; real TLS and project reads, no service start."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import engine, store, transport

QUESTION = ("先调用一次read_project_record读取docs/MODEL.md的第1到24行，"
            "start_line=1、line_count=24。再回答architectures字段的准确值，"
            "并附工具给出的引用。")
CASES = {"known": (2, 1, "tool_result"), "unknown": (2, 2, "tool_returned"),
         "partial": (3, 1, "wire")}


def main():
    os.umask(0o077)
    root, profile, case, output = sys.argv[1:]
    output = Path(output)
    value, binding = transport.profile(profile)
    requests, tools, boundary = CASES[case]
    name = store.task_name(binding["client_id"], case)
    spec = engine.specification(binding, QUESTION, max_requests=requests, max_tools=tools)
    _, created = store.create(root, name, spec)
    store.require(created, "initial task already exists")

    class Records(engine.ProjectRecords):
        def execute(self, invocation):
            result = super().execute(invocation)
            # Separate acceptance evidence; the task engine never imports it.
            with (output / "read-observations.jsonl").open("a") as receipt:
                receipt.write(json.dumps(dict(invocation=asdict(invocation), result=result,
                    monotonic=time.monotonic()), ensure_ascii=False) + "\n")
                receipt.flush(); os.fsync(receipt.fileno())
            return result

    def checkpoint(kind, number, ledger):
        if kind == boundary:
            store.write_new(output / "boundary.json", store.encode(dict(
                pid=os.getpid(), kind=kind, number=number, task_id=name,
                monotonic=time.monotonic())))
            threading.Event().wait()

    with store.open_task(root, name) as ledger:
        engine.run(ledger, binding, transport.Client(value),
                   records=Records(ROOT), checkpoint=checkpoint)
    raise RuntimeError("expected interrupt boundary was not reached")


if __name__ == "__main__":
    main()
