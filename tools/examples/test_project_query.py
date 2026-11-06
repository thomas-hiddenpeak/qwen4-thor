"""CPU contracts for read-only sources, citations and durable business loops."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import chat_client as client
import project_query
from project_records import MAX_FILE, ProjectRecords, RecordError
from test_chat_client import capabilities, completion, whole
from tool_recovery import inspect_recovery


class ProjectContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs/current.md").write_text("release\n版本 123\naccepted\n")
        self.records = ProjectRecords(self.root)

    def call(self, path, start=1, count=3):
        return self.records.execute(client.ToolInvocation("id", "read_project_record",
            dict(path=path, start_line=start, line_count=count)))

    def test_actual_source_identity_and_lines(self):
        source = self.call("docs/current.md", 2, 2)["source"]
        self.assertEqual(source["text"], "2: 版本 123\n3: accepted")
        self.assertEqual(source["citation"], "[docs/current.md:L2-L3]")
        self.assertEqual(source["sha256"], hashlib.sha256(
            (self.root / "docs/current.md").read_bytes()).hexdigest())

    def test_paths_symlinks_and_special_files(self):
        (self.root / "docs/link.md").symlink_to(self.root / "docs/current.md")
        (self.root / "docs/linkdir").symlink_to(self.root / "docs",
                                                target_is_directory=True)
        os.mkfifo(self.root / "docs/pipe.md")
        for path in ("/etc/passwd", "docs/../secret.json", "docs/.private.json",
                     "reference/a.md", "build/q4t", "docs/link.md",
                     "docs/linkdir/current.md", "docs/pipe.md", "docs//current.md"):
            self.assertFalse(self.call(path)["ok"], path)

    def test_expected_tool_errors_are_results(self):
        self.assertEqual(self.call("docs/missing.md")["error"]["code"],
                         "record_not_found")
        self.assertEqual(self.call("docs/current.md", 4)["error"]["code"],
                         "line_out_of_range")
        (self.root / "docs/big.md").write_bytes(b"x" * (MAX_FILE + 1))
        self.assertEqual(self.call("docs/big.md")["error"]["code"], "file_too_large")
        (self.root / "docs/bad.md").write_bytes(b"\xff")
        self.assertEqual(self.call("docs/bad.md")["error"]["code"], "record_not_utf8")

    def test_search_is_literal_bounded_and_traceable(self):
        for index in range(8):
            (self.root / f"docs/{index}.md").write_text("some [literal] data\n")
        value = self.records.search({"query": "[literal]"})
        self.assertEqual(len(value["matches"]), 6)
        self.assertTrue(value["truncated"])
        self.assertIn("not an absence proof", value["scope"])
        for result in value["matches"]:
            self.assertIn("[literal]", result["text"])
            self.assertEqual(len(result["sha256"]), 64)

    def test_citations_must_have_been_read(self):
        good = "[docs/current.md:L1-L3]"
        project_query.validate_answer("123 " + good, {good}, 1)
        for answer, sources, calls in (("123", {good}, 1),
                ("123 [docs/other.md:L1-L3]", {good}, 1), ("123", set(), 0)):
            with self.assertRaises(client.ProtocolError):
                project_query.validate_answer(answer, sources, calls)

    def test_tool_batch_and_journal_real_source(self):
        source = self.call("docs/current.md")["source"]
        fake = FakeClient("docs/current.md", "123 " + source["citation"])
        directory = self.root / ".q4t-work/query"
        with patch.object(project_query, "ROOT", self.root):
            self.assertIn("123", project_query.run(directory, "http://localhost",
                "read current", stream=False, client=fake))
        self.assertEqual(inspect_recovery(directory)["state"], "COMPLETED")
        result = json.loads((directory / "round-00-tool-000-result.json").read_text())
        self.assertEqual(json.loads(result["content"])["source"], source)
        self.assertEqual(json.loads(result["content"])["application_budget"],
            dict(remaining_model_requests=3, remaining_tool_calls=7,
                 final_response_required=False))
        self.assertEqual(fake.calls, 2)

    def test_output_budget_is_preserved_in_requests_and_recovery(self):
        source = self.call("docs/current.md")["source"]
        fake = FakeClient("docs/current.md", "123 " + source["citation"])
        directory = self.root / ".q4t-work/budget"
        with patch.object(project_query, "ROOT", self.root):
            project_query.run(directory, "http://localhost", "read current",
                              stream=False, client=fake, max_tokens=768)
        self.assertEqual(json.loads((directory/"run-started.json").read_text())["max_tokens"], 768)
        for path in directory.glob("*-request.json"):
            self.assertEqual(json.loads(path.read_text())["max_tokens"], 768)
        self.assertEqual(inspect_recovery(directory)["state"], "COMPLETED")

    def test_missing_record_returned_to_model(self):
        fake = FakeClient("docs/missing.md", "无法核实：record_not_found")
        directory = self.root / ".q4t-work/missing"
        with patch.object(project_query, "ROOT", self.root):
            project_query.run(directory, "http://localhost", "read missing",
                              stream=False, client=fake)
        history = json.loads((directory / "round-00-history.json").read_text())
        self.assertEqual(history[-1]["tool_call_id"], "call-test")
        self.assertFalse(json.loads(history[-1]["content"])["ok"])
        self.assertEqual(inspect_recovery(directory)["state"], "COMPLETED")

    def test_partial_response_never_executes_tools(self):
        fake = FakeClient("docs/current.md", "", truncated=True)
        directory = self.root / ".q4t-work/truncated"
        with patch.object(project_query, "ROOT", self.root):
            with self.assertRaises(client.ProtocolError):
                project_query.run(directory, "http://localhost", "read",
                                  stream=False, client=fake)
        self.assertEqual(fake.calls, 1)
        self.assertFalse(list(directory.glob("*-tool-*-started.json")))
        self.assertEqual(inspect_recovery(directory)["state"], "LENGTH_LIMIT")

    def test_remote_errors_preserve_fields_without_retry(self):
        error = dict(message="busy", type="service_unavailable_error",
                     code="capacity_exhausted", param=None)
        for body in (error, {"error": error}, json.dumps({"error": error})):
            result = client.RemoteError(503, body)
            self.assertEqual(result.code, "capacity_exhausted")
            self.assertEqual(result.type, "service_unavailable_error")
            self.assertFalse(result.automatic_retry_safe)
        self.assertIsNone(client.RemoteError(502, "<html>proxy error").code)
        self.assertEqual(client.RemoteError(200, error).status, 200)


class FakeClient:
    def __init__(self, path, answer, truncated=False):
        self.path, self.answer, self.truncated = path, answer, truncated
        self.calls = 0

    def capabilities(self, **kwargs):
        return capabilities()

    def complete(self, request, sink):
        self.calls += 1
        if self.calls == 1:
            message = dict(role="assistant", content=None, tool_calls=[
                dict(id="call-test", type="function", function=dict(
                    name="read_project_record", arguments=json.dumps(dict(
                        path=self.path, start_line=1, line_count=3))))])
            finish = "length" if self.truncated else "tool_calls"
        else:
            message = dict(role="assistant", content=self.answer)
            finish = "stop"
        response = whole(message, finish, request["request_id"])
        sink(json.dumps(response, ensure_ascii=False).encode())
        return completion(message, finish, request["request_id"])


if __name__ == "__main__":
    unittest.main()
