"""Frozen bounds, native fixture checks and fake socket proxy; no HTTP/model."""
import copy
import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_protocol import stage, verify, build_contract, cases
from tools.serve_protocol.check_contract import load_validators


class Contracts(unittest.TestCase):
    def test_node_report_formats_reject_failures_and_incomplete_output(self):
        for prefix in ("# ", "ℹ "):
            log = "\n".join(prefix+line for line in ("tests 35", "pass 35", "fail 0", "cancelled 0"))
            self.assertTrue(stage.node_report_passed(log))
            self.assertFalse(stage.node_report_passed(log.replace("fail 0", "fail 1")))
            self.assertFalse(stage.node_report_passed(log.replace("tests 35", "tests 34")))
            self.assertFalse(stage.node_report_passed(log.replace("cancelled 0", "cancelled 1")))

    def test_guard_argv_and_guard_check_precedes_all_work(self):
        argv = stage.guard_command()
        self.assertEqual(argv[2], str(stage.ROOT / "tools/evalscope/gpu_exclusive.py"))
        self.assertEqual(argv[4], str(stage.OUTPUT / "lease"))
        self.assertEqual(argv[-2:], [str(Path(stage.__file__).resolve()), "execute"])
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
             patch.object(stage, "check") as check:
            with self.assertRaises(RuntimeError):
                stage.execute()
            check.assert_not_called()

    def test_every_attempt_charged_and_original_deadline_not_reset(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            run = object.__new__(verify.Run)
            run.out = Path(directory)
            run.deadline = time.time()+60
            run.counts = {k: stage.LIMITS[k]-1 for k in ("starts", "generations", "negatives")}
            for kind in run.counts:
                run.budget(kind)
                with self.assertRaises(ValueError):
                    run.budget(kind)
            self.assertEqual(len(list(run.out.glob("attempt-*.json"))), 3)
            run.deadline = time.time()-1
            with self.assertRaises(ValueError):
                run.budget()
        # Guard emits UTC ISO times (not a nonexistent numeric unix field).
        event = json.loads((stage.ROOT / ".q4t-work/serve-preflight-20261103/"
            "execution-final/lease/events.jsonl").read_text().splitlines()[0])
        self.assertGreater(datetime.datetime.fromisoformat(event["utc"]).timestamp(), 0)

    def test_sdk_missing_before_any_model_start(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            run = object.__new__(verify.Run)
            run.out, run.started = Path(directory), time.monotonic()
            run.server = run.server_fd = None
            run.counts = dict(generations=0, negatives=0, starts=0)
            with patch.object(stage, "sdk_identity", side_effect=RuntimeError("missing SDK")), \
                 patch.object(run, "model") as model:
                with self.assertRaises(Exception):
                    run.run()
                model.assert_not_called()
            result = json.loads((run.out / "result.json").read_text())
            self.assertEqual(result["counts"], run.counts)
            self.assertIn("missing SDK", result["error"])

    def test_all_38_fields_have_positive_null_wrong_shape_and_omission(self):
        schema, rows = build_contract.chat_schema(), cases.cases()
        inventory = stage.h.read(stage.ROOT / "tools/serve_audit/inventory.json")
        self.assertEqual(set(schema["properties"]), set(inventory["fields"]))
        self.assertEqual(len(schema["properties"]), 38)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(len(by_id), len(rows))
        for field in schema["properties"]:
            for suffix in ("valid", "null", "wrong-shape", "omitted"):
                self.assertEqual(by_id[field+"-"+suffix]["field"], field)

    def test_openapi_refs_routes_and_explicit_semantic_boundaries(self):
        spec = build_contract.openapi()
        routes = {method.upper()+" "+path for path, methods in spec["paths"].items() for method in methods}
        self.assertEqual(routes, set(stage.h.read(stage.ROOT / "tools/serve_audit/inventory.json")["routes"]))
        documents = {p.name: json.loads(p.read_text()) for p in (stage.ROOT / "docs").glob("*.schema.json")}
        documents["serve-openapi-v1.json"] = spec
        def check(value, owner):
            if isinstance(value, dict):
                if "$ref" in value:
                    file, _, pointer = value["$ref"].partition("#")
                    target = documents[file or owner]
                    for component in pointer.split("/")[1:]:
                        target = target[component.replace("~1", "/").replace("~0", "~")]
                    self.assertIsInstance(target, dict)
                for child in value.values(): check(child, owner)
            elif isinstance(value, list):
                for child in value: check(child, owner)
        for name, value in documents.items(): check(value, name)
        semantics = build_contract.semantics()
        self.assertTrue(semantics["required"])
        self.assertIn("runtime availability", semantics["probe_excludes"])
        self.assertIn("model execution", semantics["probe_excludes"])
        stage.a.verify_hashes(stage.ROOT, semantics["sources_sha256"])

    def test_http_negative_fixtures_fail_production_contract_before_gpu(self):
        rows = verify.negative_requests()
        self.assertEqual(len(rows), 16)
        inputs = [dict(raw=json.dumps(body, ensure_ascii=False)) for _, body in rows]
        process = subprocess.run([str(stage.BASE / "cpu/contract-build/contract_probe")],
            input="".join(json.dumps(row)+"\n" for row in inputs), capture_output=True,
            text=True, check=True, timeout=30)
        self.assertFalse(process.stderr)
        native = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(native), 16)
        self.assertTrue(all(not row["accepted"] and row["response"]["error"]["code"] for row in native))
        stage.h.save(stage.BASE / "cpu/http-negative-contracts.json",
            dict(names=[name for name, _ in rows], inputs=inputs, native=native))

    def test_strict_tool_request_shapes_and_final_history_contract(self):
        tools = verify.strict_read_tools(["docs/MODEL.md", "docs/ARCHITECTURE.md"])
        history = [dict(role="system", content=verify.SYSTEM), dict(role="user", content="read both")]
        request = verify.make_request(history, tools, stream=True, timeout_ms=180000)
        request["tool_choice"] = "required"
        load_validators()["chat"].validate(request)
        native = subprocess.run([str(stage.BASE / "cpu/contract-build/contract_probe")],
            input=json.dumps(dict(raw=json.dumps(request)))+"\n", text=True, capture_output=True,
            timeout=30, check=True)
        self.assertEqual(json.loads(native.stdout), dict(accepted=True))
        bad = copy.deepcopy(request)
        bad["tools"][0]["function"]["parameters"]["properties"]["path"]["enum"] = ["x"]*2
        native = subprocess.run([str(stage.BASE / "cpu/contract-build/contract_probe")],
            input=json.dumps(dict(raw=json.dumps(bad)))+"\n", text=True, capture_output=True,
            timeout=30, check=True)
        self.assertFalse(json.loads(native.stdout)["accepted"])

    def test_cut_proxy_forwards_actual_prefix_and_no_complete_terminal(self):
        class Peer:
            def __init__(self, chunks): self.chunks, self.sent = list(chunks), b""
            def __enter__(self): return self
            def __exit__(self, *unused): return False
            def settimeout(self, value): pass
            def recv(self, size): return self.chunks.pop(0) if self.chunks else b""
            def sendall(self, value): self.sent += value
        ingress = b'POST /v1/chat/completions HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}'
        response = (b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n'
                    b'data: {"delta":{"role":"assistant","content":""}}\n\n'
                    b'data: {"delta":{"content":"real answer"}}\n\ndata: [DONE]\n\n')
        peer, upstream = Peer([ingress]), Peer([response[:30], response[30:]])
        listener = Peer([])
        listener.accept = lambda: (peer, None)
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            proxy = object.__new__(verify.CutProxy)
            proxy.port, proxy.folder, proxy.listener, proxy.error = 1, Path(directory), listener, None
            with patch.object(verify.socket, "create_connection", return_value=upstream): proxy.run()
            self.assertIsNone(proxy.error)
            self.assertEqual(upstream.sent, ingress)
            self.assertTrue(response.startswith(peer.sent))
            self.assertTrue(peer.sent.endswith(b'"content":"r'))
            self.assertNotIn(b"[DONE]", peer.sent)


if __name__ == "__main__":
    unittest.main()
