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

    def test_corrected_budget_charges_failed_generation_and_all_preflights(self):
        run = self.run_stub()
        run.plan = dict(prior_counts=dict(starts=1, generations=8, negatives=15,
                                         preflights=23))
        run.requests, run.negative, run.preflights = 2, 5, 7
        run.starts = run.capability_gets = 1
        run.budget()
        run.requests = 3
        with self.assertRaises(AssertionError):
            run.budget()
        run.requests, run.preflights = 2, 8
        with self.assertRaises(AssertionError):
            run.budget()
        self.assertEqual(stage.guard_command(True)[-1], "--corrected")

    def test_retained_failure_and_same_runtime_are_exact(self):
        prior = stage.BASE / "execution"
        failure = stage.h.read(prior / "business/result.json")
        self.assertEqual(failure["error"],
                         'AttributeError("\'NoneType\' object has no attribute \'strip\'")')
        self.assertEqual([failure[k] for k in ("request_attempts", "negative_requests",
            "preflight_requests", "starts")], [8, 15, 23, 1])
        self.assertEqual(stage.h.sha(stage.BASE / "engine/q4t"),
                         stage.h.read(stage.BASE / "package/plan.json")["candidate_sha256"])

    def test_reuse_requires_every_original_file_identity(self):
        files = list((stage.BASE / "execution/business/candidate-mtp").iterdir())
        self.assertTrue(files)
        for path in files:
            if path.is_file():
                identity = stage.h.identity(path)
                self.assertEqual(stage.h.sha(path), identity["sha256"])

    def test_multi_slot_wait_requires_all_slots_and_owners_released(self):
        run = self.run_stub()
        health = lambda free: (200, json.dumps(dict(gpu_healthy=True,
            seq_slots_free=free, seq_slots_total=2)))
        with patch.object(run, "request", side_effect=[health(1), health(2)]) as request, \
             patch.object(run, "observe") as observe:
            run.wait_slot("two-slots")
            self.assertEqual(request.call_count, 2)
            predicate = observe.call_args.args[0]
            rows = dict(q4t_active_chats=0, q4t_structured_active=0,
                        q4t_structured_workers_active=0)
            self.assertTrue(predicate(rows))
            rows["q4t_structured_workers_active"] = 1
            self.assertFalse(predicate(rows))
        with patch.object(run, "request", return_value=health(1)), \
             patch.object(v.time, "monotonic", side_effect=[0, 4]):
            with self.assertRaises(AssertionError):
                run.wait_slot("still-occupied")

    def test_final_budget_charges_both_failures_without_health_model_start(self):
        for attribute in ("requests", "negative", "preflights", "starts", "capability_gets"):
            run = self.run_stub()
            run.plan = dict(combined_plain=True, prior_counts=dict(starts=2,
                generations=8, negatives=15, preflights=24))
            run.requests, run.negative, run.preflights = 4, 5, 8
            run.starts = run.capability_gets = 1
            run.budget()
            setattr(run, attribute, getattr(run, attribute) + 1)
            with self.assertRaises(AssertionError):
                run.budget()
        self.assertEqual(stage.guard_command(final=True)[-1], "--final")
        failure = stage.h.read(stage.BASE / "execution-corrected/business/result.json")
        self.assertEqual([failure[k] for k in ("request_attempts", "negative_requests",
            "preflight_requests", "starts")], [0, 0, 1, 1])

    def test_installed_health_routes_without_model_verifier(self):
        from unittest.mock import Mock
        health = Mock()
        plan = dict(combined_plain=True)
        with patch.object(stage, "module", return_value=health) as load:
            stage.verifier(plan, installed=True)
            self.assertEqual(load.call_args.args[1].name, "installed_health.py")
            health.run.assert_called_once_with(plan, stage.OUTPUT)
            health.Run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
