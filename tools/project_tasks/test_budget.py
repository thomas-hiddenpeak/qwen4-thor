"""Client budget compatibility and pre-generation rejection/recovery contracts."""
from copy import deepcopy
import json
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from tools.project_tasks import budget, engine as e, runner, store
from tools.project_tasks import test_tasks as old


class Budget(unittest.TestCase):
    setUp = old.Tasks.setUp
    count = old.Tasks.count
    execute = old.Tasks.execute
    status = old.Tasks.status
    break_at = old.Tasks.break_at
    child = old.Tasks.child

    def select(self, key, *, max_tokens=512, client_id="alpha"):
        self.binding = dict(old.BINDING, client_id=client_id)
        self.name = store.task_name(client_id, key)
        self.spec = e.specification(self.binding, "read current", stream=False, max_tokens=max_tokens)
        store.create(self.root, self.name, self.spec)

    def custom_client(self, function):
        fake = old.FakeClient()
        def preflight(body, *, sink):
            fake.preflight_calls.append(deepcopy(body))
            return function(body, sink, len(fake.preflight_calls))
        fake.preflight = preflight
        return fake

    def denied(self, code="client_output_limit_exceeded", param="max_tokens"):
        def response(body, sink, number):
            raw = dict(error=dict(type="invalid_request_error", code=code, param=param, message="bounded rejection"))
            sink(store.encode(raw))
            raise e.chat.RemoteError(400, raw)
        return self.custom_client(response)

    def test_configurable_output_is_saved_and_bound_to_key(self):
        for value in (True, 0, -1, 513, 64.0):
            with self.assertRaises(ValueError):
                e.specification(self.binding, "question", max_tokens=value)
        self.select("small", max_tokens=64, client_id="beta")
        fake = self.custom_client(lambda body, sink, count: self.accept(body, sink, output_limit=64))
        result = self.execute(fake)
        self.assertEqual((result["state"], result["max_tokens"], result["preflight_checks"]),
                         ("COMPLETED", 64, 2))
        self.assertTrue(all(v["max_tokens"] == 64 for v in fake.calls + fake.preflight_calls))
        self.assertEqual([v["messages"] for v in fake.calls], [v["messages"] for v in fake.preflight_calls])
        with self.assertRaisesRegex(ValueError, "conflicts"):
            runner.submit(self.root, self.binding, "small", "read current", fake, stream=False, max_tokens=512)
        self.assertEqual(len(fake.calls), 2)

    @staticmethod
    def accept(body, sink, **options):
        value = old.preflight_response(body, **options)
        sink(store.encode(value))
        return value

    def test_remote_rejection_is_known_before_generation_and_not_retried(self):
        fake = self.denied()
        result = self.execute(fake)
        self.assertEqual((result["state"], result["requests"], result["tool_attempts"],
                          result["preflight_checks"]), ("PREFLIGHT_REJECTED", 0, 0, 1))
        self.assertEqual(result["preflight_error"], dict(category="remote_error", http_status=400,
            code="client_output_limit_exceeded", param="max_tokens"))
        self.assertFalse(list((self.root / "tasks" / self.name).glob("wire-*.bin")))
        duplicate = old.FakeClient()
        again = runner.submit(self.root, self.binding, "key", "read current", duplicate, stream=False)
        self.assertEqual(again, dict(result, created=False))
        self.assertEqual((duplicate.cap_calls, len(duplicate.calls), len(duplicate.preflight_calls)), (0, 0, 0))
        saved = (self.root / "tasks" / self.name / "check-001.bin").read_bytes()
        done = self.execute()
        self.assertEqual((done["requests"], done["preflight_checks"], self.count()), (2, 3, 1))
        self.assertEqual((self.root / "tasks" / self.name / "check-001.bin").read_bytes(), saved)

    def test_every_round_checks_full_history_and_reuses_saved_tool_result(self):
        def respond(body, sink, number):
            if number == 1:
                return self.accept(body, sink)
            raw = dict(error=dict(type="invalid_request_error", code="client_prompt_limit_exceeded",
                                 param="messages", message="prompt budget"))
            sink(store.encode(raw))
            raise e.chat.RemoteError(400, raw)
        fake = self.custom_client(respond)
        result = self.execute(fake)
        self.assertEqual((result["state"], result["requests"], result["saved_tool_results"], self.count()),
                         ("PREFLIGHT_REJECTED", 1, 1, 1))
        self.assertEqual(fake.preflight_calls[1]["messages"][-1]["role"], "tool")
        (self.project / "docs/current.md").write_text("changed source\n")
        done = self.execute()
        self.assertEqual((done["state"], done["requests"], done["preflight_checks"], self.count()),
                         ("COMPLETED", 2, 3, 1))
        self.assertIn("initial value", done["answer"])

    def test_saved_approval_is_not_reused_after_resume(self):
        self.break_at("preflight_result")
        before = self.status()
        self.assertEqual((before["state"], before["requests"]), ("PREFLIGHT_APPROVED", 0))
        fake = self.denied()
        result = self.execute(fake)
        self.assertEqual((result["state"], result["requests"], result["preflight_checks"], self.count()),
                         ("PREFLIGHT_REJECTED", 0, 2, 0))
        with store.open_task(self.root, self.name) as ledger:
            planned = [v["payload"] for v in ledger.events() if v["kind"] == "preflight_started"]
        self.assertNotEqual(planned[0]["request_id"], planned[1]["request_id"])
        self.assertNotEqual(planned[0]["cancel_token"], planned[1]["cancel_token"])

    def test_transport_failure_does_not_create_generation_uncertainty(self):
        def disconnected(*args):
            raise ConnectionError("private response unavailable")
        fake = self.custom_client(disconnected)
        result = self.execute(fake)
        self.assertEqual((result["state"], result["requests"], result["preflight_error"]["category"]),
                         ("PREFLIGHT_REJECTED", 0, "transport_error"))
        self.assertEqual(len(fake.preflight_calls), 1)
        self.assertEqual(len(fake.calls), 0)
        done = self.execute()
        self.assertEqual((done["state"], done["requests"], done["preflight_checks"]), ("COMPLETED", 2, 3))

    def test_invalid_or_clipped_preflight_cannot_authorize_generation(self):
        edits = [lambda v: v.pop("client_limits"), lambda v: v.update(protocol_source_sha256="f" * 64),
            lambda v: v["output"].update(available_tokens=511),
            lambda v: v["output"].update(requested_tokens=64),
            lambda v: v["checks"].update(gpu_executed=True),
            lambda v: v["checks"].update(request_registered=True),
            lambda v: v["checks"].update(budget=1),
            lambda v: v["admission"].update(slot_reserved=True),
            lambda v: v["admission"].update(eligible_slots=[]),
            lambda v: v["client_limits"].update(max_output_tokens=64),
            lambda v: v.update(prompt="unexpected private prompt"),
            lambda v: v.update(execution_guaranteed=True)]
        for number, edit in enumerate(edits):
            with self.subTest(number=number):
                self.select("invalid-" + str(number))
                def invalid(body, sink, count):
                    value = old.preflight_response(body); edit(value)
                    sink(store.encode(value)); return value
                fake = self.custom_client(invalid)
                result = self.execute(fake)
                self.assertEqual((result["state"], result["requests"], len(fake.calls)),
                                 ("PREFLIGHT_REJECTED", 0, 0))
                self.assertEqual(result["preflight_error"]["category"], "invalid_response")

    def test_preflight_wire_and_submitted_body_must_match_committed_check(self):
        self.break_at("preflight_result")
        with store.open_task(self.root, self.name) as ledger:
            state = e.replay(ledger)
            altered = dict(state["planned_request"], max_tokens=64)
            with self.assertRaises(ValueError):
                e.validate_next(ledger, "request", altered)
        path = self.root / "tasks" / self.name / "check-001.bin"
        value = json.loads(path.read_bytes()); value["prompt_tokens"] += 1
        path.write_bytes(store.encode(value))
        with self.assertRaisesRegex(ValueError, "wire/receipt mismatch"):
            self.status()

    def test_explicit_attempt_budget_includes_failed_capability_checks(self):
        fake = old.FakeClient()
        with patch.object(fake, "capabilities", side_effect=ConnectionError("no capabilities")) as calls:
            for _ in range(16):
                with self.assertRaises(ConnectionError):
                    self.execute(fake)
            with self.assertRaisesRegex(ValueError, "resume budget"):
                self.execute(fake)
        self.assertEqual(calls.call_count, 16)
        state = self.status()
        self.assertEqual((state["runs"], state["requests"]), (16, 0))
        self.assertNotIn("resume", state["allowed_actions"])

    def test_resume_cannot_silently_ignore_budget_override(self):
        for action in ("resume", "inspect", "resolve"):
            with patch.object(sys, "argv", ["runner", action, "--root", str(self.root), "--max-tokens", "64"]):
                with self.assertRaises(runner.errors.ContractError) as caught:
                    runner.main()
                self.assertEqual(caught.exception.code, "invalid_arguments")

    def test_actual_process_death_after_approval_requires_a_new_check(self):
        process, marker = self.child("preflight_result")
        process.kill(); self.assertEqual(process.wait(5), -signal.SIGKILL)
        self.assertEqual((self.status()["state"], self.count()), ("PREFLIGHT_APPROVED", 0))
        result = subprocess.run([sys.executable, "-B", str(store.ROOT / "tools/project_tasks/test_worker.py"),
            str(self.root), self.name, str(self.project), "finish", str(self.directory / "finished.json")],
            cwd=store.ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        done = self.status()
        self.assertEqual((done["state"], done["requests"], done["preflight_checks"], self.count()),
                         ("COMPLETED", 2, 3, 1))

    def test_accepted_legacy_ledgers_remain_offline_verifiable(self):
        from tools.project_tasks import report
        self.assertTrue(report.verify()["passed"])


if __name__ == "__main__":
    unittest.main()
