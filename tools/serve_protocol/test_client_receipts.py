"""Real SDK bridge and read-only journal contracts; CPU fixtures, no model."""
from copy import deepcopy
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
CLIENT_ROOT = Path(os.environ.get("Q4T_CLIENT_ARTIFACT_ROOT", ROOT)).resolve()
sys.path.insert(0, str(ROOT / "tools/examples"))  # Public fixture helpers.
sys.path.insert(0, str(CLIENT_ROOT / "tools/examples"))
sys.path.insert(0, str(CLIENT_ROOT / "tools/serve_protocol"))
import chat_client
import js_client
from js_client import JsSdkClient, JsSdkError
from test_chat_client import capabilities
import tool_recovery

for module, relative in ((chat_client, "tools/examples/chat_client.py"),
                         (js_client, "tools/serve_protocol/js_client.py"),
                         (tool_recovery, "tools/examples/tool_recovery.py")):
    if Path(module.__file__).resolve() != CLIENT_ROOT/relative:
        raise RuntimeError("test imported a different client artifact")

MODEL = "qwen3.8-flash-next"


def response(request, *, finish="stop", review_error=False):
    """Synthetic public protocol fixture, not model/business quality evidence."""
    report = dict(type="moderation_results", model=MODEL, results=[dict(
        type="moderation_result", model=MODEL, flagged=finish == "content_filter",
        categories={"violence": finish == "content_filter"},
        category_scores={"violence": .9 if finish == "content_filter" else .1},
        category_applied_input_types={"violence": ["text"]})])
    error = dict(type="error", code="classifier_unavailable", message="Fixture")
    logprobs = dict(content=[dict(token="A", bytes=[65], logprob=-.5,
                                 top_logprobs=[])], refusal=None)
    message = dict(role="assistant", content="A")
    if finish == "content_filter":
        message["content"] = None
        logprobs = None
    elif finish == "tool_calls":
        message = dict(role="assistant", content=None, tool_calls=[dict(
            id="read-1", type="function", function=dict(name="read_project_record",
            arguments='{"path":"docs/MODEL.md","start_line":1,"line_count":8}'))])
    count = request.get("n", 1)
    return dict(id=request["request_id"], model=request["model"], created=1,
        object="chat.completion", choices=[dict(index=i, message=deepcopy(message),
            finish_reason=finish, logprobs=deepcopy(logprobs)) for i in range(count)],
        usage=dict(prompt_tokens=20, completion_tokens=count, total_tokens=20+count),
        moderation=dict(input=error if review_error else deepcopy(report),
                        output=deepcopy(report)),
        q4t_moderation_usage=dict(input_tokens=41, output_tokens=23, model_steps=2))


def wire(value, stream):
    if not stream:
        return json.dumps(value).encode()
    envelope = {k: value[k] for k in ("id", "model", "created")}
    envelope["object"] = "chat.completion.chunk"
    events = []
    for choice in value["choices"]:
        message = deepcopy(choice["message"])
        for index, call in enumerate(message.get("tool_calls", [])):
            call["index"] = index
        events.append(dict(envelope, usage=None, choices=[dict(
            index=choice["index"], delta=message, finish_reason=None,
            logprobs=choice["logprobs"])]))
        events.append(dict(envelope, usage=None, choices=[dict(
            index=choice["index"], delta={}, finish_reason=choice["finish_reason"],
            logprobs=None)]))
    if "moderation" in value:
        events.append(dict(envelope, choices=[], moderation=value["moderation"],
            q4t_moderation_usage=value["q4t_moderation_usage"]))
    events.append(dict(envelope, choices=[], usage=value["usage"]))
    return ("".join("data: "+json.dumps(v)+"\r\n\r\n" for v in events)+
            "data: [DONE]\r\n\r\n").encode()


class SdkReceipts(unittest.TestCase):
    def call(self, *, stream, choices=1, review_error=False, malformed=False):
        sdk = Path(os.environ.get("Q4T_NODE_SDK_DIR",
            ROOT/".q4t-work/serve-protocol-20261103/sdk"))
        self.assertTrue((sdk/"node_modules/openai/package.json").is_file())
        observed, errors = [], []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    if self.path != "/v1/chat/completions":
                        raise ValueError("unexpected endpoint")
                    size = int(self.headers["Content-Length"])
                    if not 0 < size <= 131072:
                        raise ValueError("unexpected request size")
                    request = json.loads(self.rfile.read(size))
                    value = response(request, review_error=review_error)
                    if malformed:
                        value["q4t_moderation_usage"]["input_tokens"] = -1
                    raw = wire(value, request["stream"])
                    observed.append((request, value, raw))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream" if
                        request["stream"] else "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    # Exercise the actual Node HTTP adapter and inherited pipe.
                    for offset in range(0, len(raw), 37):
                        self.wfile.write(raw[offset:offset+37])
                    self.wfile.flush()
                except Exception as error:
                    errors.append(repr(error))

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs=dict(poll_interval=.02), daemon=True)
        thread.start()
        request = chat_client.make_request([dict(role="user", content="A")], [],
            stream=stream, moderation=dict(model=MODEL))
        request["n"] = choices
        request["logprobs"] = True
        client = JsSdkClient(f"http://127.0.0.1:{server.server_port}", sdk, timeout=3)
        sink = io.BytesIO()
        try:
            if malformed:
                with self.assertRaises(JsSdkError):
                    client.complete(request, sink=sink.write)
            else:
                result = client.complete(request, sink=sink.write)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(observed), 1)  # No retry, including invalid wire.
        sent, value, raw = observed[0]
        self.assertEqual(sent, request)
        self.assertEqual(sink.getvalue(), raw)
        if malformed:
            return
        expected = (chat_client.read_sse if stream else chat_client.read_json)(
            io.BytesIO(raw))
        self.assertEqual(asdict(result), asdict(expected))
        selected = result.select(choices-1) if choices > 1 else result
        self.assertEqual(selected.moderation, value["moderation"])
        self.assertEqual(selected.q4t_moderation_usage, value["q4t_moderation_usage"])
        self.assertEqual(selected.logprobs, value["choices"][0]["logprobs"])

    def test_single_json(self):
        self.call(stream=False)

    def test_single_sse(self):
        self.call(stream=True)

    def test_multiple_json(self):
        self.call(stream=False, choices=2)

    def test_multiple_sse(self):
        self.call(stream=True, choices=2)

    def test_score_error_is_preserved(self):
        self.call(stream=True, review_error=True)

    def test_invalid_metadata_fails_without_retry(self):
        self.call(stream=True, malformed=True)


