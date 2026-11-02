"""CPU-only driver checks; sockets/processes are mocked, no model or HTTP."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_backpressure import stage, verify


class Contracts(unittest.TestCase):
    def test_budget_charges_once_and_never_resets_deadline(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            run = object.__new__(verify.Run)
            run.out, run.deadline = Path(directory), time.time()+60
            run.counts = {k: stage.LIMITS[k]-1 for k in ("starts", "generations", "negatives")}
            for kind in run.counts:
                run.budget(kind)
                with self.assertRaises(ValueError): run.budget(kind)
            self.assertEqual(len(list(run.out.glob("attempt-*.json"))), 3)
            run.deadline = time.time()-1
            with self.assertRaises(ValueError): run.budget()

    def test_guard_is_absolute_and_precedes_checks(self):
        argv = stage.guard_command()
        self.assertEqual(argv[2], str(stage.ROOT / "tools/evalscope/gpu_exclusive.py"))
        self.assertEqual(argv[4], str(stage.OUTPUT / "lease"))
        self.assertEqual(argv[-2], str(Path(stage.__file__).resolve()))
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
             patch.object(stage, "check") as check:
            with self.assertRaises(RuntimeError): stage.execute()
            check.assert_not_called()

    def test_receive_window_precedes_connect_no_read_on_submission(self):
        sock = Mock()
        with patch.object(verify.socket, "socket", return_value=sock):
            self.assertIs(verify.slow_socket(123, {"hello": "世界"}), sock)
        names = [call[0] for call in sock.mock_calls]
        self.assertLess(names.index("setsockopt"), names.index("connect"))
        self.assertNotIn("recv", names)
        data = sock.sendall.call_args.args[0]
        head, _, body = data.partition(b"\r\n\r\n")
        self.assertIn(b"Content-Length: "+str(len(body)).encode(), head)
        self.assertEqual(json.loads(body), {"hello": "世界"})
        sock.reset_mock()
        sock.connect.side_effect = OSError("failed")
        with patch.object(verify.socket, "socket", return_value=sock):
            with self.assertRaises(OSError): verify.slow_socket(123, {})
        sock.close.assert_called_once()

    def test_partial_wire_rejects_success_second_status_and_heartbeat_only(self):
        header = b"HTTP/1.1 200 OK\r\nContent-Length: 9999\r\n\r\n"
        for stream, body in ((False, b'{"choices":[{"message":{"content":"===='),
                             (True, b'data: {"choices":[{"delta":{"content":"====')):
            self.assertTrue(verify.partial_contract(header+body, stream)["client_rejected"])
            for suffix in (b'data: [DONE]', b'"finish_reason":"stop"',
                           b'"usage":{}', b'HTTP/1.1 500 ERR'):
                with self.assertRaises(ValueError): verify.partial_contract(header+body+suffix, stream)
        with self.assertRaises(ValueError): verify.partial_contract(header+b": heartbeat\r\n\r\n", True)
        with self.assertRaises(ValueError):
            verify.partial_contract(header.replace(b"9999", b"1")+b'"content":"====', False)

    def test_existing_payload_and_new_requests_match_native_contract(self):
        validator = verify.load_validators()["chat"]
        requests = [stage.h.read(stage.BASE / "payload-array" / name)
                    for name in ("json-request.json", "sse-request.json")]
        requests += [verify.short_request(), verify.short_request(True)]
        for body in requests: validator.validate(body)
        probe = stage.prior.BASE / "cpu/contract-build/contract_probe"
        raw = "".join(json.dumps(dict(raw=json.dumps(body)))+"\n" for body in requests)
        proc = subprocess.run([str(probe)], input=raw, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(proc.stderr)
        self.assertEqual([json.loads(line) for line in proc.stdout.splitlines()], [dict(accepted=True)]*4)

    def test_build_matches_runtime_and_only_explicit_fault_setting_changes(self):
        self.assertEqual(stage.build_gate()["runtime_files"], 189)
        self.assertEqual(stage.h.sha(stage.ROOT / "build/q4t"), stage.prior.DEFAULT_SHA)
        self.assertEqual(stage.h.sha(stage.ROOT / "build/q4t.release.json"), stage.prior.SIDECAR_SHA)

    def test_drain_waits_for_all_owners_not_only_slot(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            run = object.__new__(verify.Run)
            run.server = Mock()
            run.server.poll.return_value = None
            full = {k:0 for k in verify.EMPTY}
            full.update(q4t_seq_slots_free=1, q4t_structured_workers_started_total=1,
                        q4t_structured_workers_reaped_total=1)
            held = dict(full, q4t_structured_active=1)
            run.http = Mock(side_effect=[dict(status=200, raw="\n".join(f"{k} {v}" for k,v in row.items()))
                                        for row in (held, full)])
            run.snapshot = Mock(return_value=full)
            with patch.object(verify.time, "sleep") as sleep:
                self.assertEqual(run.drain(Path(directory), "drained"), full)
                sleep.assert_called_once()
            self.assertEqual(run.http.call_count, 2)

    def test_case_keeps_peer_unread_until_cleanup_and_checks_recovery(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            folder = Path(directory)
            body = stage.h.read(stage.BASE / "payload-array/sse-request.json")
            (folder / "server.log").write_text("id="+body["request_id"]+" event=stage stage=response ")
            (folder / "backpressure.entered").touch()
            run = object.__new__(verify.Run)
            run.port, run.cases = 123, []
            run.request = run.reject = run.health = Mock()
            rows = dict(q4t_structured_active=1, q4t_structured_workers_active=1,
                        q4t_seq_slots_free=0, q4t_generation_tokens_total=250, q4t_active_chats=2)
            run.snapshot = Mock(return_value=rows)
            run.http = Mock(side_effect=[dict(raw="\n".join(f"{k} {v}" for k,v in rows.items())),
                                        dict(status=202)])
            done = threading.Event()
            normal = SimpleNamespace(finish_reason="stop", message={"content":"710003"})
            strict = SimpleNamespace(finish_reason="stop", message={"content":'{"ok":true}'})
            def complete(folder, label, body, holder):
                if label == "queued":
                    done.wait(timeout=5)
                    holder["result"] = normal
                else:
                    holder["result"] = strict
            run.complete = complete
            final = {'q4t_requests_aborted_by_reason_total{reason="explicit_cancel"}':1,
                     'q4t_requests_aborted_by_reason_total{reason="deadline_exceeded"}':0,
                     'q4t_requests_success_total':2, 'q4t_requests_aborted_total':1}
            def drain(*args, **kwargs):
                self.assertTrue((folder / "backpressure.release").exists())
                done.set()
                return final
            run.drain = Mock(side_effect=drain)
            sock = Mock()
            sock.getsockopt.return_value = 2304
            chunks = iter([b'HTTP/1.1 200 OK\r\n\r\ndata: {"delta":{"content":"====', b''])
            def receive(size):
                self.assertTrue(done.is_set())
                return next(chunks)
            sock.recv.side_effect = receive
            with patch.object(verify, "slow_socket", return_value=sock), \
                 patch.object(stage, "PACKAGE", stage.BASE):
                run.case(folder, True)
            sock.close.assert_called_once()
            self.assertEqual(len(run.cases), 1)
            self.assertGreaterEqual(run.cases[0]["first_read"], run.cases[0]["cleaned"])
            self.assertEqual(run.drain.call_count, 2)


if __name__ == "__main__":
    unittest.main()
