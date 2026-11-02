"""Offline client contracts; no network, tokenizer, model, or GPU dependencies."""
from copy import deepcopy
from dataclasses import asdict
from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import agent_tools_demo as demo
import chat_client as client


USAGE = dict(prompt_tokens=10, completion_tokens=4, total_tokens=14,
             prompt_tokens_details=dict(cached_tokens=3))
CALL = dict(id="call-0", type="function", function=dict(
    name="lookup", arguments='{"key":"audit"}'))
MESSAGE = dict(role="assistant", content=None, reasoning_content="思考\n",
               tool_calls=[CALL])


def frame(event):
    return b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\r\n\r\n"


def chunk(delta, finish=None, identifier="request-1"):
    return dict(id=identifier, model="qwen3.8-flash-next",
        object="chat.completion.chunk", choices=[dict(index=0, delta=delta,
                                                      finish_reason=finish)])


def stream(deltas=(), *, finish="tool_calls", identifier="request-1"):
    events = [chunk({"role": "assistant", "content": ""}, identifier=identifier)]
    events.extend(chunk(d, identifier=identifier) for d in deltas)
    events.append(chunk({}, finish, identifier))
    events.append(dict(id=identifier, model="qwen3.8-flash-next",
        object="chat.completion.chunk", choices=[], usage=deepcopy(USAGE)))
    return b"".join(frame(e) for e in events) + b"data: [DONE]\r\n\r\n"


def tool_deltas():
    return [{"reasoning_content": "思考\n"},
            {"tool_calls": [dict(index=0, **deepcopy(CALL))]}]


def whole(message=MESSAGE, finish="tool_calls", identifier="request-1"):
    return dict(id=identifier, model="qwen3.8-flash-next",
        object="chat.completion", choices=[dict(index=0,
        message=deepcopy(message), finish_reason=finish)], usage=deepcopy(USAGE))


def completion(message=MESSAGE, finish="tool_calls", identifier="request-1"):
    return client.read_json(io.BytesIO(json.dumps(
        whole(message, finish, identifier), ensure_ascii=False).encode()))


def capabilities():
    return dict(object="q4t.capabilities", schema_version=1,
        model="qwen3.8-flash-next", protocol_source_sha256="a" * 64,
        chat=dict(messages=True, reasoning_content=True, tool_calls=True,
                  stream=True, stream_include_usage=True),
        tools=dict(type="function", execution="client", tool_choice=["auto"],
                   parallel_tool_calls=[True]),
        template=dict(id="qwen4_exp-native-v1", reference_sha256="b" * 64,
                      renderer_source_sha256="c" * 64,
                      tool_history_source_sha256="d" * 64,
                      fixture_sha256="e" * 64))


class Response(io.BytesIO):
    def __init__(self, data, mime="text/event-stream", status=200):
        super().__init__(data)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = mime


