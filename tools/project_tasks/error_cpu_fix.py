"""Recheck only the 18 cases blocked by the original CA fixture path."""
import io
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import error_cpu as cpu, test_tasks, test_errors, store


def run():
    os.umask(0o077)
    output = cpu.BASE / "cpu-fix-01"; output.mkdir(mode=0o700)
    names = sorted(set((*cpu.SOURCES, "tools/project_tasks/error_cpu_fix.py")))
    sources = {name: cpu.pkg.sha(ROOT / name) for name in names}
    for name in names:
        path = output / "source" / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    test_tasks.BASE = cpu.BASE
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(test_errors))
    (output / "tests.log").write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun, errors=len(result.errors),
        failures=len(result.failures), sources=sources, models_started=0, listeners_started=0, gpu_started=0)
    store.write_new(output / "result.json", store.encode(value))
    print(log.getvalue(), end="")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
