"""Post-delivery rollback verification; no model/network/default mutation."""
import copy
import unittest
from unittest.mock import patch

from tools.serve_admission import stage


class RollbackTests(unittest.TestCase):
    def test_sealed_pair_remains_usable_after_workspace_changes(self):
        with patch.object(stage.a, "verify_hashes", side_effect=AssertionError("workspace dependency")):
            plan = stage.rollback_plan()
        self.assertEqual(plan["previous_binary_sha256"], stage.prior.DEFAULT_SHA)
        self.assertEqual(plan["binary"]["sha256"], plan["candidate_sha256"])

    def test_foreign_package_refused_before_artifact_use(self):
        read = stage.h.read
        for key, value in (("root", "/different-worktree"),
                           ("previous_binary_sha256", "0"*64)):
            plan = copy.deepcopy(read(stage.PACKAGE / "plan.json"))
            plan[key] = value
            def altered(path):
                return plan if path == stage.PACKAGE / "plan.json" else read(path)
            with patch.object(stage.h, "read", side_effect=altered):
                with self.assertRaises(RuntimeError):
                    stage.rollback_plan()


if __name__ == "__main__":
    unittest.main()
