"""Offline acceptance controls; these do not substitute for real model evidence."""
import copy
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import stage
v = stage.module("b2_contract_checks", Path(__file__).with_name("verify.py"))
import check_schema as schema


class Contracts(unittest.TestCase):
    def test_compare_preserves_usage_and_content(self):
        public = SimpleNamespace(message=dict(content="710003", reasoning="why"),
            usage=dict(prompt_tokens=5, completion_tokens=20), finish_reason="stop")
        hidden = copy.deepcopy(public)
        del hidden.message["reasoning"]
        v.compare(public, hidden, "reasoning")
        for target, key, value in (("usage", "completion_tokens", 3),
                                   ("message", "content", "wrong")):
            altered = copy.deepcopy(hidden)
            getattr(altered, target)[key] = value
            with self.assertRaises(AssertionError):
                v.compare(public, altered, "reasoning")

    def test_budget_and_modes(self):
        self.assertIn("--no-mtp", v.b.command("q4t", "m", 1, False))
        self.assertNotIn("--no-mtp", v.b.command("q4t", "m", 1, True))
        run = object.__new__(v.Run)
        run.installed, run.deadline = False, time.time() + 60
        run.requests, run.negative, run.starts, run.capability_gets = 13, 12, 2, 2
        run.budget()
        with self.assertRaises(AssertionError):
            run.counted()
        run.requests, run.starts = 13, 3
        with self.assertRaises(AssertionError):
            run.budget()

    def test_request_options_are_sdk_extras(self):
        for field in (None, "reasoning"):
            body = v.simple(field=field, include=False, stream=True)
            self.assertIs(body["extra_body"]["include_reasoning"], False)
            self.assertEqual(body["extra_body"].get("reasoning_field"), field)
            self.assertLessEqual(body["max_completion_tokens"], 128)

    def test_negative_locations_and_schema(self):
        cases = v.negative_cases()
        self.assertEqual(len(cases), 12)
        self.assertEqual(len(set(r[0] for r in cases)), 12)
        request = schema.validator("request")
        history = schema.validator("history")
        for label, body, param in cases:
            self.assertTrue(param)
            if label == "history-conflict":
                continue  # Equal-value alias checking belongs to native code.
            self.assertTrue(not request.is_valid(body) or any(
                not history.is_valid(m) for m in body.get("messages", [])), label)

    def test_output_projection_refuses_hidden_and_mixed_reasoning(self):
        output, hidden = schema.validator("output"), schema.validator("hidden")
        self.assertTrue(output.is_valid(dict(reasoning="why")))
        self.assertFalse(output.is_valid(dict(reasoning="why", reasoning_content="why")))
        self.assertFalse(hidden.is_valid(dict(reasoning="")))
        self.assertTrue(hidden.is_valid(dict(content="<think>literal</think>")))

    def test_guard_before_install(self):
        stage.bind()
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
             patch.object(stage.a, "check") as checked:
            with self.assertRaises(RuntimeError):
                stage.a.execute()
            checked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
