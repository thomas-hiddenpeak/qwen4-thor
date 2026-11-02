"""Offline checks of the acceptance controls, not model validation."""
import copy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import stage
v = stage.module("budget_contract_checks", Path(__file__).with_name("verify.py"))
import check_schema as schema


class Contracts(unittest.TestCase):
    def test_disabled_and_zero_are_distinct(self):
        usage = dict(prompt_tokens=2, completion_tokens=4, total_tokens=6)
        schema.check_usage(usage, None)
        with self.assertRaises(Exception):
            schema.check_usage(usage, 0)
        usage.update(completion_tokens_details=dict(reasoning_tokens=1),
            q4t_thinking_budget=dict(limit=0, consumed=0, forced_closure_tokens=1))
        schema.check_usage(usage, 0)
        with self.assertRaises(ValueError):
            schema.check_usage(usage, None)

    def test_usage_equations_are_not_just_json_schema(self):
        usage = dict(prompt_tokens=2, completion_tokens=20, total_tokens=22,
            completion_tokens_details=dict(reasoning_tokens=17),
            q4t_thinking_budget=dict(limit=16, consumed=16, forced_closure_tokens=1))
        schema.check_usage(usage, 16)
        for key, value in (("consumed", 17), ("forced_closure_tokens", 12), ("limit", 15)):
            changed = copy.deepcopy(usage)
            changed["q4t_thinking_budget"][key] = value
            with self.assertRaises(Exception):
                schema.check_usage(changed, 16)
        with self.assertRaises(ValueError):
            schema.check_usage(usage, 8)

    def test_lexical_negatives_and_projection(self):
        cases = v.negative_cases()
        self.assertEqual(len(cases), 12)
        self.assertEqual(len({name for name, _ in cases}), 12)
        validator = schema.validator("request")
        for _, raw in cases:
            body = json.loads(raw, parse_float=Decimal)
            self.assertFalse(validator.is_valid(body), raw)
        self.assertTrue(validator.is_valid(dict(prompt="raw", thinking_token_budget=None)))
        self.assertTrue(validator.is_valid(dict(messages=[{}], thinking_token_budget=2147483647)))

    def test_total_limit_and_sdk_extensions(self):
        body = v.schema_body(0, maximum=1)
        self.assertEqual(body["max_completion_tokens"], 1)
        self.assertEqual(body["extra_body"]["thinking_token_budget"], 0)
        self.assertEqual(body["stream_options"], dict(include_usage=True))
        self.assertNotIn("thinking_token_budget", body)

    def test_command_limits_and_private_cache(self):
        mtp = v.command("candidate", "model", 18098, True, Path("/private/cache"))
        self.assertEqual(mtp[mtp.index("--max-len") + 1], "16384")
        self.assertNotIn("--no-mtp", mtp)
        plain = v.command("candidate", "model", 18098, False)
        self.assertIn("--no-mtp", plain)
        self.assertNotIn("--session-cache-dir", plain)
        body = v.cache_seed()
        self.assertTrue(body["extra_body"]["chat_template_kwargs"]["preserve_thinking"])
        self.assertEqual(len(body["extra_body"]["session_id"]), 64)

    def test_request_start_and_time_limits(self):
        run = object.__new__(v.Run)
        run.installed, run.deadline = False, time.time() + 60
        run.requests, run.negative, run.starts, run.capability_gets = 21, 12, 3, 3
        run.budget()
        with self.assertRaises(AssertionError):
            run.counted()
        run.requests, run.starts = 21, 4
        with self.assertRaises(AssertionError):
            run.budget()
        run.starts, run.deadline = 3, time.time() - 1
        with self.assertRaises(AssertionError):
            run.budget()

    def test_guard_before_install(self):
        stage.bind()
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
             patch.object(stage.a, "check") as checked:
            with self.assertRaises(RuntimeError):
                stage.a.execute()
            checked.assert_not_called()

    def test_sdk_failure_preserves_raw_error(self):
        run = object.__new__(v.Run)
        run.counted = lambda: None
        error = RuntimeError("expected failure")
        error.response = SimpleNamespace(content=b'{"error":{"code":"failed"}}',
                                         headers={"x-request-id": "trace-failure"})
        def fail(**unused):
            raise error
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            with_raw_response=SimpleNamespace(create=fail))))
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with self.assertRaises(RuntimeError):
                run.sdk_chat(client, folder, "failure", {})
            self.assertEqual((folder / "failure-wire.bin").read_bytes(), error.response.content)
            self.assertIn("expected failure", (folder / "failure-failure.json").read_text())


if __name__ == "__main__":
    unittest.main()
