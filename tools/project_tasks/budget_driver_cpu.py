"""CPU checks for the budget stage's cleanup and strict negative boundary."""
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
from tools.project_tasks import budget_stage as stage, budget_worker, store
from tools.project_tasks import test_stage, test_tasks


class NegativeBoundary(unittest.TestCase):
    def test_accepted_negative_fixture_cannot_generate(self):
        fixture = test_tasks.Tasks("test_complete_then_duplicate_is_read_only")
        fixture.setUp()
        client = test_tasks.FakeClient()
        with self.assertRaisesRegex(RuntimeError, "unexpectedly passed preflight"):
            fixture.execute(client, budget_worker.boundary("negative", fixture.directory))
        state = fixture.status()
        self.assertEqual((len(client.calls), fixture.count(), state["requests"]), (0, 0, 0))
        self.assertEqual((state["state"], state["preflight_checks"]), ("PREFLIGHT_APPROVED", 1))


def run():
    os.umask(0o077)
    output = stage.BASE / "cpu-driver"
    output.mkdir(mode=0o700)
    sources = {name: stage.pkg.sha(ROOT / name) for name in stage.SOURCES}
    for name in sources:
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    suite = unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromModule(test_stage),
                               unittest.defaultTestLoader.loadTestsFromTestCase(NegativeBoundary)))
    log = io.StringIO()
    with patch.object(test_stage, "stage", stage), patch.object(test_tasks, "BASE", stage.BASE):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (output / "tests.log").write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
                 errors=len(result.errors), failures=len(result.failures), sources=sources,
                 models_started=0, listeners_started=0, gpu_started=0)
    store.write_new(output / "result.json", store.encode(value))
    print(log.getvalue(), end="")
    print(json.dumps({key: value for key, value in value.items() if key != "sources"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
