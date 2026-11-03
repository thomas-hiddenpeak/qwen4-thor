"""Run an unchanged accepted historical engine, then pause after a saved read."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import threading
import time


def boundary(output, hold=None):
    def checkpoint(kind, number, ledger):
        if kind == "tool_result":
            from tools.project_tasks import store
            store.write_new(output / "boundary.json", store.encode(dict(pid=os.getpid(),
                kind=kind, number=number, task_id=ledger.meta["task_id"], monotonic=time.monotonic())))
            (hold or threading.Event().wait)()
    return checkpoint


def main():
    os.umask(0o077); sys.dont_write_bytecode = True
    runtime, root, profile, project, output, tokens, question = sys.argv[1:]
    sys.path.insert(0, runtime)
    from tools.project_tasks import engine, store, transport
    store.require(Path(engine.__file__).is_relative_to(Path(runtime)), "wrong historical engine imported")
    output = Path(output)
    value, binding = transport.profile(profile)
    options = dict(max_requests=2, max_tools=1)
    if (Path(runtime) / "tools/project_tasks/budget.py").exists():
        options["max_tokens"] = int(tokens)
    else:
        store.require(int(tokens) == 512, "legacy output must remain 512")
    spec = engine.specification(binding, question, **options)
    name = store.task_name(binding["client_id"], "migration-query")
    _, created = store.create(root, name, spec)
    store.require(created, "original historical task already exists")

    class Records(engine.ProjectRecords):
        def execute(self, invocation):
            result = super().execute(invocation)
            with (output / "read-observations.jsonl").open("a") as receipt:
                receipt.write(json.dumps(dict(invocation=asdict(invocation), result=result,
                    monotonic=time.monotonic()), ensure_ascii=False) + "\n")
                receipt.flush(); os.fsync(receipt.fileno())
            return result

    with store.open_task(root, name) as ledger:
        engine.run(ledger, binding, transport.Client(value), records=Records(Path(project)),
                   checkpoint=boundary(output))
    raise RuntimeError("historical task did not stop at saved result")


if __name__ == "__main__":
    main()
