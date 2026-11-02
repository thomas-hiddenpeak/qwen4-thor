"""Offline acceptance controls; real generation remains a separate guarded stage."""
import copy
import io
import json
from pathlib import Path
import time
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import stage
v = stage.module("text_stop_driver_checks", Path(__file__).with_name("verify.py"))
import check_schema


class Contracts(unittest.TestCase):
    def test_launch_uses_absolute_guard_identity(self):
        command = stage.guard_command(True)
        self.assertEqual(command[2], str(stage.ROOT / "tools/evalscope/gpu_exclusive.py"))
        self.assertEqual(command[4], str(stage.OUTPUT / "lease"))
        self.assertEqual(command[-2:], ["execute", "--guard-retry"])
        self.assertTrue(Path(command[-3]).is_absolute())
        self.assertEqual(command[6], str(stage.SDK_PYTHON))

    def test_sdk_retry_charges_prior_start(self):
        run = object.__new__(v.Run)
        run.installed, run.deadline = False, time.time() + 60
        run.plan = dict(prior_counts=dict(starts=1))
        run.requests, run.negative, run.starts, run.capability_gets = 20, 16, 2, 2
        run.budget()
        run.starts = 3
        with self.assertRaises(AssertionError):
            run.budget()

    def test_fixture_reuse_keeps_total_and_installed_plain_budget(self):
        run = object.__new__(v.Run)
        run.installed, run.deadline = False, time.time() + 60
        run.plan = dict(prior_counts=dict(starts=2, generations=8, negatives=16),
                        installed_plain=True)
        run.requests, run.negative, run.starts, run.capability_gets = 11, 0, 1, 1
        run.budget()
        run.requests = 12
        with self.assertRaises(AssertionError):
            run.budget()
        run.installed, run.requests = True, 3
        run.budget()
        run.requests = 4
        with self.assertRaises(AssertionError):
            run.budget()
        command = stage.guard_command(fixture_retry=True)
        self.assertEqual(command[-1], "--fixture-retry")

    def test_installed_health_checks_plain_and_final_trace(self):
        run = object.__new__(v.Run)
        run.plan = dict(installed_plain=True)
        result = SimpleNamespace(message=dict(content="ALPHA0123"), finish_reason="stop")
        with patch.object(run, "plain") as plain, patch.object(run, "success", return_value=result), \
             patch.object(run, "resources_empty"), patch.object(run, "traces") as traces:
            run.health(Path("/bounded"))
            plain.assert_called_once_with(Path("/bounded"))
            traces.assert_called_once_with(Path("/bounded"), mtp=False)

    def test_missing_sdk_precedes_model_start(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            run = object.__new__(v.Run)
            run.out, run.installed = Path(directory), False
            run.plan = {}
            run.requests = run.negative = run.starts = 0
            run.started = time.monotonic()
            with patch.object(run, "sdk", side_effect=RuntimeError("missing SDK")), \
                 patch.object(run, "model") as model:
                with self.assertRaises(RuntimeError):
                    run.run()
                model.assert_not_called()
                result = json.loads((run.out / "result.json").read_text())
                self.assertEqual(result["starts"], 0)

    def test_guard_retry_keeps_first_deadline(self):
        with patch.object(v.b.Run, "__init__", return_value=None):
            run = v.Run(dict(started=2000, original_started=1000), Path("/unused"))
            self.assertEqual(run.deadline, 2770)

    def test_shape_byte_limits_and_neutral_cache(self):
        for stop in (None, [], "x" * 256, ["中" * 85, "x", "y", "z"]):
            check_schema.check_request(dict(stop=stop))
        for stop in ("", True, "中" * 86, "x" * 257, ["x"] * 5, [None], "\ud800"):
            with self.assertRaises(Exception):
                check_schema.check_request(dict(stop=stop))
        check_schema.check_request(dict(stop=[], session_id="a" * 64))
        with self.assertRaises(Exception):
            check_schema.check_request(dict(stop="x", session_id="a" * 64))

    def test_negative_cases_and_fixed_limits(self):
        cases = v.negative_cases()
        self.assertEqual(len(cases), 16)
        self.assertEqual(stage.LIMITS, dict(starts=4, generations=22, negatives=16, seconds=1800))
        self.assertTrue(all(param.startswith("stop") for _, param in cases))
        self.assertEqual(v.copying()["stop"], "456789END")
        self.assertNotIn("stop", v.copying(stop=None))

    def test_budget_reserves_installed_start_and_request(self):
        run = object.__new__(v.Run)
        run.installed, run.deadline = False, time.time() + 60
        run.requests, run.negative, run.starts, run.capability_gets = 21, 16, 3, 3
        run.budget()
        with self.assertRaises(AssertionError):
            run.counted()
        run.requests, run.starts = 21, 4
        with self.assertRaises(AssertionError):
            run.budget()
        run.starts, run.deadline = 3, time.time() - 1
        with self.assertRaises(AssertionError):
            run.budget()

    def test_guard_precedes_mutation(self):
        stage.bind()
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
             patch.object(stage.a, "check") as checked:
            with self.assertRaises(RuntimeError):
                stage.a.execute()
            checked.assert_not_called()

    def test_trace_joins_independent_completion_identity(self):
        log = "[q4t][http] trace_id=trace-ascii completion_id=chatcmpl-xyz\n" + \
              "[text-stop] request=chatcmpl-xyz matched_index=1 tokens=8 mtp_steps=3 mtp_tokens=10 mtp_discarded=2\n"
        self.assertEqual(v.stop_traces(log)["ascii"]["mtp_discarded"], 2)
        with self.assertRaises(AssertionError):
            v.stop_traces(log.split("\n", 1)[1])
        with self.assertRaises(AssertionError):
            v.stop_traces(log + log)

    def test_tool_error_rejects_executable_or_successful_partial(self):
        error = dict(error=dict(message="incomplete tool", type="server_error",
                               code="response_parse_failed", param=None))
        v.truncated_tool(502, json.dumps(error).encode(), False)
        raw = b'data: {"choices":[{"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
        tail = b"data: " + json.dumps(error).encode() + b"\n\ndata: [DONE]\n\n"
        v.truncated_tool(200, raw + tail, True)
        for bad in (raw.replace(b'"finish_reason":null', b'"finish_reason":"stop"'),
                    raw.replace(b'"content":""', b'"tool_calls":[{}]')):
            with self.assertRaises(AssertionError):
                v.truncated_tool(200, bad + tail, True)
        with self.assertRaises(AssertionError):
            v.truncated_tool(200, raw + tail + b"data: [DONE]\n\n", True)

    def test_real_model_command_uses_cpu_preflight(self):
        run = object.__new__(v.Run)
        run.out = Path("/bounded")
        run.plan = dict(startup_preflight=dict(path="/probe", sha256="a"))
        def delegated(_self, name, binary, mtp, action):
            argv = v.b.command(binary, "model", 18099, mtp)
            self.assertEqual(argv[:2], ["candidate", "serve"])
            self.assertNotIn("--session-cache-dir", argv)
            self.assertIn("--no-mtp", argv)
        with patch.object(v.b.Run, "model", delegated), \
             patch.object(v.wire, "digest", return_value="a"), \
             patch.object(v.wire, "save"), \
             patch.object(v.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout="", stderr="")) as probe:
            run.model("candidate-plain", Path("candidate"), False, lambda _: None)
            self.assertEqual(probe.call_args.args[0][0], "/probe")

    def test_project_loop_preserves_frozen_sse_despite_legacy_null_defaults(self):
        run = object.__new__(v.Run)
        body = dict(stream=None, stream_options=None)
        with patch.object(v.b.Run, "success") as saved:
            run.success(Path("/bounded"), "project-read", body)
            passed = saved.call_args.args[-1]
            self.assertIs(passed["stream"], True)
            self.assertEqual(passed["stream_options"], dict(include_usage=True))
            self.assertEqual(passed["stop"], "NEVER_STOP_24c3")


if __name__ == "__main__":
    unittest.main()
