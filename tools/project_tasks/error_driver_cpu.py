"""Error driver environmental abort and owned-process cleanup."""
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import error_stage as stage, test_stage, test_tasks, store


def run():
    os.umask(0o077)
    output = stage.BASE / "cpu-driver"; output.mkdir(mode=0o700)
    sources = {name: stage.pkg.sha(ROOT / name) for name in stage.SOURCES}
    for name in sources:
        path = output / "source" / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_stage)
    log = io.StringIO()
    with patch.object(test_stage, "stage", stage), patch.object(test_tasks, "BASE", stage.BASE):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (output / "tests.log").write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun, errors=len(result.errors),
        failures=len(result.failures), sources=sources, models_started=0, listeners_started=0, gpu_started=0)
    store.write_new(output / "result.json", store.encode(value))
    print(log.getvalue(), end="")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