class ClientContracts(unittest.TestCase):
    def setUp(self):
        # These contracts must remain offline even if a future edit sends HTTP.
        self.network = patch("urllib.request.OpenerDirector.open",
                             side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_stream_and_json_preserve_message_and_usage(self):
        raw = stream(tool_deltas())
        captured = io.BytesIO()
        result = client.read_sse(io.BytesIO(raw), sink=captured.write)
        self.assertEqual(result, completion())
        self.assertEqual(result.message, MESSAGE)
        self.assertEqual(captured.getvalue(), raw)
        self.assertEqual(USAGE, result.usage)

    def test_reasoning_alias_roundtrip_and_hidden_output(self):
        message = deepcopy(MESSAGE)
        message["reasoning"] = message.pop("reasoning_content")
        deltas = [{"reasoning": "思"}, {"reasoning": "考\n"},
                  {"tool_calls": [dict(index=0, **deepcopy(CALL))]}]
        result = client.read_sse(io.BytesIO(stream(deltas)))
        self.assertEqual(result, completion(message))
        history = [{"role": "user", "content": "query"}]
        following = client.append_tool_results(history, result,
                                               {"call-0": "result"})
        self.assertEqual(following[1], message)
        hidden = deepcopy(message)
        del hidden["reasoning"]
        self.assertEqual(client.read_sse(io.BytesIO(stream(deltas[2:]))),
                         completion(hidden))
        self.assertEqual(result.usage, USAGE)

    def test_reasoning_fields_cannot_mix_or_switch(self):
        for values in (("same", "same"), ("", ""), ("first", "second")):
            for fields in (("reasoning", "reasoning_content"),
                           ("reasoning_content", "reasoning")):
                mixed = dict(zip(fields, values))
                message = dict(role="assistant", content="answer", **mixed)
                with self.assertRaises(client.ProtocolError):
                    completion(message, "stop")
                for deltas in ([mixed], [{k: v} for k, v in mixed.items()]):
                    with self.assertRaises(client.ProtocolError):
                        client.read_sse(io.BytesIO(stream(deltas, finish="stop")))
        # Null placeholders are not a second active reasoning field.
        self.assertEqual(completion(dict(role="assistant", content="answer",
            reasoning="why", reasoning_content=None), "stop").message["reasoning"],
            "why")

    def test_reasoning_request_options_are_explicit(self):
        history = [dict(role="user", content="query")]
        default = client.make_request(history, [])
        self.assertNotIn("reasoning_field", default)
        self.assertNotIn("include_reasoning", default)
        request = client.make_request(history, [], reasoning_field="reasoning",
                                      include_reasoning=False)
        self.assertEqual(request["reasoning_field"], "reasoning")
        self.assertIs(request["include_reasoning"], False)
        for kwargs in (dict(reasoning_field="unknown"),
                       dict(include_reasoning=0), dict(include_reasoning="false")):
            with self.assertRaises(client.ProtocolError):
                client.make_request(history, [], **kwargs)

    def test_thinking_budget_input_and_usage(self):
        body = client.make_request([dict(role="user", content="q")], [],
                                   thinking_token_budget=0)
        self.assertEqual(body["thinking_token_budget"], 0)
        for bad in (True, -1, 1.1, 2147483648, "1"):
            with self.assertRaises(client.ProtocolError):
                client.make_request([dict(role="user", content="q")], [],
                                    thinking_token_budget=bad)
        usage = dict(prompt_tokens=10, completion_tokens=9, total_tokens=19,
            completion_tokens_details=dict(reasoning_tokens=6),
            q4t_thinking_budget=dict(limit=5, consumed=5, forced_closure_tokens=1))
        self.assertEqual(client._usage(usage), usage)
        for changes in (dict(consumed=6), dict(forced_closure_tokens=15),
                        dict(limit=True), dict(consumed=-1)):
            invalid = deepcopy(usage)
            invalid["q4t_thinking_budget"].update(changes)
            with self.assertRaises(client.ProtocolError):
                client._usage(invalid)
        for reasoning in (True, -1, 10, None):
            invalid = deepcopy(usage)
            invalid["completion_tokens_details"]["reasoning_tokens"] = reasoning
            with self.assertRaises(client.ProtocolError):
                client._usage(invalid)

    def test_fragmented_interleaved_tools_and_unicode(self):
        deltas = [{"content": "前言\n", "reasoning_content": "思"},
            {"reasoning_content": "考\n", "tool_calls": [dict(index=0,
                id="one", type="function", function=dict(name="look", arguments='{'))]},
            {"tool_calls": [dict(index=1, id="two", type="function",
                function=dict(name="lookup", arguments='{"key":"第二个"}'))]},
            {"tool_calls": [dict(index=0, function=dict(name="up",
                arguments='"key":"audit"}'))]}]
        result = client.read_sse(io.BytesIO(stream(deltas)))
        self.assertEqual(result.message["content"], "前言\n")
        self.assertEqual(result.message["reasoning_content"], "思考\n")
        calls = result.message["tool_calls"]
        self.assertEqual([c["id"] for c in calls], ["one", "two"])
        self.assertEqual(calls[0]["function"], CALL["function"])
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]),
                         {"key": "第二个"})

    def test_lf_multiline_data_and_comments(self):
        raw = stream([{"content": "你好"}], finish="stop").replace(b"\r\n", b"\n")
        first, rest = raw.split(b"\n\n", 1)
        event = json.loads(first[6:])
        first = b"\n".join(b"data: " + line.encode() for line in
                           json.dumps(event, indent=2).splitlines())
        raw = b": keepalive\n\n" + first + b"\n\n" + rest
        result = client.read_sse(io.BytesIO(raw))
        self.assertEqual(result.message, {"role": "assistant", "content": "你好"})

    def test_terminal_failures_never_return_a_completion(self):
        raw = stream(tool_deltas())
        packets = raw.split(b"\r\n\r\n")
        invalid = {
            "missing_done": raw.removesuffix(b"data: [DONE]\r\n\r\n"),
            "partial_done": raw[:-2],
            "missing_usage": b"\r\n\r\n".join(packets[:-3] + packets[-2:]),
            "early_done": b"data: [DONE]\r\n\r\n",
            "duplicate_done": raw + b"data: [DONE]\r\n\r\n",
            "event_after_done": raw + frame(chunk({"content": "late"})),
            "duplicate_finish": raw.replace(packets[-3],
                frame(chunk({}, "tool_calls")) + packets[-3]),
        }
        for name, wire in invalid.items():
            with self.subTest(name=name), self.assertRaises(client.ProtocolError):
                client.read_sse(io.BytesIO(wire))

    def test_error_after_complete_call_is_failure(self):
        wire = stream(tool_deltas()).replace(b"data: [DONE]", frame({
            "error": {"code": "response_parse_failed", "message": "truncated"}})
            + b"data: [DONE]")
        with self.assertRaises(client.RemoteError):
            client.read_sse(io.BytesIO(wire))

    def test_must_read_through_eof(self):
        class BrokenEof(io.BytesIO):
            def readline(self, size=-1):
                value = super().readline(size)
                if not value:
                    raise ConnectionResetError("lost connection before EOF")
                return value
        with self.assertRaises(ConnectionResetError):
            client.read_sse(BrokenEof(stream(tool_deltas())))

    def test_stream_identity_order_and_types(self):
        invalid = [
            [{"tool_calls": [dict(index=1, **CALL)]}],
            [{"tool_calls": [dict(index=True, **CALL)]}],
            [{"tool_calls": [dict(index=0, **CALL), dict(index=1, **CALL)]}],
            [{"content": 2}],
            [{"role": "assistant"}],
            [{"function_call": {"name": "lookup"}}],
        ]
        for delta in invalid:
            with self.subTest(delta=delta), self.assertRaises(client.ProtocolError):
                client.read_sse(io.BytesIO(stream(delta)))
        wire = stream(tool_deltas()).replace(b'"request-1"', b'"changed"', 1)
        with self.assertRaises(client.ProtocolError):
            client.read_sse(io.BytesIO(wire))

    def test_tool_and_usage_validation(self):
        for args in ('[]', '{"key":1,"key":2}', '{"x":NaN}',
                     '{"x":Infinity}', '{"x":1e999}', '{"key":'):
            value = deepcopy(MESSAGE)
            value["tool_calls"][0]["function"]["arguments"] = args
            with self.subTest(args=args), self.assertRaises(client.ProtocolError):
                completion(value)
        for change in ({"total_tokens": 15}, {"prompt_tokens": True},
                       {"prompt_tokens_details": {"cached_tokens": 11}}):
            event = whole()
            event["usage"].update(change)
            with self.subTest(change=change), self.assertRaises(client.ProtocolError):
                client.read_json(io.BytesIO(json.dumps(event).encode()))
        for event in (whole(MESSAGE, "stop"),
                      whole(dict(role="assistant", content=""), "tool_calls")):
            with self.assertRaises(client.ProtocolError):
                client.read_json(io.BytesIO(json.dumps(event).encode()))

    def test_byte_limits_and_invalid_utf8(self):
        wire = stream(tool_deltas())
        for options in ({"max_bytes": len(wire) - 1}, {"max_event_bytes": 10}):
            with self.assertRaises(client.ProtocolError):
                client.read_sse(io.BytesIO(wire), **options)
        invalid = stream([{"content": "valid"}], finish="stop").replace(b"valid", b"\xff")
        with self.assertRaises(client.ProtocolError):
            client.read_sse(io.BytesIO(invalid))
        with self.assertRaises(client.ProtocolError):
            client.read_json(io.BytesIO(b" " * 20), max_bytes=10)
        for invalid_limit in (0, -1, True):
            with self.assertRaises(client.ProtocolError):
                client.read_sse(io.BytesIO(wire), max_bytes=invalid_limit)

    def test_length_is_observable_but_never_executable_or_final(self):
        result = completion(MESSAGE, "length")
        self.assertEqual(result.finish_reason, "length")
        with self.assertRaises(client.ProtocolError):
            client.prepare_tool_calls(result, {"lookup": demo.validate_lookup})
        with self.assertRaises(client.ProtocolError):
            client.append_tool_results([], result, {"call-0": "result"})
        with self.assertRaises(client.ProtocolError):
            client.append_final([], result)

    def test_tool_plans_and_full_history_are_copied(self):
        result = completion()
        history = [{"role": "user", "content": "lookup"}]
        validator = MagicMock(side_effect=demo.validate_lookup)
        planned = client.prepare_tool_calls(result, {"lookup": validator})
        self.assertEqual(asdict(planned[0]), dict(call_id="call-0", name="lookup",
                                                arguments={"key": "audit"}))
        following = client.append_tool_results(history, result, {"call-0": "结果"})
        self.assertEqual(following[1], MESSAGE)
        self.assertEqual(following[2], dict(role="tool", tool_call_id="call-0",
                                          content="结果"))
        following[0]["content"] = "changed"
        following[1]["tool_calls"][0]["function"]["arguments"] = "{}"
        self.assertEqual(history[0]["content"], "lookup")
        self.assertEqual(result.message, MESSAGE)
        for results in ({}, {"other-id": "value"}, {"call-0": {"code": 1}}):
            with self.assertRaises(client.ProtocolError):
                client.append_tool_results(history, result, results)

    def test_unknown_tools_and_arguments_rejected_before_dispatch(self):
        with self.assertRaises(client.ProtocolError):
            client.prepare_tool_calls(completion(), {})
        with self.assertRaises(client.ProtocolError):
            client.prepare_tool_calls(completion(), {"lookup": lambda a: False})
        message = deepcopy(MESSAGE)
        message["tool_calls"][0]["function"]["arguments"] = '{"key":"unknown"}'
        with self.assertRaises(client.ProtocolError):
            client.prepare_tool_calls(completion(message), {"lookup": demo.validate_lookup})
        message["tool_calls"][0]["function"]["arguments"] = '{"n":9007199254740993}'
        plans = client.prepare_tool_calls(completion(message), {"lookup": lambda a: None})
        self.assertEqual(plans[0].arguments["n"], 9007199254740993)

    def test_fresh_request_credentials_and_stable_session(self):
        history = [{"role": "user", "content": "你好"}]
        tools = [deepcopy(demo.TOOL)]
        first = client.make_request(history, tools, session_id="a" * 64)
        second = client.make_request(history, tools, session_id="a" * 64)
        self.assertEqual(first["session_id"], second["session_id"])
        for key in ("request_id", "cancel_token"):
            self.assertNotEqual(first[key], second[key])
        history[0]["content"] = "changed"
        tools[0]["function"]["name"] = "changed"
        self.assertEqual(first["messages"][0]["content"], "你好")
        self.assertEqual(first["tools"][0]["function"]["name"], "lookup")

    def test_transport_single_post_and_matching_identity(self):
        request = client.make_request([{"role": "user", "content": "q"}], [])
        for base in ("http://localhost:18096", "http://localhost:18096/v1/"):
            api = client.ChatClient(base)
            api.opener = MagicMock()
            api.opener.open.return_value = Response(stream(
                [{"content": "answer"}], finish="stop", identifier=request["request_id"]))
            result = api.complete(request)
            self.assertEqual(result.message["content"], "answer")
            api.opener.open.assert_called_once()
            sent = api.opener.open.call_args.args[0]
            self.assertEqual(sent.full_url, "http://localhost:18096/v1/chat/completions")
            self.assertEqual(json.loads(sent.data), request)
        for base in ("file:///tmp/socket", "http://x/v1/chat/completions",
                     "http://user:secret@x", "http://x?route=other"):
            with self.assertRaises(client.ProtocolError):
                client.ChatClient(base)
        self.assertEqual(client.ChatClient("http://v1").base, "http://v1/v1")

    def test_transport_failures_do_not_retry(self):
        request = client.make_request([{"role": "user", "content": "q"}], [])
        for response in (Response(b"error", "text/html"),
                         Response(stream(tool_deltas())),
                         urllib.error.HTTPError("http://localhost", 409, "busy",
                                               {}, io.BytesIO(b'{"error":"busy"}'))):
            api = client.ChatClient("http://localhost")
            api.opener = MagicMock()
            if isinstance(response, Exception):
                api.opener.open.side_effect = response
            else:
                api.opener.open.return_value = response
            with self.assertRaises((client.ProtocolError, client.RemoteError)):
                api.complete(request)
            api.opener.open.assert_called_once()

    def test_explicit_cancel_uses_original_credentials(self):
        request = client.make_request([{"role": "user", "content": "q"}], [])
        api = client.ChatClient("http://localhost")
        api.opener = MagicMock()
        api.opener.open.return_value = Response(b'{"status":"cancelling"}',
                                              "application/json", 202)
        self.assertEqual(api.cancel(request), {"status": "cancelling"})
        sent = api.opener.open.call_args.args[0]
        self.assertEqual(sent.full_url, "http://localhost/v1/requests/cancel")
        self.assertEqual(json.loads(sent.data), {key: request[key] for key in
                                              ("request_id", "cancel_token")})

    def test_capability_identity_and_feature_gate(self):
        caps = capabilities()
        self.assertEqual(client.validate_capabilities(caps, caps["model"],
            protocol_sha256="a" * 64, template_sha256="b" * 64), caps)
        for section, key, value in (("chat", "tool_calls", False),
                ("tools", "parallel_tool_calls", [1]),
                ("template", "reference_sha256", "bad")):
            changed = deepcopy(caps)
            changed[section][key] = value
            with self.assertRaises(client.ProtocolError):
                client.validate_capabilities(changed, caps["model"])
        with self.assertRaises(client.ProtocolError):
            client.validate_capabilities(caps, caps["model"], protocol_sha256="f" * 64)
        with self.assertRaises(client.ProtocolError):
            client.validate_capabilities(caps, "another-model")

    def test_capability_discovery_is_one_get(self):
        api = client.ChatClient("http://localhost:18096")
        api.opener = MagicMock()
        api.opener.open.return_value = Response(json.dumps(capabilities()).encode(),
                                                "application/json")
        self.assertEqual(api.capabilities(), capabilities())
        api.opener.open.assert_called_once()
        request = api.opener.open.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.full_url, "http://localhost:18096/v1/capabilities")

    def test_capability_superset_preserves_auto_client(self):
        caps = capabilities()
        caps["tools"]["tool_choice"] = ["auto", "none", "required", "named"]
        caps["tools"]["parallel_tool_calls"] = [True, False]
        self.assertEqual(client.validate_capabilities(caps, caps["model"]), caps)
        for key, value in (("tool_choice", ["required"]),
                           ("tool_choice", ["auto", 1]),
                           ("parallel_tool_calls", [False]),
                           ("parallel_tool_calls", [True, 0])):
            changed = deepcopy(caps)
            changed["tools"][key] = value
            with self.assertRaises(client.ProtocolError):
                client.validate_capabilities(changed, caps["model"])

    def test_capability_failure_does_not_fallback_or_generate(self):
        for response in (Response(b"x" * 65537, "application/json"),
                         Response(b"{}", "text/html"),
                         urllib.error.HTTPError("http://localhost", 404, "missing",
                                               {}, io.BytesIO(b"missing"))):
            api = client.ChatClient("http://localhost")
            api.opener = MagicMock()
            if isinstance(response, Exception):
                api.opener.open.side_effect = response
            else:
                api.opener.open.return_value = response
            with self.assertRaises((client.ProtocolError, client.RemoteError)):
                api.capabilities()
            api.opener.open.assert_called_once()


