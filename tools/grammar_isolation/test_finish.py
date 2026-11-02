"""Only the observed cleanup race permits case reuse; failures stay failures."""
import time
import unittest
from unittest.mock import patch

import finish as f


class FinishContracts(unittest.TestCase):
    def test_only_completed_cleanup_is_reusable(self):
        result = dict(status="FAILED", request_attempts=7, starts=1,
            overload_rejections=1, error="AssertionError('worker leak or missing reap')")
        rows = dict(q4t_structured_workers_active=1,
            q4t_structured_workers_started_total=2, q4t_structured_workers_reaped_total=1,
            q4t_structured_compile_timeouts_total=1,
            q4t_structured_prepare_failed_total=1, q4t_structured_resource_exhausted_total=0)
        log = ("grammar worker reaped=1 \n" * 2 +
            "shutdown complete (0 in-flight remaining)\n"
            "event=end stage=response outcome=success")
        f.recognized_cleanup_failure(result, rows, log)
        for bad_log in (log.replace("reaped=1", "reaped=0"),
                        log.replace("0 in-flight", "1 in-flight")):
            with self.assertRaises(RuntimeError):
                f.recognized_cleanup_failure(result, rows, bad_log)
        with self.assertRaises(RuntimeError):
            f.recognized_cleanup_failure(dict(result, error="unexpected failure"), rows, log)

    def test_remaining_budget_is_one_start_and_three_requests(self):
        run = object.__new__(f.Remaining)
        run.deadline = time.time() + 5
        run.requests, run.starts, run.negative, run.capability_gets = 3, 1, 0, 1
        run.budget()
        run.starts = 2
        with self.assertRaises(RuntimeError):
            run.budget()

    def test_changed_installed_elf_blocks_health_reuse(self):
        with patch.object(f.h, "sha", return_value="unexpected"):
            with self.assertRaises(RuntimeError):
                f.installed_health(dict(candidate_sha256="expected"))


if __name__ == "__main__":
    unittest.main()