class RecoveryReceipts(unittest.TestCase):
    def journal(self, *, stream=False, finish="stop", legacy=False):
        root = Path(os.environ.get("Q4T_TEST_OUTPUT_DIR",
                                   ROOT/"build/client-receipt-contracts"))
        root.mkdir(parents=True, exist_ok=True)
        folder = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(folder.cleanup)
        directory = Path(folder.name)
        with tool_recovery.journal_lock(directory, create=True):
            pass
        history = [dict(role="user", content="A")]
        request = chat_client.make_request(history, [], stream=stream)
        values = {
            "run-started.json": dict(journal_version=1, initial_messages=history,
                max_requests=4, max_tool_calls=8, model=MODEL),
            "capabilities.json": capabilities(),
            "round-00-request.json": request,
            "round-00-submitted.json": dict(request_id=request["request_id"])}
        value = response(request, finish=finish)
        if legacy:
            del value["moderation"], value["q4t_moderation_usage"]
            del value["choices"][0]["logprobs"]
        raw = wire(value, stream)
        completion = (chat_client.read_sse if stream else chat_client.read_json)(
            io.BytesIO(raw))
        values["round-00-response.json"] = asdict(completion)
        if legacy:
            for key in ("logprobs", "moderation", "q4t_moderation_usage"):
                del values["round-00-response.json"][key]
        for name, value in values.items():
            tool_recovery.save_receipt(directory/name, value)
        (directory/"round-00-wire.bin").write_bytes(raw)
        return directory, history, completion

    def inspect(self, directory):
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        result = tool_recovery.inspect_recovery(directory)
        self.assertFalse(result["automatic_retry"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})
        return result

    def test_metadata_roundtrip_json_and_sse(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                directory, _, _ = self.journal(stream=stream)
                self.assertEqual(self.inspect(directory)["state"], "FINAL_RESPONSE_SAVED")

    def test_modified_report_usage_or_logprobs_is_invalid(self):
        for field in ("moderation", "q4t_moderation_usage", "logprobs"):
            with self.subTest(field=field):
                directory, _, _ = self.journal()
                path = directory/"round-00-response.json"
                value = json.loads(path.read_text())
                if field == "moderation":
                    value[field]["input"]["results"][0]["category_scores"]["violence"] = .2
                elif field == "logprobs":
                    value[field]["content"][0]["logprob"] = -.6
                else:
                    value[field]["input_tokens"] += 1
                path.write_text(json.dumps(value))
                result = self.inspect(directory)
                self.assertEqual(result["state"], "RECORDS_INVALID")
                self.assertIn("wire/receipt mismatch", result["error"])

    def test_content_filter_is_terminal_without_tools_or_history(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                directory, history, _ = self.journal(stream=stream, finish="content_filter")
                result = self.inspect(directory)
                self.assertEqual(result["state"], "CONTENT_FILTERED")
                self.assertEqual(result["messages"], history)
                self.assertEqual(result["attempted_tool_calls"], 0)
                self.assertEqual(result["pending_tool_calls"], [])
                self.assertEqual(result["requests"], 1)

    def test_content_filter_cannot_commit_history_or_tool_execution(self):
        for name in ("round-00-history.json", "round-00-tool-000-started.json"):
            with self.subTest(receipt=name):
                directory, history, _ = self.journal(finish="content_filter")
                tool_recovery.save_receipt(directory/name, history)
                self.assertEqual(self.inspect(directory)["state"], "RECORDS_INVALID")

    def test_older_receipts_without_optional_fields_remain_readable(self):
        directory, _, _ = self.journal(legacy=True)
        self.assertEqual(self.inspect(directory)["state"], "FINAL_RESPONSE_SAVED")

    def test_reviewed_tools_preserve_pending_and_unknown_boundaries(self):
        directory, history, completion = self.journal(finish="tool_calls")
        self.assertEqual(self.inspect(directory)["state"], "TOOLS_PENDING")
        plan = [asdict(p) for p in chat_client.prepare_tool_calls(completion,
            {"read_project_record": lambda _: None})]
        tool_recovery.save_receipt(directory/"round-00-plan.json", plan)
        tool_recovery.save_receipt(directory/"round-00-tool-000-started.json", plan[0])
        result = self.inspect(directory)
        self.assertEqual(result["state"], "TOOL_OUTCOME_UNKNOWN")
        self.assertEqual(result["attempted_tool_calls"], 1)
        self.assertEqual(result["messages"], history)


if __name__ == "__main__":
    unittest.main()
