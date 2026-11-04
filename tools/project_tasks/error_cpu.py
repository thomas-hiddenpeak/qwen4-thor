"""Freeze safe command errors and verify application contracts as one CPU group."""
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import store, engine, archive_cpu, migration_fixtures as f
from tools.project_tasks import test_tasks, test_budget, test_migration, test_archive, test_backup, test_errors
from tools.serve_service import package as pkg
from tools.project_tasks import backup_cpu as backup_sources

BASE = ROOT / ".q4t-work/serve-task-errors-20261104"
SOURCES = sorted(set((*backup_sources.SOURCES, *engine.RUNTIME_FILES,
    "tools/project_tasks/error_cpu.py", "tools/project_tasks/test_errors.py",
    "docs/project-task-errors-v1.schema.json", "tools/project_tasks/backup_report.py",
    "tools/project_tasks/backup_worker.py", "tools/project_tasks/archive_report.py",
    "docs/SERVE_TASK_ERRORS_2026-11-04.md")))


def run():
    os.umask(0o077)
    output = BASE / "cpu-01"; output.mkdir(parents=True, mode=0o700)
    sources = {name: pkg.sha(ROOT / name) for name in SOURCES}
    for name in sources:
        path = output / "source" / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    test_tasks.BASE = BASE
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(module)
        for module in (test_tasks, test_budget, test_migration, test_archive, test_backup, test_errors))
    log = io.StringIO(); result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (output / "tests.log").write_text(log.getvalue())
    kills = [json.loads(path.read_bytes()) for path in sorted((BASE / "fixtures").glob("*/killed-*.json"))]
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun, errors=len(result.errors),
        failures=len(result.failures), sources=sources, file_operation_kills=kills,
        models_started=0, listeners_started=0, gpu_started=0,
        archives={f.release(i)["evidence"]: f.release(i)["archive_sha256"] for i in range(5)},
        default={name: pkg.sha(ROOT / name) for name in ("build/q4t", "build/q4t.release.json")})
    store.write_new(output / "result.json", store.encode(value))
    print(log.getvalue(), end="")
    print(json.dumps({key: value for key, value in value.items() if key not in ("sources", "file_operation_kills")}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
