"""Target the observed sealed-log capacity boundary and continuation cleanup."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import migration_continue as stage, store, test_stage


class Continued(unittest.TestCase):
    def test_original_capacity_refusal_keeps_the_sealed_gateway(self):
        retained = stage.stage.retained.store
        root = Path(tempfile.mkdtemp(dir=stage.ORIGINAL / "continuation-fixtures")) / "store"
        retained.init(root, max_bytes=128 * 1024**2, max_generations=3)
        owner = stage.sup.process_identity(os.getpid())
        backend, backend_fd = retained.reserve(root, "backend", {}, owner)
        first, front_fd = retained.reserve(root, "gateway", {}, owner)
        try:
            retained.save(first / "retained-log.json", dict(log="original bytes retained"))
            retained.seal(first, dict(cleanup_confirmed=True), front_fd)
            os.close(front_fd); front_fd = None
            with self.assertRaisesRegex(RuntimeError, "capacity exhausted"):
                retained.reserve(root, "gateway", {}, owner)
            self.assertTrue((first / "retained-log.json").exists())
            self.assertEqual(len(list((root / "generations").iterdir())), 2)
        finally:
            os.close(backend_fd)
            if front_fd is not None: os.close(front_fd)

    def test_full_sequence_preflight_and_small_continuation_fit(self):
        policy = dict(max_bytes=128 * 1024**2, max_generations=3)
        with self.assertRaisesRegex(ValueError, "exceeds frozen capacity"):
            stage.capacity(policy, ("backend", "gateway", "gateway"))
        self.assertEqual(stage.capacity(dict(policy, max_bytes=160 * 1024**2),
            ("backend", "gateway", "gateway")), 160 * 1024**2)
        self.assertEqual(stage.capacity(dict(policy, max_generations=2), ("backend", "gateway")), 128 * 1024**2)

    def test_continuation_only_finishes_saved_tasks(self):
        root = Path(tempfile.mkdtemp(dir=stage.ORIGINAL / "continuation-fixtures"))
        instance = object.__new__(stage.Run); instance.counts = {}; instance.fronts = {1: dict(folder=root)}
        instance.backend_folder = root
        instance.start_front = Mock(); instance.revoked = Mock(); instance.migrated = Mock()
        instance.historical = Mock(side_effect=AssertionError("historical work must not rerun"))
        instance.front_cli = Mock(); instance.wait_front = Mock(); instance.wait_gone = Mock()
        instance.preserve = Mock(); instance.stop_backend = Mock()
        instance.drain = Mock(return_value=dict(q4t_structured_workers_started_total=2, q4t_structured_workers_reaped_total=2))
        with patch.object(stage, "OUT", root): instance.business()
        instance.historical.assert_not_called(); instance.start_front.assert_called_once_with(1)
        self.assertEqual(instance.migrated.call_args_list, [call("alpha", 1), call("beta", 2)])
        self.assertEqual(instance.counts["generations"], 2)


def run():
    os.umask(0o077)
    output = stage.ORIGINAL / "cpu-continuation"; output.mkdir(mode=0o700)
    (stage.ORIGINAL / "continuation-fixtures").mkdir(mode=0o700)
    sources = {name: stage.pkg.sha(ROOT / name) for name in stage.SOURCES}
    for name in sources:
        path = output / "source" / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    suite = unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromModule(test_stage),
                               unittest.defaultTestLoader.loadTestsFromTestCase(Continued)))
    log = io.StringIO()
    with patch.object(test_stage, "stage", stage):
        # Shared cleanup cases allocate only under their module's BASE.
        with patch.object(stage, "BASE", stage.ORIGINAL):
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (output / "tests.log").write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        errors=len(result.errors), failures=len(result.failures), sources=sources,
        models_started=0, listeners_started=0, gpu_started=0, reused_application_tests=49, reused_driver_tests=3)
    store.write_new(output / "result.json", store.encode(value))
    print(log.getvalue(), end=""); print(json.dumps({key: value for key, value in value.items() if key != "sources"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
