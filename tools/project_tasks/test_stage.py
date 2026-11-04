"""Driver fault cleanup and environmental classification; no listeners."""
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.project_tasks import stage


class Driver(unittest.TestCase):
    def fixture(self):
        parent = stage.BASE / "driver-fixtures"
        parent.mkdir(exist_ok=True)
        root = Path(tempfile.mkdtemp(dir=parent))
        instance = object.__new__(stage.Run)
        instance.started = time.monotonic(); instance.gpu_started = instance.started
        instance.fronts = {}; instance.logs = []; instance.applications = []
        instance.run_dir = root / "not-started"; instance.counts = {}
        instance.launch = Mock(); instance.stop_backend = Mock()
        return root / "execution", instance

    def test_gpu_observation_abort_never_launches_or_becomes_product_failure(self):
        output, run = self.fixture()
        with patch.object(stage, "OUT", output), patch.object(stage.a, "require_idle",
                side_effect=RuntimeError("ENV_ABORT: occupied or unobservable GPU")):
            with self.assertRaisesRegex(RuntimeError, "ENV_ABORT"):
                run.execute()
        run.launch.assert_not_called()
        self.assertEqual(stage.pkg.read(output / "result.json")["status"], "ENV_ABORT")

    def test_failure_stops_application_then_frontend_then_backend(self):
        output, run = self.fixture()
        seen = []
        run.launch = Mock(side_effect=ValueError("injected application failure"))
        run.applications = [dict(owner={"pid": 999999999}, process=SimpleNamespace(
            poll=lambda: None, wait=lambda _: seen.append("application-exited")))]
        run.fronts = {1: dict(process=SimpleNamespace(poll=lambda: None,
            wait=lambda _: seen.append("frontend-exited")))}
        run.front_cli = lambda *args: seen.append("frontend-stop")
        run.stop_backend = lambda: seen.append("backend-stop")
        with patch.object(stage, "OUT", output), patch.object(stage.a, "require_idle"), \
                patch.object(stage.sup, "signal_owner", side_effect=lambda *a: seen.append("application-stop")), \
                patch.object(stage.a, "lock_available", return_value=True), \
                patch.object(stage.pkg, "read", return_value={"status": "COMPLETED"}), \
                patch.object(stage.evidence, "guard_archive", return_value={}), patch.object(stage, "check"):
            with self.assertRaisesRegex(ValueError, "injected application failure"):
                run.execute()
        self.assertEqual(seen, ["application-stop", "application-exited",
                               "frontend-stop", "frontend-exited", "backend-stop"])


if __name__ == "__main__":
    unittest.main()
