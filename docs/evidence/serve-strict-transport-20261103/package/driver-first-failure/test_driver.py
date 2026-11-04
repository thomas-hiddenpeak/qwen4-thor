"""CPU driver checks, including corrupt evidence and incomplete outputs."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tokenizers import Tokenizer
from tools.serve_strict_transport import stage, verify


class DriverTests(unittest.TestCase):
    def test_changed_runtime_is_only_opt_in_diagnostics(self):
        self.assertEqual(len(stage.build_gate()), 193)
        self.assertEqual(stage.h.sha(stage.ROOT / "build/q4t"), stage.DEFAULT_SHA)
        self.assertEqual(stage.h.sha(stage.ROOT / "build/q4t.release.json"), stage.SIDECAR_SHA)

    def test_guard_before_any_check_or_dispatch(self):
        argv = stage.guard_command()
        self.assertEqual(argv[2], str(stage.ROOT / "tools/evalscope/gpu_exclusive.py"))
        self.assertEqual(argv[-2], str(Path(stage.__file__).resolve()))
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
                patch.object(stage, "check") as check:
            with self.assertRaises(RuntimeError): stage.execute()
            check.assert_not_called()

    def test_no_retry_budget_and_original_deadline(self):
        self.assertEqual(stage.LIMITS, dict(generations=6, negatives=4, starts=2, seconds=1800))
        with tempfile.TemporaryDirectory(dir=stage.BASE) as tmp:
            run = object.__new__(verify.Run)
            run.out, run.deadline = Path(tmp), time.time()+60
            run.counts = {k:stage.LIMITS[k]-1 for k in stage.PLANNED}
            for key in run.counts:
                run.budget(key)
                with self.assertRaises(ValueError): run.budget(key)
            run.deadline = time.time()-1
            with self.assertRaises(ValueError): run.budget()
            self.assertEqual(len(list(Path(tmp).iterdir())), 3)

    def test_generated_evidence_refuses_ambiguous_or_corrupt_ids(self):
        prefix = "[serve-test-generated] request=r "
        valid = prefix+"total=2 captured=2 truncated=0 tokens=[3,4]"
        self.assertEqual(verify.token_record(valid, "r")["ids"], [3,4])
        self.assertIsNone(verify.token_record(valid, "other"))
        bad = [valid+"\n"+valid, valid.replace("captured=2", "captured=1"),
               valid.replace("truncated=0", "truncated=1"),
               valid.replace("[3,4]", "[true,4]"), valid.replace("[3,4]", "[-1,4]"),
               valid.replace("[3,4]", "[248320,4]")]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                verify.token_record(raw, "r")

    def test_long_generated_content_requires_eos_and_exact_schema_value(self):
        from tools.serve_strict_transport.prepare import sha
        body = stage.h.read(stage.BASE / "cpu/sse-request.json")
        expected = (stage.BASE / "cpu/expected-content.json").read_text()
        dependencies = stage.h.read(stage.BASE / "cpu/dependencies.json")
        tokenizer_path = next(p for p in dependencies if p.endswith("/tokenizer.json"))
        self.assertEqual(sha(tokenizer_path), dependencies[tokenizer_path])
        tokenizer = Tokenizer.from_file(tokenizer_path)
        ids = stage.h.read(stage.BASE / "cpu/canonical-ids.json")
        variants = [(ids+[248046], True), (ids, False), (ids[:100], False),
                    (tokenizer.encode('{"value":[]}').ids+[248046], False)]
        for tokens, accepted in variants:
            with tempfile.TemporaryDirectory(dir=stage.BASE) as tmp:
                folder = Path(tmp)
                run = object.__new__(verify.Run)
                run.tokenizer, run.expected = tokenizer, json.loads(expected)
                run.plan = dict(stop_ids=[248046,248044])
                (folder / "server.log").write_text(
                    f'[serve-test-generated] request={body["request_id"]} total={len(tokens)} '
                    f'captured={len(tokens)} truncated=0 tokens={json.dumps(tokens)}\n')
                if accepted:
                    self.assertTrue(run.generated(folder, body, complete=True)["complete"])
                else:
                    with self.assertRaises(ValueError): run.generated(folder, body, complete=True)
                    saved = stage.h.read(folder / "generated.json")
                    self.assertFalse(saved["complete"])
                    self.assertTrue((folder / "generated-content.txt").exists())

    def test_missing_evidence_is_recorded_without_inventing_tokens(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as tmp:
            folder = Path(tmp)
            (folder / "server.log").write_text("request ended early\n")
            run = object.__new__(verify.Run)
            result = run.generated(folder, dict(request_id="absent"), complete=False)
            self.assertFalse(result["present"] or result["complete"])
            self.assertNotIn("ids", result)

    def test_all_planned_requests_pass_native_contract(self):
        bodies = [stage.h.read(stage.BASE / "cpu" / name)
                  for name in ("json-request.json", "sse-request.json")]
        bodies += [verify.short_request(), verify.short_request(True)]
        validator = verify.base.load_validators()["chat"]
        for body in bodies: validator.validate(body)
        raw = "".join(json.dumps(dict(raw=json.dumps(body)))+"\n" for body in bodies)
        probe = stage.previous.BASE / "cpu/planned-native/contract_probe"
        proc = subprocess.run([str(probe)], input=raw, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(proc.stderr)
        self.assertEqual([json.loads(line) for line in proc.stdout.splitlines()],
                         [dict(accepted=True)]*4)
        stage.h.save(stage.BASE / "planned-native.json", dict(passed=True,
            probe=stage.h.identity(probe), requests=bodies, output=proc.stdout))

    def test_capacity_queue_cannot_be_ignored_by_drain(self):
        run = object.__new__(verify.Run)
        with patch.object(verify.base.Run, "drain", return_value={"q4t_queued_requests":1}):
            with self.assertRaises(ValueError): run.drain(Path("unused"), "unused")

    def test_case_requires_complete_tokens_first_write_and_delayed_read(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as tmp:
            root = Path(tmp)
            (root / "payload").mkdir()
            folder = root / "case"
            folder.mkdir()
            body = stage.h.read(stage.BASE / "cpu/sse-request.json")
            stage.h.save(root / "payload/sse-request.json", body)
            (folder / "server.log").write_text(
                'id='+body['request_id']+' event=stage stage=response \n'+
                '[q4t][http-write] trace_id=trace reason=explicit_cancel origin=handler '
                'transferred=100 system_error=0 poll_events=0\n')
            (folder / "backpressure.entered").touch()
            run = object.__new__(verify.Run)
            run.port, run.cases = 123, []
            run.request = run.reject = run.health = Mock()
            run.generated = Mock()
            rows = dict(q4t_structured_active=1, q4t_structured_workers_active=1,
                        q4t_seq_slots_free=0, q4t_generation_tokens_total=244, q4t_active_chats=2)
            run.snapshot = Mock(return_value=rows)
            run.http = Mock(side_effect=[dict(raw="\n".join(f"{k} {v}" for k,v in rows.items())),
                                        dict(status=202)])
            released = threading.Event()
            def complete(folder, label, body, holder):
                if label == "queued": released.wait(timeout=5)
                holder["result"] = SimpleNamespace(finish_reason="stop", message={
                    "content":"710003" if label == "queued" else '{"ok":true}'})
            run.complete = complete
            final = {'q4t_requests_aborted_by_reason_total{reason="explicit_cancel"}':1,
                     'q4t_requests_aborted_by_reason_total{reason="deadline_exceeded"}':0,
                     'q4t_requests_success_total':2, 'q4t_requests_aborted_total':1}
            def drain(*args, **kwargs):
                self.assertTrue((folder / "backpressure.release").exists())
                released.set()
                return final
            run.drain = Mock(side_effect=drain)
            sock = Mock()
            sock.getsockopt.return_value = 2304
            parts = iter([b'HTTP/1.1 200 OK\r\nX-Request-Id: trace\r\n\r\n'
                          b'data: {"delta":{"content":"====', b''])
            def receive(size):
                self.assertTrue(released.is_set())
                return next(parts)
            sock.recv.side_effect = receive
            with patch.object(verify, "slow_socket", return_value=sock), \
                    patch.object(stage, "PACKAGE", root), \
                    patch.object(verify, "transport_rows", return_value={
                        verify.write_key("handler", "explicit_cancel"):1}):
                run.case(folder, True)
            run.generated.assert_called_once_with(folder, body, complete=True)
            sock.close.assert_called_once()
            self.assertIsNone(run.pending_socket)
            self.assertGreaterEqual(run.cases[0]["first_read"], run.cases[0]["cleaned"])


if __name__ == "__main__":
    unittest.main()
