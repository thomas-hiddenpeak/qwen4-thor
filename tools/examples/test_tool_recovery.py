"""Offline crash-boundary contracts. No requests or tool dispatch."""
from copy import deepcopy
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import chat_client as client
import tool_recovery as recovery
from test_chat_client import CALL, MESSAGE, capabilities, whole


class RecoveryContracts(unittest.TestCase):
    def setUp(self):
        network = patch("urllib.request.OpenerDirector.open",
                        side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        root = Path(os.environ.get("Q4T_TEST_OUTPUT_DIR",
                                   recovery.ROOT / "build/tool-recovery-contracts"))
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        with recovery.journal_lock(self.directory, create=True):
            pass
        self.history = [dict(role="user", content="lookup")]
        self.start = dict(journal_version=1, initial_messages=self.history,
            max_requests=4, max_tool_calls=8, model="qwen3.8-flash-next")
        self.save("run-started.json", self.start)
        self.save("capabilities.json", capabilities())
        self.request = client.make_request(self.history, [], stream=False)

    def save(self, name, value):
        recovery.save_receipt(self.directory / name, value)

    def inspect(self):
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        result = recovery.inspect_recovery(self.directory)
        self.assertFalse(result["automatic_retry"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})
        return result

    def submitted(self):
        self.save("round-00-request.json", self.request)
        self.save("round-00-submitted.json", dict(request_id=self.request["request_id"]))

    def response(self, message=MESSAGE, finish="tool_calls"):
        self.submitted()
        raw = json.dumps(whole(message, finish, self.request["request_id"])).encode()
        (self.directory / "round-00-wire.bin").write_bytes(raw)
        result = client.read_json(io.BytesIO(raw))
        self.save("round-00-response.json", asdict(result))
        return result

    def planned(self, message=MESSAGE):
        result = self.response(message)
        plan = [asdict(call) for call in client.prepare_tool_calls(result,
                                                           {"lookup": lambda a: None})]
        self.save("round-00-plan.json", plan)
        return result, plan

    def test_no_request_or_submission_is_not_an_unknown_execution(self):
        self.assertEqual(self.inspect()["state"], "READY_TO_REQUEST")
        self.save("round-00-request.json", self.request)
        self.assertEqual(self.inspect()["state"], "REQUEST_NOT_SUBMITTED")

    def test_submitted_without_response_is_unknown_even_with_wire(self):
        self.submitted()
        (self.directory / "round-00-wire.bin").write_bytes(b"data: partial")
        result = self.inspect()
        self.assertEqual(result["state"], "REQUEST_OUTCOME_UNKNOWN")
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["messages"], self.history)

    def test_null_request_receipts_cannot_erase_submission(self):
        self.submitted()
        for name in ("capabilities.json", "round-00-request.json",
                     "round-00-submitted.json"):
            with self.subTest(receipt=name):
                path = self.directory / name
                original = path.read_bytes()
                try:
                    path.write_text("null\n")
                    result = self.inspect()
                    self.assertEqual(result["state"], "RECORDS_INVALID")
                    self.assertIn("null receipt: " + name, result["error"])
                finally:
                    path.write_bytes(original)

    def test_length_never_commits_or_executes_calls(self):
        self.response(finish="length")
        result = self.inspect()
        self.assertEqual(result["state"], "LENGTH_LIMIT")
        self.assertEqual(result["messages"], self.history)

    def test_started_tool_without_result_is_unknown(self):
        _, plan = self.planned()
        self.assertEqual(self.inspect()["state"], "TOOLS_PENDING")
        self.save("round-00-tool-000-started.json", plan[0])
        result = self.inspect()
        self.assertEqual(result["state"], "TOOL_OUTCOME_UNKNOWN")
        self.assertEqual(result["attempted_tool_calls"], 1)
        self.assertEqual(result["remaining_tool_calls"], 7)
        self.assertEqual(result["messages"], self.history)

    def test_null_tool_receipts_cannot_make_started_call_pending(self):
        _, plan = self.planned()
        self.save("round-00-tool-000-started.json", plan[0])
        for name in ("round-00-response.json", "round-00-plan.json",
                     "round-00-tool-000-started.json"):
            with self.subTest(receipt=name):
                path = self.directory / name
                original = path.read_bytes()
                try:
                    path.write_text("null\n")
                    result = self.inspect()
                    self.assertEqual(result["state"], "RECORDS_INVALID")
                    self.assertIn("null receipt: " + name, result["error"])
                finally:
                    path.write_bytes(original)

    def test_durable_results_reconstruct_missing_history(self):
        response, plan = self.planned()
        self.save("round-00-tool-000-started.json", plan[0])
        self.save("round-00-tool-000-result.json",
                  dict(tool_call_id=CALL["id"], content='{"code":"831907"}'))
        result = self.inspect()
        self.assertEqual(result["state"], "READY_TO_REQUEST")
        self.assertEqual(result["completed_tool_calls"], 1)
        self.assertEqual(result["remaining_requests"], 3)
        self.assertEqual(result["messages"], client.append_tool_results(
            self.history, response, {CALL["id"]: '{"code":"831907"}'}))

    def test_partial_batch_preserves_known_results_without_replay(self):
        message = deepcopy(MESSAGE)
        second = deepcopy(CALL)
        second["id"] = "second"
        message["tool_calls"].append(second)
        _, plan = self.planned(message)
        for i in range(2):
            self.save(f"round-00-tool-{i:03d}-started.json", plan[i])
        self.save("round-00-tool-000-result.json", dict(tool_call_id=CALL["id"],
                                                        content="known"))
        result = self.inspect()
        self.assertEqual(result["state"], "TOOL_OUTCOME_UNKNOWN")
        self.assertEqual(result["known_results"], {CALL["id"]: "known"})
        self.assertEqual([c["call_id"] for c in result["pending_tool_calls"]], ["second"])

    def test_final_completion_requires_consistent_receipts(self):
        response = self.response(dict(role="assistant", content="answer"), "stop")
        self.assertEqual(self.inspect()["state"], "FINAL_RESPONSE_SAVED")
        history = client.append_final(self.history, response)
        self.save("round-00-history.json", history)
        self.save("completed.json", dict(requests=1, tool_calls=0, messages=history))
        self.assertEqual(self.inspect()["state"], "COMPLETED")

    def test_active_writer_blocks_inspection(self):
        with (self.directory / ".journal.lock").open("r") as writer:
            recovery.fcntl.flock(writer, recovery.fcntl.LOCK_EX | recovery.fcntl.LOCK_NB)
            self.assertEqual(self.inspect()["state"], "ACTIVE_RUN")

    def test_torn_unexpected_and_symlink_receipts_fail_closed(self):
        bad = self.directory / "failed.json"
        bad.write_bytes(b'{"automatic_retry":')
        self.assertEqual(self.inspect()["state"], "RECORDS_INVALID")
        bad.unlink()
        bad.symlink_to(self.directory / "run-started.json")
        self.assertEqual(self.inspect()["state"], "RECORDS_INVALID")
        bad.unlink()
        self.save("round-02-request.json", self.request)
        self.assertEqual(self.inspect()["state"], "RECORDS_INVALID")

    def test_result_cannot_exist_without_start_or_matching_wire(self):
        self.planned()
        self.save("round-00-tool-000-result.json", dict(tool_call_id=CALL["id"], content="x"))
        self.assertEqual(self.inspect()["state"], "RECORDS_INVALID")
        (self.directory / "round-00-tool-000-result.json").unlink()
        (self.directory / "round-00-wire.bin").write_bytes(b"{}")
        self.assertEqual(self.inspect()["state"], "RECORDS_INVALID")

    def test_receipts_are_no_replace_and_not_published_before_fsync(self):
        path = self.directory / "receipt.json"
        recovery.save_receipt(path, {"n": 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            recovery.save_receipt(path, {"n": 2})
        self.assertEqual(json.loads(path.read_text()), {"n": 1})
        other = self.directory / "not-published.json"
        with patch.object(recovery.os, "fsync", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                recovery.save_receipt(other, {"n": 1})
        self.assertFalse(other.exists())


if __name__ == "__main__":
    unittest.main()