class DemoContracts(unittest.TestCase):
    def setUp(self):
        network = patch("urllib.request.OpenerDirector.open",
                        side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        root = Path(os.environ.get("Q4T_TEST_OUTPUT_DIR",
                                   demo.ROOT / "build/agent-tool-client-contracts"))
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / ".q4t-work/demo"
        self.patch = patch.object(demo, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_tool_round_trip_preserves_history_and_no_replay(self):
        requests = []
        def complete(body, sink):
            requests.append(deepcopy(body))
            sink(b"recorded response")
            message = MESSAGE if len(requests) == 1 else dict(
                role="assistant", content="831907")
            return completion(message, "tool_calls" if len(requests) == 1 else "stop",
                              body["request_id"])
        api = MagicMock()
        api.complete.side_effect = complete
        api.capabilities.return_value = capabilities()
        with patch.object(demo, "ChatClient", return_value=api):
            answer = demo.run(self.directory, "http://localhost:18096",
                              session_cache=True, max_tokens=256)
            with self.assertRaises(FileExistsError):
                demo.run(self.directory, "http://localhost:18096")
        self.assertEqual(answer, "831907")
        self.assertEqual(len(requests), 2)
        self.assertEqual([r["max_tokens"] for r in requests], [256, 256])
        self.assertEqual(requests[0]["session_id"], requests[1]["session_id"])
        self.assertNotEqual(requests[0]["cancel_token"], requests[1]["cancel_token"])
        self.assertEqual(requests[1]["messages"][-2], MESSAGE)
        self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "call-0")
        self.assertTrue((self.directory / "completed.json").is_file())
        for path in self.directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_partial_stream_never_reaches_tool_plan(self):
        api = MagicMock()
        api.capabilities.return_value = capabilities()
        def fail(body, sink):
            raw = stream(tool_deltas(), identifier=body["request_id"])
            return client.read_sse(io.BytesIO(raw[:-20]), sink=sink)
        api.complete.side_effect = fail
        with patch.object(demo, "ChatClient", return_value=api):
            with self.assertRaises(client.ProtocolError):
                demo.run(self.directory, "http://localhost:18096")
        self.assertFalse(list(self.directory.glob("*-plan.json")))
        self.assertFalse(list(self.directory.glob("*-history.json")))
        self.assertTrue((self.directory / "failed.json").is_file())
        api.complete.assert_called_once()

    def test_reject_invalid_batch_or_last_round_before_any_tool(self):
        message = deepcopy(MESSAGE)
        second = deepcopy(CALL)
        second.update(id="call-1")
        second["function"]["name"] = "unregistered"
        message["tool_calls"].append(second)
        for name, result, rounds in (("unknown", completion(message), 4),
                                      ("budget", completion(), 1)):
            directory = self.directory / name
            api = MagicMock()
            api.capabilities.return_value = capabilities()
            api.complete.return_value = result
            with patch.object(demo, "ChatClient", return_value=api):
                with self.assertRaises(client.ProtocolError):
                    demo.run(directory, "http://localhost:18096", max_rounds=rounds)
            self.assertFalse(list(directory.glob("*-tool-*-started.json")))
            self.assertFalse(list(directory.glob("*-history.json")))
            self.assertFalse((directory / "completed.json").exists())


if __name__ == "__main__":
    unittest.main()
