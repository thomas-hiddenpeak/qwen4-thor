"""Check migration-stage cleanup and saved-result stop without HTTP/GPU."""
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
from tools.project_tasks import migration_stage as stage, migration_worker, store, test_stage, test_tasks


class SavedResultBoundary(unittest.TestCase):
    def test_historical_worker_boundary_prevents_extra_generation(self):
        fixture = test_tasks.Tasks("test_complete_then_duplicate_is_read_only"); fixture.setUp()
        fake = test_tasks.FakeClient()
        def hold(): raise InterruptedError("saved result checkpoint")
        with self.assertRaisesRegex(InterruptedError, "checkpoint"):
            fixture.execute(fake, migration_worker.boundary(fixture.directory, hold=hold))
        value = fixture.status()
        self.assertEqual((value["state"], len(fake.calls), fixture.count(), value["saved_tool_results"]),
                         ("TOOLS_PENDING", 1, 1, 1))
        marker = json.loads((fixture.directory / "boundary.json").read_bytes())
        self.assertEqual(marker["task_id"], value["task_id"])


def run():
    os.umask(0o077)
    output = stage.BASE / "cpu-driver"; output.mkdir(mode=0o700)
    sources = {name: stage.pkg.sha(ROOT / name) for name in stage.SOURCES}
    for name in sources:
        path = output / "source" / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    suite = unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromModule(test_stage),
                               unittest.defaultTestLoader.loadTestsFromTestCase(SavedResultBoundary)))
    log = io.StringIO()
    with patch.object(test_stage, "stage", stage), patch.object(test_tasks, "BASE", stage.BASE):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (output / "tests.log").write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        errors=len(result.errors), failures=len(result.failures), sources=sources,
        models_started=0, listeners_started=0, gpu_started=0)
    store.write_new(output / "result.json", store.encode(value))
    print(log.getvalue(), end=""); print(json.dumps({key: value for key, value in value.items() if key != "sources"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
