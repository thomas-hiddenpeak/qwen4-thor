"""Freeze task budget contracts and legacy recovery regression as one CPU group."""
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import engine, store, test_tasks, test_budget
from tools.project_tasks import correction as legacy

BASE = ROOT / ".q4t-work/serve-task-budgets-20261104"
SOURCES = sorted(set((*legacy.SOURCES, *engine.RUNTIME_FILES,
    "tools/project_tasks/test_budget.py", "tools/project_tasks/budget_cpu.py",
    "tools/project_tasks/report.py", "docs/SERVE_PROJECT_TASK_BUDGET_2026-11-04.md")))


def run():
    os.umask(0o077)
    output = BASE / "cpu-01"; output.mkdir(parents=True, mode=0o700)
    sources = {name: legacy.stage.pkg.sha(ROOT / name) for name in SOURCES}
    for name in sources:
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    test_tasks.BASE = BASE
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(module)
                               for module in (test_tasks, test_budget))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (output / "tests.log").write_text(stream.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        errors=len(result.errors), failures=len(result.failures), sources=sources,
        models_started=0, listeners_started=0, gpu_started=0,
        legacy_archive_sha256=legacy.stage.pkg.sha(ROOT / "docs/evidence/serve-project-tasks-20261104/records.tar.gz"),
        default={name: legacy.stage.pkg.sha(ROOT / name) for name in ("build/q4t", "build/q4t.release.json")})
    store.write_new(output / "result.json", store.encode(value))
    print(stream.getvalue(), end="")
    print(json.dumps({k: v for k, v in value.items() if k != "sources"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
