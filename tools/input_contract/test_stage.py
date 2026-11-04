"""Offline checks for input-contract acceptance accounting and tool execution."""
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import stage
v = stage.module("input_checks_test", Path(__file__).with_name("verify.py"))


def completion(message, finish):
    return v.wire.read_json(io.BytesIO(json.dumps(dict(id="reply",
        model="qwen3.8-flash-next", object="chat.completion", choices=[dict(
            index=0, message=message, finish_reason=finish)], usage=dict(
                prompt_tokens=1, completion_tokens=1, total_tokens=2))).encode()))


class StageContracts(unittest.TestCase):
    def test_negative_wire_retains_fractional_spelling(self):
        cases = dict(v.negative_cases())
        self.assertEqual(len(cases), 16)
        for name in ("fractional-budget", "fractional-timeout", "fractional-array-bound"):
            self.assertIn("1.0000000000000001", cases[name])
        self.assertIn("9007199254740991.1", cases["fractional-seed"])
        for text in cases.values():
            self.assertIsInstance(json.loads(text), dict)
        self.assertIsNone(json.loads(cases["auto-outer-strict"])["tools"][0][
            "function"]["strict"])

    def test_real_read_and_identified_history_in_both_modes(self):
        for strict in (False, True):
            with tempfile.TemporaryDirectory(dir=os.environ["Q4T_TEST_OUTPUT_DIR"]) as tmp:
                run = v.Run(dict(started=time.time(), model_dir="unused",
                    protocol_sha256="a" * 64), Path(tmp) / "run")
                first = completion(dict(role="assistant", content=None,
                    tool_calls=[dict(id="call-a", type="function", function=dict(
                        name="read_project_record", arguments=json.dumps(dict(
                            path="docs/MODEL.md", start_line=1, line_count=8))))]), "tool_calls")
                def reply(folder, label, body):
                    if label == "project-read":
                        self.assertEqual(body["tool_choice"], "required" if strict else "auto")
                        if not strict:
                            for key in v.NULLS:
                                self.assertIsNone(body[key])
                        return first
                    self.assertEqual(body["messages"][-1]["tool_call_id"], "call-a")
                    record = json.loads(body["messages"][-1]["content"])
                    self.assertIn("Qwen3.8", record["source"]["text"])
                    return completion(dict(role="assistant", content="Qwen3.8 " +
                        record["source"]["citation"]), "stop")
                with patch.object(run, "success", side_effect=reply) as mocked:
                    run.project_loop(run.out, strict=strict, stream=strict)
                self.assertEqual(mocked.call_count, 2)

    def test_incomplete_tool_response_never_executes(self):
        with tempfile.TemporaryDirectory(dir=os.environ["Q4T_TEST_OUTPUT_DIR"]) as tmp:
            run = v.Run(dict(started=time.time(), model_dir="unused",
                protocol_sha256="a" * 64), Path(tmp) / "run")
            with patch.object(run, "success", return_value=completion(
                    dict(role="assistant", content="incomplete"), "length")), \
                 patch.object(v.s.ProjectRecords, "execute") as execute:
                with self.assertRaises(AssertionError):
                    run.project_loop(run.out, strict=False, stream=False)
                execute.assert_not_called()

    def test_no_model_request_beyond_frozen_budget(self):
        run = object.__new__(v.Run)
        run.installed = False
        run.deadline = time.time() + 60
        run.requests, run.negative, run.starts, run.capability_gets = 6, 19, 2, 2
        run.budget()
        with self.assertRaises(AssertionError):
            run.counted()
        run.requests = 6
        with self.assertRaises(AssertionError):
            run.counted(negative=True)
        run.negative = 19
        run.deadline = time.time() - 1
        with self.assertRaises(AssertionError):
            run.budget()

    def test_mutations_require_gpu_guard(self):
        stage.bind()
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
             patch.object(stage.a, "check") as checked:
            with self.assertRaises(RuntimeError):
                stage.a.execute()
            with self.assertRaises(RuntimeError):
                stage.a.restore(Path("unused"))
            checked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
