"""Offline safety checks for stage accounting, error publication and tool replay."""
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import stage
v = stage.module("structured_checks_test", Path(__file__).with_name("verify.py"))


ERROR = dict(error=dict(code="structured_output_incomplete", type="server_error",
                       param=None, message="incomplete"))


def sse(events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"


class StageContracts(unittest.TestCase):
    def test_complete_error_terminals(self):
        v.error_terminal(502, json.dumps(ERROR), False)
        v.error_terminal(200, sse([dict(choices=[dict(delta=dict(role="assistant",
            content=""), finish_reason=None)]), ERROR]), True)

    def test_error_never_releases_partial_success(self):
        for event in [dict(usage={}), dict(choices=[dict(finish_reason="stop")]),
                dict(choices=[dict(delta=dict(content="partial"))]),
                dict(choices=[dict(delta=dict(tool_calls=[{}]))]),
                dict(choices=[dict(delta=dict(reasoning_content="partial"))])]:
            with self.assertRaises(AssertionError):
                v.error_terminal(200, sse([event, ERROR]), True)
        with self.assertRaises(AssertionError):
            v.error_terminal(502, json.dumps(dict(**ERROR, choices=[])), False)
        with self.assertRaises(AssertionError):
            v.error_terminal(200, sse([ERROR, dict(choices=[])]), True)

    def test_project_roundtrip_real_read_and_client_history(self):
        with tempfile.TemporaryDirectory(dir=os.environ["Q4T_TEST_OUTPUT_DIR"]) as tmp:
            run = v.Run(dict(started=time.time(), model_dir="unused", protocol_sha256="a" * 64),
                        Path(tmp) / "run")
            usage = dict(prompt_tokens=1, completion_tokens=1, total_tokens=2)
            message = dict(role="assistant", content=None, tool_calls=[dict(id="call-1",
                type="function", function=dict(name="read_project_record",
                arguments=json.dumps(dict(path="docs/MODEL.md", start_line=1, line_count=8))))])
            def completed(msg, finish):
                return v.wire.read_json(io.BytesIO(json.dumps(dict(id="reply", model="qwen3.8-flash-next",
                    object="chat.completion", choices=[dict(index=0, message=msg,
                    finish_reason=finish)], usage=usage)).encode()))
            first = completed(message, "tool_calls")
            def reply(folder, label, body):
                if label == "project-read":
                    return first
                self.assertEqual(body["tool_choice"], "none")
                self.assertEqual(body["messages"][-1]["tool_call_id"], "call-1")
                record = json.loads(body["messages"][-1]["content"])
                self.assertIn("Qwen", record["source"]["text"])
                return completed(dict(role="assistant", content="Qwen " +
                    record["source"]["citation"]), "stop")
            with patch.object(run, "success", side_effect=reply):
                run.project_loop(run.out)

    def test_negative_matrix_and_budget_are_fixed(self):
        run = object.__new__(v.Run)
        seen = []
        with patch.object(run, "sdk", return_value=None), patch.object(run, "wait_slot"), \
             patch.object(run, "sdk_chat", side_effect=lambda client, folder, label, body,
                 error_status: seen.append((label, body, error_status))):
            run.negatives(Path("unused"))
        self.assertEqual(len(seen), 12)
        self.assertEqual(len({label for label, _, _ in seen}), 12)
        self.assertTrue(all(status == 400 for _, _, status in seen))
        self.assertEqual(v.READ_TOOLS[0]["function"]["parameters"]["required"],
                         ["path", "start_line", "line_count"])

    def test_install_and_rollback_reject_missing_guard_before_any_write(self):
        stage.bind()
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
             patch.object(stage.a, "check") as check:
            with self.assertRaises(RuntimeError):
                stage.a.execute()
            with self.assertRaises(RuntimeError):
                stage.a.restore(Path("unused"))
            check.assert_not_called()

    def test_timeout_and_request_budget_stop_before_next_request(self):
        run = object.__new__(v.Run)
        run.installed = False
        run.deadline = time.time() + 60
        run.requests, run.negative, run.starts, run.capability_gets = 16, 12, 2, 2
        run.budget()
        with self.assertRaises(AssertionError):
            run.counted()
        run.requests = 16
        run.deadline = time.time() - 1
        with self.assertRaises(AssertionError):
            run.budget()


if __name__ == "__main__":
    unittest.main()
