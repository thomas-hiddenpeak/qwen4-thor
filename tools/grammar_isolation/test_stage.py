"""Offline acceptance checks: telemetry semantics and safe cancellation."""
import json
from pathlib import Path
import signal
import time
import unittest
from unittest.mock import Mock, patch

import stage
v = stage.module("grammar_checks_test", Path(__file__).with_name("verify.py"))


def sample_metrics():
    lines = []
    for legacy, current in [("q4t_queue_seconds", "q4t_admission_seconds"),
                            ("q4t_e2e_seconds", "q4t_generation_complete_seconds")]:
        lines.append("# HELP " + legacy + " Deprecated alias")
        for name in (legacy, current):
            lines += [name + "_count 1", name + "_sum 0.5"]
    lines += ['q4t_requests_aborted_by_reason_total{reason="' + reason + '"} ' +
              ("1" if reason == "explicit_cancel" else "0") for reason in sorted(v.r.REASONS)]
    lines.append("q4t_requests_aborted_total 1")
    return "\n".join(lines)


class StageContracts(unittest.TestCase):
    def test_metrics_alias_and_fixed_reason_accounting(self):
        text = sample_metrics()
        v.r.metric_contract(text)
        for bad in [text.replace("q4t_queue_seconds_sum 0.5", "q4t_queue_seconds_sum 0.6"),
                    text.replace('reason="other"', 'reason="user-id"'),
                    text.replace("q4t_requests_aborted_total 1", "q4t_requests_aborted_total 2"),
                    text.replace("Deprecated alias", "Misleading old HELP"),
                    text + "\nq4t_requests_aborted_total 1"]:
            with self.assertRaises(AssertionError):
                v.r.metric_contract(bad)

    def test_cancel_json_and_sse_cannot_succeed(self):
        error = dict(error=dict(code="explicit_cancel"))
        body = json.dumps(error).encode()
        v.r.cancellation_wire(b"HTTP/1.1 409 Conflict\r\n\r\n" + body, "explicit_cancel")
        raw = b"HTTP/1.1 200 OK\r\n\r\ndata: " + body + b"\n\ndata: [DONE]\n\n"
        v.r.cancellation_wire(raw, "explicit_cancel")
        for bad in [raw.replace(b"[DONE]", b"missing"),
                    raw.replace(b"explicit_cancel", b"deadline_exceeded"),
                    raw.replace(b"data: [DONE]", b'data: {"choices":[{"finish_reason":"stop"}]}\n\ndata: [DONE]'),
                    b""]:
            with self.assertRaises(AssertionError):
                v.r.cancellation_wire(bad, "explicit_cancel")

    def test_signal_targets_only_the_captured_process_descriptor(self):
        run = object.__new__(v.Run)
        run.server, run.server_fd = Mock(), 42
        run.server.poll.return_value = None
        with patch.object(v.signal, "pidfd_send_signal") as send:
            run.stop()
            send.assert_called_once_with(42, signal.SIGTERM)
            run.server.poll.return_value = 0
            run.stop()
            self.assertEqual(send.call_count, 1)

    def test_frozen_budget_and_deadline(self):
        run = object.__new__(v.Run)
        run.installed = False
        run.deadline = time.time() + 60
        run.requests, run.negative, run.starts, run.capability_gets = 8, 1, 1, 1
        run.budget()
        with self.assertRaises(AssertionError):
            run.counted()
        run.requests = 8
        run.deadline = time.time() - 1
        with self.assertRaises(AssertionError):
            run.budget()

    def test_long_request_is_bounded_and_explicitly_cancellable(self):
        body = v.r.long_request("resource-owned")
        self.assertEqual(body["request_id"], "resource-owned")
        self.assertEqual(len(body["cancel_token"]), 64)
        self.assertLess(body["max_completion_tokens"], 8192)
        shape = body["response_format"]["json_schema"]["schema"]
        self.assertEqual(shape["properties"]["xs"]["maxItems"], 64)
        self.assertEqual(len(shape["properties"]["xs"]["items"]["enum"][0]), 256)

    def test_worker_accounting_rejects_leaks_and_wrong_memory_scope(self):
        rows = dict(q4t_structured_workers_active=0,
            q4t_structured_workers_started_total=2,
            q4t_structured_workers_reaped_total=2,
            q4t_structured_worker_max_rss_bytes=1024**2)
        v.worker_contract(rows)
        for name, value in [("q4t_structured_workers_active", 1),
                            ("q4t_structured_workers_reaped_total", 1),
                            ("q4t_structured_worker_max_rss_bytes", 3 * 1024**3)]:
            with self.assertRaises(AssertionError):
                v.worker_contract(dict(rows, **{name: value}))

    def test_installation_requires_shared_guard(self):
        stage.bind()
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
             patch.object(stage.a, "check") as checked:
            with self.assertRaises(RuntimeError):
                stage.a.execute()
            checked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
