"""Offline acceptance-driver bounds and failure propagation; no model/HTTP."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import stage
v = stage.module("preflight_driver_contracts", Path(__file__).with_name("verify.py"))
import check_schema


class Contracts(unittest.TestCase):
    def run_stub(self):
        run = object.__new__(v.Run)
        run.installed = False
        run.deadline = time.time() + 60
        run.requests, run.negative, run.preflights = 10, 20, 30
        run.starts = run.capability_gets = 2
        return run

    def test_exact_guard_argv_and_sdk_environment(self):
        args = stage.guard_command()
        self.assertEqual(args[2], str(stage.ROOT / "tools/evalscope/gpu_exclusive.py"))
        self.assertEqual(args[4], str(stage.OUTPUT / "lease"))
        self.assertEqual(args[6], str(stage.SDK_PYTHON))
        self.assertEqual(args[-2:], [str(Path(stage.__file__).resolve()), "execute"])

    def test_hard_budget_reserves_installed_requests_and_no_reset(self):
        for attribute in ("requests", "negative", "preflights", "starts", "capability_gets"):
            run = self.run_stub()
            run.budget()
            setattr(run, attribute, getattr(run, attribute) + 1)
            with self.assertRaises(AssertionError):
                run.budget()
        run = self.run_stub()
        run.deadline = time.time() - 1
        with self.assertRaises(AssertionError):
            run.budget()
        with patch.object(v.b.Run, "__init__", return_value=None):
            self.assertEqual(v.Run(dict(started=2000, original_started=1000),
                                   Path("/unused")).deadline, 2770)

    def test_guard_precedes_any_mutation(self):
        stage.bind()
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
             patch.object(stage.a, "check") as checked:
            with self.assertRaises(RuntimeError):
                stage.a.execute()
            checked.assert_not_called()

    def test_missing_sdk_stops_before_model_start(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            run = self.run_stub()
            run.out, run.plan, run.checked = Path(directory), {}, []
            run.started = time.monotonic()
            run.requests = run.negative = run.preflights = run.starts = 0
            with patch.object(run, "sdk", side_effect=RuntimeError("no SDK")), \
                 patch.object(run, "model") as model:
                with self.assertRaises(RuntimeError):
                    run.run()
                model.assert_not_called()
            state = json.loads((run.out / "result.json").read_text())
            self.assertEqual(state["starts"], 0)

    def test_flattening_preserves_actual_request_without_sdk_headers(self):
        body = dict(messages=[dict(role="user", content="q")], stream=True,
            extra_body=dict(enable_thinking=False), extra_headers={"X-Request-Id": "trace"})
        result = v.flattened(body)
        self.assertEqual(result, dict(messages=body["messages"], stream=True,
                                     enable_thinking=False))
        self.assertIn("extra_body", body)
        self.assertEqual(len(v.negative_cases()), 8)

    def test_cli_preflight_failure_stops_before_parent_model(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            run = self.run_stub()
            run.out = Path(directory)
            run.args = type("Args", (), dict(model_dir="/model", port=18099))()
            run.plan = dict(startup_preflight=dict(path="/probe", sha256="hash"))
            with patch.object(v.wire, "digest", return_value="hash"), \
                 patch.object(v.subprocess, "run", return_value=type("Checked", (),
                    dict(returncode=2, stdout="", stderr="options"))()), \
                 patch.object(v.b.Run, "model") as model:
                with self.assertRaises(AssertionError):
                    run.model("candidate-explicit", Path("/binary"), True, lambda _: None)
                model.assert_not_called()
            argv = json.loads((run.out / "candidate-explicit-startup.json").read_text())["argv"]
            self.assertEqual(argv[argv.index("--max-seq") + 1], "2")
            self.assertIn("2048,8192", argv)

    def test_schema_catches_count_mismatch_and_false_reservation(self):
        path = stage.BASE / "cpu/template-diff/native.jsonl"
        value = next(json.loads(r) for r in path.read_text().splitlines()
                     if '"object":"q4t.chat.preflight"' in r)
        schemas = check_schema.validators()
        self.assertEqual(check_schema.check_value(value, schemas), "preflight")
        value["prompt_tokens"] += 1
        with self.assertRaises(AssertionError):
            check_schema.check_value(value, schemas)
        value["prompt_tokens"] -= 1
        value["admission"]["slot_reserved"] = True
        with self.assertRaises(Exception):
            check_schema.check_value(value, schemas)


if __name__ == "__main__":
    unittest.main()
