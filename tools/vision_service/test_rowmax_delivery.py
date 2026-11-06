"""Reuse real temporary-file rollback/failure contracts for the new transaction."""
import json
import unittest
from unittest.mock import patch

from tools.vision_service import rowmax_delivery
from tools.vision_service import test_delivery as contracts


class RowmaxDelivery(contracts.Delivery):
    def setUp(self):
        replacement = patch.object(contracts, "d", rowmax_delivery)
        replacement.start()
        self.addCleanup(replacement.stop)
        super().setUp()

    def test_outer_guard_retains_marker_until_finalization(self):
        d = rowmax_delivery
        stage = d.ROOT / "stage"
        health = stage / "health"
        (health / "lease").mkdir(parents=True)
        (health / "execution/model-health").mkdir(parents=True)
        guard = health / "lease/exit.json"
        guard.write_text(json.dumps(dict(status="COMMAND_FAILED", child_returncode=1)))
        (health / "execution/result.json").write_text(json.dumps(dict(
            status=d.Run.success_status, failure=None, cleanup_errors=[],
            monitor_failures=[], counts=dict(starts=1, generations=4, controls=3, native=0))))
        (health / "execution/model-health/identity.json").write_text(json.dumps(dict(pid=999999999)))
        with patch.object(d, "BORROW_GUARD", True):
            self.assertEqual(d.change()["status"], "PASS_INSTALLED_PAIR")
        self.assertTrue(d.MARKER.exists())
        with patch.object(d, "BASE", stage), patch.object(d, "HEALTH", health):
            with self.assertRaisesRegex(RuntimeError, "outer guard incomplete"):
                d.finalize()
            self.assertTrue(d.MARKER.exists())
            guard.write_text(json.dumps(dict(status="COMPLETED", child_returncode=0)))
            result = d.finalize()
            self.assertFalse(d.MARKER.exists())
            self.assertEqual(d.finalize(), result)


if __name__ == "__main__":
    unittest.main()
