"""Isolation and accounting gates for the new diagnostic, no GPU."""
import copy
import unittest

from tools.vision_service import closeout as c


class Closeout(unittest.TestCase):
    def test_contrasts_do_not_mutate_media_prompt_or_other_controls(self):
        original = dict(messages=[dict(role="user", content=[dict(
            type="video_frames", video_frames=["first", "second"],
            video_timestamps=[0, 1])])], max_tokens=256, stream=False,
            response_format=c.s.structured(dict(direction=dict(type="string"))))
        snapshot = copy.deepcopy(original)
        rows = c.contrasts([("forward", original, dict(direction="left_to_right"))])
        self.assertEqual(original, snapshot)
        self.assertEqual(len({r["request"]["request_id"] for r in rows}), 3)
        for row in rows:
            self.assertRegex(row["request"]["cancel_token"], r"^[0-9a-f]{64}$")
            self.assertEqual(c.input_contract(row["request"]), c.input_contract(original))
        self.assertNotIn("response_format", rows[-1]["request"])
        rows[-1]["request"]["messages"][0]["content"][0]["video_frames"].reverse()
        self.assertEqual(original, snapshot)
        self.assertEqual(rows[0]["request"]["messages"], snapshot["messages"])

    def test_total_budget_counts_failed_and_prepared_phases(self):
        self.assertEqual(c.budget_plans([dict(limits=c.LIMITS)]), c.LIMITS)
        with self.assertRaisesRegex(RuntimeError, "total phase budget"):
            c.budget_plans([dict(limits=c.LIMITS)] * 3)
        with self.assertRaisesRegex(RuntimeError, "invalid budget"):
            c.budget_plans([dict(limits=dict(c.LIMITS, starts=-1))])

    def test_truth_comes_from_record_not_fixed_expected_values(self):
        data = dict(totals=dict(starts=2, generations=29), phases=[
            dict(phase=p, counts=dict(generations=n)) for p, n in
            (("phase-04", 3), ("phase-05b", 22), ("phase-07", 4))])
        truth = c.record_truth(data)
        self.assertEqual(truth["totals"], data["totals"])
        self.assertEqual(truth["tallest"], "phase-05b")


if __name__ == "__main__":
    unittest.main()
