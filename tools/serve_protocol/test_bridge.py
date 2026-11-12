"""Actual Node SDK/subprocess/journal checks with an in-memory test transport."""
import io
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import js_client
from js_client import JsSdkClient, JsSdkError, ROOT
from chat_client import ProtocolError, load_json
import project_query
from project_records import VALIDATORS
from tool_recovery import inspect_recovery

BASE = ROOT / ".q4t-work/serve-protocol-20261103"
SDK = BASE / "sdk"
CAPS = json.loads(json.loads((ROOT / "docs/evidence/serve-preflight-20261103/"
    "execution-final/business/candidate-mtp/capabilities.json").read_text())["raw"])
REAL_POPEN = subprocess.Popen


class Bridge(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bridge-", dir=BASE)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.config = self.folder / "fixture.json"
        self.calls = self.folder / "calls.jsonl"
        self.journal = self.folder / "journal"
        self.mode()

        def spawn(argv, **kwargs):
            # Explicit test-only preload after the production adapter removes
            # ambient NODE_OPTIONS. No HTTP/model work is performed here.
            kwargs["env"]["Q4T_PROTOCOL_FIXTURE"] = str(self.config)
            return REAL_POPEN([argv[0], "--import", str(Path(__file__).with_name(
                "fixture_transport.mjs")), *argv[1:]], **kwargs)

        self.addCleanup(patch.stopall)
        patch.object(js_client.subprocess, "Popen", side_effect=spawn).start()
        self.client = JsSdkClient("http://127.0.0.1:1", SDK,
            protocol_sha256=CAPS["protocol_source_sha256"], timeout=3)

    def mode(self, mode="normal", paths=None):
        self.config.write_text(json.dumps(dict(mode=mode,
            paths=paths or ["docs/MODEL.md"], calls=str(self.calls), capabilities=CAPS)))

    def run_query(self, stream=True):
        return project_query.run(self.journal, self.client.base_url, "读取资料。",
            stream=stream, max_rounds=2, max_tool_calls=4, client=self.client)

    def submitted(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_real_read_journal_roundtrip_json(self):
        self.assertIn("[docs/MODEL.md:L1-L8]", self.run_query(False))
        state = inspect_recovery(self.journal)
        self.assertEqual(state["state"], "COMPLETED")
        self.assertEqual((state["requests"], state["completed_tool_calls"]), (2, 1))
        self.assertEqual(len(self.submitted()), 3)

    def test_real_read_journal_roundtrip_sse_and_two_results(self):
        self.mode(paths=["docs/MODEL.md", "docs/ARCHITECTURE.md"])
        answer = self.run_query()
        self.assertIn("[docs/MODEL.md:L1-L8]", answer)
        self.assertIn("[docs/ARCHITECTURE.md:L1-L8]", answer)
        state = inspect_recovery(self.journal)
        self.assertEqual(state["state"], "COMPLETED")
        self.assertEqual(state["completed_tool_calls"], 2)
        self.assertEqual([m["tool_call_id"] for m in state["messages"] if
                          m["role"] == "tool"], ["call-0", "call-1"])

    def test_business_failure_is_a_result_not_transport_success_claim(self):
        self.mode(paths=["docs/SRV08-intentionally-absent.md"])
        self.assertIn("record_not_found", self.run_query())
        state = inspect_recovery(self.journal)
        self.assertEqual(state["state"], "COMPLETED")
        result = json.loads(next(m["content"] for m in state["messages"] if m["role"] == "tool"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "record_not_found")

    def failed_query(self, mode):
        self.mode(mode, ["docs/MODEL.md", "docs/ARCHITECTURE.md"])
        with self.assertRaises(JsSdkError):
            self.run_query()
        state = inspect_recovery(self.journal)
        self.assertEqual(state["state"], "REQUEST_OUTCOME_UNKNOWN")
        self.assertEqual(state["attempted_tool_calls"], 0)
        self.assertFalse(state["automatic_retry"])
        self.assertEqual(len(self.submitted()), 2)  # Discovery + one chat.
        self.assertGreater((self.journal / "round-00-wire.bin").stat().st_size, 0)
        self.assertFalse(list(self.journal.glob("*-started.json"))[1:])
        self.assertFalse((self.journal / "round-00-response.json").exists())

    def test_whole_batch_invalid_tool_blocks_every_execution(self):
        self.failed_query("unknown-tool")

    def test_missing_done_does_not_commit_or_dispatch(self):
        self.failed_query("missing-done")

    def test_transport_disconnect_retains_partial_wire_without_retry(self):
        self.failed_query("disconnect")

    def test_node_crash_retains_live_pipe_capture_and_unknown_outcome(self):
        self.failed_query("child-crash")

    def test_http_error_preserves_status_code_and_raw_with_zero_retries(self):
        self.mode("http-error")
        with self.assertRaises(JsSdkError) as caught:
            self.run_query()
        self.assertEqual(caught.exception.details["status"], 503)
        self.assertEqual(caught.exception.details["code"], "overloaded")
        self.assertEqual(len(self.submitted()), 2)
        self.assertEqual(inspect_recovery(self.journal)["state"], "REQUEST_OUTCOME_UNKNOWN")

    def test_recovery_tampering_is_not_replayed(self):
        self.run_query()
        before = self.calls.read_bytes()
        path = self.journal / "round-00-tool-000-result.json"
        result = json.loads(path.read_text())
        result["tool_call_id"] = "other"
        path.write_text(json.dumps(result))
        self.assertEqual(inspect_recovery(self.journal)["state"], "RECORDS_INVALID")
        self.assertEqual(self.calls.read_bytes(), before)

    def test_explicit_capability_identity_override_is_enforced(self):
        with self.assertRaises(ProtocolError):
            self.client.capabilities(protocol_sha256="0"*64)
        self.assertEqual(len(self.submitted()), 1)

    def test_actual_sdk_models_discovery(self):
        self.assertEqual(self.client.models()["data"][0]["id"], CAPS["model"])
        self.assertTrue(self.submitted()[0]["url"].endswith("/v1/models"))

    def test_exact_schema_crosses_python_node_and_sdk_without_rounding(self):
        schema = dict(type="object", properties=dict(value=dict(type="number",
            enum=[9007199254740993, Decimal("1e400"), Decimal("1e-400"),
                  Decimal("0.30000000000000001")])), required=["value"],
            additionalProperties=False)
        body = dict(model=CAPS["model"], request_id="bridge-exact", stream=False,
            messages=[dict(role="user", content="Read docs/MODEL.md")],
            response_format=dict(type="json_schema", json_schema=dict(
                name="exact", strict=True, schema=schema)))
        value = self.client.complete(body)
        self.assertEqual(value.finish_reason, "tool_calls")
        raw = self.submitted()[0]["request_raw"]
        actual = load_json(raw, exact_numbers=True)
        self.assertEqual(actual["response_format"]["json_schema"]["schema"], schema)
        self.assertIn('9007199254740993', raw)
        self.assertNotIn('"1E+400"', raw)


class BusinessProfile(unittest.TestCase):
    def test_js_python_pure_validators_for_supported_argument_shapes(self):
        rows = []
        for query in ("q", "", " ", "\x85", "\ufeff", "\x1c q \x1f", "a\nb",
                      "🙂"*128, "🙂"*129, None, 1, " q "):
            rows.append(dict(name="search_project_docs", args=dict(query=query)))
        for path in ("docs/MODEL.md", "", "🙂"*512, "🙂"*513, None):
            rows.append(dict(name="read_project_record", args=dict(path=path, start_line=1, line_count=8)))
        for key, values in (("start_line", (0, 1, 1000000, 1000001, True, 1.5)),
                            ("line_count", (0, 1, 80, 81, True, 1.5))):
            for value in values:
                args = dict(path="docs/MODEL.md", start_line=1, line_count=8)
                args[key] = value
                rows.append(dict(name="read_project_record", args=args))
        for row in rows[:2]:
            rows.append(dict(name=row["name"], args={**row["args"], "extra": True}))
        expected = []
        for row in rows:
            try:
                VALIDATORS[row["name"]](row["args"])
                expected.append(True)
            except ValueError:
                expected.append(False)
        source = """import { VALIDATORS } from './tools/examples/js/project_profile.mjs';
let data=''; for await (const c of process.stdin) data+=c;
console.log(JSON.stringify(JSON.parse(data).map(r=>{
try { VALIDATORS[r.name](r.args); return true; } catch { return false; }
})));"""
        run = subprocess.run(["node", "--input-type=module", "-e", source],
            input=json.dumps(rows), text=True, capture_output=True, cwd=ROOT, check=True)
        self.assertEqual(json.loads(run.stdout), expected)
        self.assertEqual(len(rows), 31)


if __name__ == "__main__":
    unittest.main()
