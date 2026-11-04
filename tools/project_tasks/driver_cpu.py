"""Freeze and check the real-task recovery driver's failure handling."""
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import store, stage, test_stage


def run():
    os.umask(0o077)
    output = stage.BASE / "cpu-driver"
    output.mkdir(mode=0o700)
    sources = {}
    for name in stage.SOURCES:
        source = ROOT / name
        sources[name] = stage.pkg.sha(source)
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(test_stage))
    (output / "tests.log").write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), sources=sources,
        new_models_or_listeners=0)
    store.write_new(output / "result.json", store.encode(value))
    print(log.getvalue(), end="")
    print(json.dumps({k: v for k, v in value.items() if k != "sources"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
