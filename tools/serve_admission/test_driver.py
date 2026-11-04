"""CPU-only fixture/guard/evidence gates; no network or model startup."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from tools.serve_admission import stage, verify


class DriverTests(unittest.TestCase):
    def test_same_source_build_and_fault_separation(self):
        runtime = stage.build_gate()
        self.assertIn("src/server/chat_admission.cpp", runtime)
        self.assertNotEqual(stage.h.sha(stage.BASE / "engine/q4t"),
                            stage.h.sha(stage.BASE / "fault-engine/q4t"))

    def test_request_shapes_and_bounded_payload(self):
        validators = verify.load_validators()
        bodies = [verify.ordinary(), verify.ordinary(large=True),
                  verify.constrained(), verify.constrained(hold=True, stream=True),
                  verify.constrained(large=True, stream=True)]
        for body in bodies:
            validators["chat"].validate(body)
            self.assertEqual(body["request_timeout_ms"], 180000)
            self.assertLessEqual(body["max_tokens"], 4096)
        validators["preflight"].validate(dict(request=bodies[0]))
        schema = bodies[3]["response_format"]["json_schema"]["schema"]["properties"]["data"]
        self.assertEqual((schema["minItems"], schema["maxItems"]), (64, 64))

    def test_real_startup_cli_parses_without_loading(self):
        fixtures = []
        for fault in (False, True):
            args = ["--host", "127.0.0.1", "--port", "18101", "--max-prefill", "2048",
                "--max-len", "8192", "--max-seq", "2" if fault else "1",
                "--max-inflight-requests", "8" if fault else "1", "--max-queued-requests",
                "2" if fault else "0"]
            if fault:
                args += ["--no-mtp", "--seq-capacities", "8192,2048"]
            fixtures.append(dict(argv=args, effective_slots=2 if fault else 1))
        binary = stage.BASE / "cpu/config-probe/config_probe"
        output = subprocess.run([str(binary)], input="".join(json.dumps(f)+"\n" for f in fixtures),
            text=True, capture_output=True, timeout=10, check=True)
        self.assertFalse(output.stderr)
        results = [json.loads(line) for line in output.stdout.splitlines()]
        self.assertEqual(len(results), 2)
        for row, limits in zip(results, ([1, 0], [8, 2])):
            self.assertTrue(row["accepted"])
            self.assertEqual([row["configuration"]["limits"][key] for key in
                              ("text_requests", "queued_requests")], limits)

    @staticmethod
    def error(stream=False):
        value = {"error": {"message": "full", "type": "server_error",
                            "code": "queue_capacity_exhausted", "param": None}}
        raw = json.dumps(value)
        if stream:
            raw = "data: " + raw + "\n\ndata: [DONE]\n\n"
        return dict(status=200 if stream else 503, raw=raw,
                    headers={"Content-Type": "text/event-stream" if stream else "application/json"})

    def test_json_rejection_exact_code(self):
        verify.error_contract(self.error(), 503, "queue_capacity_exhausted")
        with self.assertRaises(ValueError):
            verify.error_contract(self.error(), 503, "capacity_exhausted")

    def test_sse_error_is_logical_rejection_after_200(self):
        verify.error_contract(self.error(True), 200, "queue_capacity_exhausted", stream=True)
        with self.assertRaises(ValueError):
            verify.error_contract(self.error(True), 503, "queue_capacity_exhausted", stream=True)

    def test_sse_rejects_duplicate_or_missing_terminal(self):
        for suffix in ("data: [DONE]\n\n", ""):
            value = self.error(True)
            value["raw"] = value["raw"]+suffix if suffix else value["raw"].replace("data: [DONE]\n\n", "")
            with self.assertRaises(ValueError):
                verify.error_contract(value, 200, "queue_capacity_exhausted", stream=True)

    def test_rejection_cannot_contain_success(self):
        value = self.error(True)
        value["raw"] = 'data: {"choices":[{"finish_reason":"stop"}]}\n\n'+value["raw"]
        with self.assertRaises(ValueError):
            verify.error_contract(value, 200, "queue_capacity_exhausted", stream=True)

    def test_rejection_cannot_end_before_error(self):
        value = self.error(True)
        value["raw"] = "data: [DONE]\n\n"+value["raw"].replace("data: [DONE]\n\n", "")
        with self.assertRaises(ValueError):
            verify.error_contract(value, 200, "queue_capacity_exhausted", stream=True)

    def test_sse_initial_role_before_error_is_not_success(self):
        value = self.error(True)
        role = dict(object="chat.completion.chunk", usage=None, choices=[dict(index=0,
            delta=dict(role="assistant"), finish_reason=None)])
        value["raw"] = "data: "+json.dumps(role)+"\n\n"+value["raw"]
        verify.error_contract(value, 200, "queue_capacity_exhausted", stream=True)

    def test_sse_role_cannot_hide_generated_content(self):
        value = self.error(True)
        role = dict(object="chat.completion.chunk", usage=None, choices=[dict(index=0,
            delta=dict(role="assistant", content="answer"), finish_reason=None)])
        value["raw"] = "data: "+json.dumps(role)+"\n\n"+value["raw"]
        with self.assertRaises(ValueError):
            verify.error_contract(value, 200, "queue_capacity_exhausted", stream=True)

    def test_absolute_guard_and_distinct_stage(self):
        command = stage.guard_command()
        self.assertTrue(Path(command[2]).is_absolute())
        self.assertEqual(command[4], str(stage.OUTPUT / "lease"))
        self.assertIn("serve-admission", command[4])
        self.assertEqual(stage.PLANNED, dict(generations=10, negatives=4, starts=2))
        self.assertEqual(stage.LIMITS, dict(generations=12, negatives=8, starts=3, seconds=1800))

    def test_budget_refuses_extra_attempt_before_recording(self):
        runner = object.__new__(verify.Run)
        runner.deadline = time.time()+10
        runner.counts = stage.LIMITS.copy()
        with self.assertRaises(ValueError):
            runner.budget("generations")
        self.assertEqual(runner.counts["generations"], 12)
        runner.deadline = time.time()-1
        with self.assertRaises(ValueError):
            runner.budget()

    def test_cancel_does_not_accept_success(self):
        pending = object.__new__(verify.Pending)
        class CompletedThread:
            def join(self, timeout): pass
            def is_alive(self): return False
        pending.thread, pending.label = CompletedThread(), "fixture"
        pending.value, pending.error = object(), None
        with self.assertRaises(ValueError):
            pending.finish(cancelled=True)
        pending.value = None
        pending.error = verify.RemoteError(409, {"error": {"code": "explicit_cancel"}})
        pending.finish(cancelled=True)


if __name__ == "__main__":
    unittest.main()
