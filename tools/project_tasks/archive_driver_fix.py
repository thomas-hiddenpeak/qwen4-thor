"""Focused CLI fixture correction and public refusal classification."""
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import archive_stage as s, archive_driver_cpu as prior, test_tasks, store


class Refusal(unittest.TestCase):
    def test_exact_public_error_contract(self):
        s.refused(SimpleNamespace(returncode=1, stdout="", stderr=json.dumps(dict(error="ValueError", automatic_retry=False))))
        for code, out, err in ((0, "", '{}'), (1, "unexpected", '{}'),
                               (1, "", '{"error":"RuntimeError","automatic_retry":false}')):
            with self.assertRaises(ValueError):
                s.refused(SimpleNamespace(returncode=code, stdout=out, stderr=err))


def run():
    os.umask(0o077)
    output = s.BASE / "cpu-driver-fix"; output.mkdir(mode=0o700)
    sources = {name: s.pkg.sha(ROOT / name) for name in s.SOURCES}
    for name in sources:
        path = output / "source" / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    suite = unittest.TestSuite((prior.Boundaries("test_archived_cli_exit_is_distinct_from_completed_answer"),
                               Refusal("test_exact_public_error_contract")))
    log = io.StringIO()
    with patch.object(test_tasks, "BASE", s.BASE):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (output / "tests.log").write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun, errors=len(result.errors),
        failures=len(result.failures), sources=sources, models_started=0, listeners_started=0, gpu_started=0)
    store.write_new(output / "result.json", store.encode(value))
    print(log.getvalue(), end="")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
