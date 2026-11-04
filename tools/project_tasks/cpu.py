"""Freeze the complete CPU group before exercising it; no network or GPU."""
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import engine, store, test_tasks

BASE = test_tasks.BASE
SOURCES = sorted(set((*engine.RUNTIME_FILES,
    "tools/project_tasks/test_tasks.py", "tools/project_tasks/test_worker.py",
    "tools/project_tasks/cpu.py", "tools/examples/test_chat_client.py",
    "docs/SERVE_PROJECT_TASKS_2026-11-04.md")))


def run(targeted=False):
    os.umask(0o077)
    out = BASE / ("cpu-wire" if targeted else "cpu-01")
    out.mkdir(parents=True, mode=0o700)
    identities = {}
    for name in SOURCES:
        raw = (ROOT / name).read_bytes()
        identities[name] = engine.hashlib.sha256(raw).hexdigest()
        destination = out / "source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    stream = io.StringIO()
    suite = (unittest.defaultTestLoader.loadTestsFromName(
        "tools.project_tasks.test_tasks.Tasks.test_actual_wire_process_death_keeps_bytes_without_promoting_response")
        if targeted else unittest.defaultTestLoader.loadTestsFromModule(test_tasks))
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (out / "tests.log").write_text(stream.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), sources=identities,
        models_started=0, listeners_started=0, gpu_started=0,
        default={n: engine.hashlib.sha256((ROOT / n).read_bytes()).hexdigest()
                 for n in ("build/q4t", "build/q4t.release.json")})
    if targeted:
        prior = json.loads((BASE / "cpu-01/result.json").read_text())
        store.require(prior["passed"] and prior["tests_run"] == 18, "original CPU evidence")
        changed = {"tools/project_tasks/engine.py", "tools/project_tasks/test_tasks.py",
                   "tools/project_tasks/cpu.py"}
        for name, checksum in prior["sources"].items():
            store.require(engine.hashlib.sha256((BASE / "cpu-01/source" / name).read_bytes()).hexdigest()
                          == checksum, "original source snapshot changed")
            if name not in changed:
                store.require(identities[name] == checksum, "reused source changed")
        value.update(effective_tests=19, reused_tests=18,
            previous_sha256=engine.hashlib.sha256((BASE / "cpu-01/result.json").read_bytes()).hexdigest())
    store.write_new(out / "result.json", store.encode(value))
    print(stream.getvalue(), end="")
    print(json.dumps({k: v for k, v in value.items() if k != "sources"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    store.require(sys.argv[1:] in ([], ["wire"]), "cpu.py [wire]")
    raise SystemExit(run(sys.argv[1:] == ["wire"]))
