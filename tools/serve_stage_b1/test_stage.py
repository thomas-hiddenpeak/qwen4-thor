"""Offline driver checks: modes, immutable histories, rejected fields, limits."""
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import stage
v = stage.module("b1_contract_checks", Path(__file__).with_name("verify.py"))


class Contracts(unittest.TestCase):
    def test_decode_modes_reach_real_command(self):
        self.assertNotIn("--no-mtp", v.command("q4t", "model", 8000, True))
        self.assertIn("--no-mtp", v.command("q4t", "model", 8000, False))

    def test_history_keeps_instruction_before_tool_result(self):
        run = object.__new__(v.Run)
        body = v.s.request("question")
        with patch.object(v.t.Run, "success"):
            run.success(None, "first", body)
            self.assertEqual(body["messages"][0]["role"], "developer")
            body["messages"].append(dict(role="tool", content="result"))
            run.success(None, "second", body)
        self.assertEqual(sum(m["role"] == "developer" for m in body["messages"]), 1)
        self.assertEqual(body["extra_body"]["request_timeout_ms"], 180000)

    def test_comparison_catches_usage_and_content_drift(self):
        a = SimpleNamespace(message=dict(content="x"), finish_reason="stop", usage=dict(prompt_tokens=1))
        v.compare(a, a)
        b = SimpleNamespace(**vars(a))
        b.usage = dict(prompt_tokens=2)
        with self.assertRaises(AssertionError):
            v.compare(a, b)

    def test_negative_cases_have_exact_locations(self):
        rows = v.d.negative_cases()
        self.assertEqual(len(rows), 12)
        self.assertEqual(len({r[0] for r in rows}), 12)
        self.assertTrue(all(param.startswith("messages[") for _, _, param in rows))

    def test_budget_rejects_extra_start_or_generation(self):
        run = object.__new__(v.Run)
        run.installed, run.deadline = False, time.time() + 60
        run.requests, run.negative, run.starts, run.capability_gets = 8, 12, 2, 2
        run.budget()
        with self.assertRaises(AssertionError):
            run.counted()
        run.requests, run.starts = 8, 3
        with self.assertRaises(AssertionError):
            run.budget()

    def test_guard_required_before_any_install_state(self):
        stage.bind()
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
             patch.object(stage.a, "check") as checked:
            with self.assertRaises(RuntimeError):
                stage.a.execute()
            checked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
