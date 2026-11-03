"""Focused correction and archive CLI/driver checks, without listeners."""
import io
import json
import os
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import archive_stage as stage, archive_worker
from tools.project_tasks import test_stage, test_tasks, test_archive, runner, store


class Boundaries(unittest.TestCase):
    def test_archived_cli_exit_is_distinct_from_completed_answer(self):
        fixture = test_archive.Archives("test_archive_preserves_bytes_answer_and_no_execution")
        fixture.setUp(); fixture.archive()
        fake = test_tasks.FakeClient()
        for action, expected, extra in (("submit", 2, ["--question", "read current", "--no-stream"]),
                                        ("resume", 2, []), ("inspect", 2, []), ("inspect-archive", 0, [])):
            argv = ["runner", action, "--root", str(fixture.root), "--profile", "private", "--key", "key", *extra]
            with patch.object(sys, "argv", argv), patch.object(runner.transport, "profile", return_value=({}, fixture.binding)), \
                    patch.object(runner.transport, "Client", return_value=fake), \
                    patch.object(sys, "stdout", io.StringIO()):
                self.assertEqual(runner.main(), expected)
        self.assertEqual((fake.calls, fake.preflight_calls, fake.cap_calls), ([], [], 0))

    def test_invalid_fault_boundary_cannot_silently_complete(self):
        with self.assertRaisesRegex(ValueError, "unknown retention checkpoint"):
            archive_worker.boundary(Path("unused"), "misspelled")


def run():
    os.umask(0o077)
    output = stage.BASE / "cpu-driver"; output.mkdir(mode=0o700)
    sources = {name: stage.pkg.sha(ROOT / name) for name in stage.SOURCES}
    for name in sources:
        path = output / "source" / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    suite = unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromModule(test_stage),
        unittest.defaultTestLoader.loadTestsFromTestCase(Boundaries),
        test_archive.Archives("test_exact_digest_stale_content_and_unknown_entries_refused")))
    log = io.StringIO()
    with patch.object(test_stage, "stage", stage), patch.object(test_tasks, "BASE", stage.BASE):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (output / "tests.log").write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        errors=len(result.errors), failures=len(result.failures), sources=sources,
        corrected_tests=1, driver_tests=4, models_started=0, listeners_started=0, gpu_started=0)
    store.write_new(output / "result.json", store.encode(value))
    print(log.getvalue(), end="")
    print(json.dumps({key: value for key, value in value.items() if key != "sources"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
