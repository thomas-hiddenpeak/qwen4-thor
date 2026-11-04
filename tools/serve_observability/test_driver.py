"""CPU-only acceptance failure-path tests; no sockets or model execution."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.serve_observability import stage, verify


class DriverTests(unittest.TestCase):
    def test_builds_share_sources_and_separate_fault_macro(self):
        self.assertIn("include/q4t/server/http_observation.h", stage.build_gate())
        self.assertNotEqual(stage.h.sha(stage.BASE / "engine/q4t"),
                            stage.h.sha(stage.BASE / "fault-engine/q4t"))

    def test_planned_shapes_and_independent_limits(self):
        validators = verify.base.load_validators()
        for body in (verify.base.ordinary(), verify.base.constrained(),
                     verify.base.constrained(hold=True, stream=True), verify.slow_request()):
            validators["chat"].validate(body)
            self.assertLessEqual(body["max_tokens"], 1024)
            self.assertEqual(body["request_timeout_ms"], 180000)
        self.assertEqual(stage.PLANNED, dict(generations=8, negatives=16, starts=2))
        self.assertEqual(stage.LIMITS, dict(generations=10, negatives=20, starts=3, seconds=1800))

    def test_wire_lengths_and_read_phase(self):
        wire = verify.raw_request("x", b"POST /v1/chat/completions",
            b"Content-Type: application/json\r\nContent-Length: 2\r\n", b"x")
        head, payload = wire.split(b"\r\n\r\n", 1)
        self.assertEqual(payload, b"x")
        self.assertIn(b"Content-Length: 2", head)
        self.assertIn(b"X-Request-Id: srv01b.x", head)

    def test_metric_labels_reject_new_caller_values(self):
        rows = {verify.receive_key(r):0 for r in verify.RECEIVE}
        rows.update({verify.write_key(o,r):0 for o in ("handler", "heartbeat") for r in verify.WRITE})
        self.assertEqual(len(verify.transport_rows(rows)), 34)
        rows['q4t_http_write_failures_total{trace="caller"}'] = 1
        with self.assertRaises(ValueError):
            verify.transport_rows(rows)

    @staticmethod
    def prefix():
        value = dict(object="chat.completion.chunk", usage=None, choices=[
            dict(index=0, delta=dict(role="assistant", content="1"), finish_reason=None)])
        return b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\ndata: " + json.dumps(value).encode()+b"\r\n\r\n"

    def test_partial_write_never_becomes_a_complete_turn(self):
        result = verify.check_failed_stream(self.prefix()+b'data: {"ch')
        self.assertEqual(result["complete_events"], 1)
        self.assertFalse(result["complete_assistant_turn"])
        self.assertFalse(result["automatic_retry"])

    def test_failed_write_refuses_success_or_error_terminal(self):
        for tail in (b"data: [DONE]\r\n\r\n",
            b'data: {"choices":[{"finish_reason":"stop"}]}\r\n\r\n',
            b'data: {"error":{}}\r\n\r\n', b'data: {"usage":{}}\r\n\r\n'):
            with self.subTest(tail=tail), self.assertRaises(ValueError):
                verify.check_failed_stream(self.prefix()+tail)

    def test_body_capacity_error_code_and_param_are_required(self):
        raw = b'HTTP/1.1 503 unavailable\r\nX-Request-Id: test\r\n\r\n'+json.dumps({"error":
            dict(message="full", type="server_error", code="body_capacity_exhausted", param="body")}).encode()
        value, trace = verify.decode_error(raw, 503, "body_capacity_exhausted", "body")
        self.assertEqual(trace, "test")
        with self.assertRaises(ValueError):
            verify.decode_error(raw, 503, "capacity_exhausted", "body")
        with self.assertRaises(ValueError):
            verify.decode_error(raw, 503, "body_capacity_exhausted", None)

    def test_failure_preserves_received_bytes_without_replay(self):
        class Socket:
            def __init__(self): self.reads=0; self.closed=False
            def recv(self, size):
                self.reads += 1
                if self.reads == 1: return b"received-prefix"
                raise ConnectionResetError("expected fixture reset")
            def close(self): self.closed=True
        with tempfile.TemporaryDirectory(dir=stage.BASE) as tmp:
            run = object.__new__(verify.Run)
            run.folder = Path(tmp)
            sock = Socket()
            run.sockets = {sock: "failed"}
            with self.assertRaises(ConnectionResetError): run.read(sock, "failed")
            self.assertEqual((run.folder / "failed-response.bin").read_bytes(), b"received-prefix")
            self.assertTrue(sock.closed)
            self.assertEqual(sock.reads, 2)
            self.assertFalse(run.sockets)

    def test_budget_refuses_overrun_before_dispatch(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as tmp:
            run = object.__new__(verify.Run)
            run.out, run.deadline = Path(tmp), 1e20
            run.counts = dict(generations=10, negatives=20, starts=3)
            for key in run.counts:
                with self.assertRaises(ValueError): run.budget(key)
            self.assertFalse(list(run.out.iterdir()))

    def test_rollback_checks_seal_not_current_workspace(self):
        plan = dict(root=str(stage.ROOT), limits=stage.LIMITS,
            previous_binary_sha256=stage.DEFAULT_SHA, old_hashes={"q4t":stage.DEFAULT_SHA},
            binary={"path":"fixture"})
        with patch.object(stage.h, "verify_seal"), patch.object(stage.h, "read", return_value=plan), \
                patch.object(stage.h, "check_identity"), patch.object(stage.h, "verify_pair"), \
                patch.object(stage.a, "verify_hashes", side_effect=AssertionError("workspace dependency")):
            self.assertEqual(stage.rollback_plan(), plan)
            plan["root"] = "/foreign"
            with self.assertRaises(RuntimeError): stage.rollback_plan()


if __name__ == "__main__":
    unittest.main()
