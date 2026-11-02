"""Small standard-library client for Q4T's parsed, single-choice chat API.

Development candidate; see docs/AGENT_TOOL_CLIENT_2026-11-02.md. No requests,
tool execution, retries, or model imports happen when this module is imported.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import secrets
import urllib.error
import urllib.parse
import urllib.request


MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_TOOL_CALLS = 128


class ProtocolError(ValueError):
    """The response cannot be committed as a complete assistant turn."""


class RemoteError(RuntimeError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        value = body
        if isinstance(value, (str, bytes)):
            try:
                value = load_json(value)
            except ProtocolError:
                value = {}
        if isinstance(value, dict) and "error" in value:
            value = value["error"]
        value = value if isinstance(value, dict) else {}
        self.code = value.get("code")
        self.type = value.get("type")
        self.param = value.get("param")
        self.message = value.get("message")
        # HTTP 200 can carry an SSE error. Neither it nor 5xx proves that
        # generation or an application tool is safe to execute again.
        self.automatic_retry_safe = False
        super().__init__(f"Remote response failed ({status}): {body}")


def require(condition, message):
    if not condition:
        raise ProtocolError(message)


def _text(value, label, nonempty=False):
    require(isinstance(value, str) and (not nonempty or value), label)
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ProtocolError(label + ": invalid Unicode") from error
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: " + key)
        result[key] = value
    return result


def _float(value):
    result = float(value)
    require(math.isfinite(result), "non-finite JSON number")
    return result


def _constant(value):
    raise ProtocolError("non-JSON number: " + value)


def load_json(value):
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        return json.loads(value, object_pairs_hook=_object, parse_float=_float,
                          parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ProtocolError("invalid JSON: " + str(error)) from error


def _arguments(value):
    _text(value, "tool arguments must be a JSON string")
    args = load_json(value)
    require(isinstance(args, dict), "tool arguments must contain an object")
    return args


def _usage(value):
    require(isinstance(value, dict), "missing usage")
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        require(type(value.get(key)) is int and value[key] >= 0,
                "invalid usage field: " + key)
    require(value["total_tokens"] ==
            value["prompt_tokens"] + value["completion_tokens"],
            "inconsistent usage total")
    if "prompt_tokens_details" in value:
        details = value["prompt_tokens_details"]
        require(isinstance(details, dict), "invalid prompt token details")
        cached = details.get("cached_tokens")
        require(type(cached) is int and 0 <= cached <= value["prompt_tokens"],
                "invalid cached token count")
    return deepcopy(value)


def _message(value):
    require(isinstance(value, dict) and value.get("role") == "assistant",
            "expected assistant message")
    require(not (set(value) - {"role", "content", "reasoning_content",
                               "tool_calls"}), "unsupported message field")
    require("content" in value, "missing message content")
    if value["content"] is not None:
        _text(value["content"], "invalid content")
    if "reasoning_content" in value:
        _text(value["reasoning_content"], "invalid reasoning content")
    calls = value.get("tool_calls", [])
    require(isinstance(calls, list) and len(calls) <= MAX_TOOL_CALLS,
            "invalid tool call list")
    ids = set()
    for call in calls:
        require(isinstance(call, dict) and call.get("type") == "function",
                "expected function tool call")
        identifier = _text(call.get("id"), "missing tool call id", True)
        require(identifier not in ids, "duplicate tool call id")
        ids.add(identifier)
        function = call.get("function")
        require(isinstance(function, dict), "missing function")
        _text(function.get("name"), "missing function name", True)
        _arguments(function.get("arguments"))
    return deepcopy(value)


@dataclass(frozen=True)
class Completion:
    id: str
    model: str
    message: dict
    finish_reason: str
    usage: dict


def _completion(identifier, model, message, finish, usage):
    _text(identifier, "missing response id", True)
    _text(model, "missing response model", True)
    message = _message(message)
    require(finish in ("stop", "tool_calls", "length"),
            "unsupported or missing finish_reason")
    has_tools = bool(message.get("tool_calls"))
    require(finish != "tool_calls" or has_tools, "tool finish without calls")
    require(finish != "stop" or not has_tools, "tool calls with stop finish")
    return Completion(identifier, model, message, finish, _usage(usage))


def _envelope(value, kind):
    require(isinstance(value, dict), "response must be an object")
    if "error" in value:
        raise RemoteError(200, value["error"])
    require(value.get("object") == kind, "unexpected response object")
    _text(value.get("id"), "missing response id", True)
    _text(value.get("model"), "missing response model", True)
    require(isinstance(value.get("choices"), list), "missing choices")


def _choice(choices):
    require(len(choices) == 1 and isinstance(choices[0], dict),
            "expected one choice")
    choice = choices[0]
    require(type(choice.get("index")) is int and choice["index"] == 0,
            "expected choice index zero")
    return choice


class _Stream:
    def __init__(self):
        self.identifier = self.model = self.finish = self.usage = None
        self.role = False
        self.content, self.reasoning, self.calls = [], [], []

    def feed(self, event):
        _envelope(event, "chat.completion.chunk")
        identity = (event["id"], event["model"])
        if self.identifier is None:
            self.identifier, self.model = identity
        require(identity == (self.identifier, self.model),
                "response identity changed mid-stream")
        choices = event["choices"]
        if not choices:
            require(self.finish is not None and self.usage is None,
                    "unexpected or duplicate usage terminal")
            self.usage = _usage(event.get("usage"))
            return
        require(self.finish is None and event.get("usage") is None,
                "choice after terminal or usage on a choice")
        choice = _choice(choices)
        delta = choice.get("delta")
        require(isinstance(delta, dict), "missing delta")
        require(not (set(delta) - {"role", "content", "reasoning_content",
                                   "tool_calls"}), "unsupported delta field")
        if "role" in delta:
            require(not self.role and delta["role"] == "assistant",
                    "invalid or duplicate assistant role")
            self.role = True
        require(self.role, "delta before assistant role")
        for name, target in (("content", self.content),
                             ("reasoning_content", self.reasoning)):
            if delta.get(name) is not None:
                target.append(_text(delta[name], "invalid " + name))
        calls = delta.get("tool_calls", [])
        require(isinstance(calls, list), "invalid tool delta list")
        for call in calls:
            self.tool(call)
        finish = choice.get("finish_reason")
        if finish is not None:
            require(finish in ("stop", "tool_calls", "length"),
                    "unsupported finish_reason")
            self.finish = finish

    def tool(self, delta):
        require(isinstance(delta, dict), "invalid tool delta")
        index = delta.get("index")
        require(type(index) is int and 0 <= index <= len(self.calls),
                "non-contiguous tool index")
        function = delta.get("function", {})
        require(isinstance(function, dict), "invalid tool function delta")
        if index == len(self.calls):
            require(index < MAX_TOOL_CALLS and delta.get("type") == "function",
                    "invalid tool type or too many calls")
            identifier = _text(delta.get("id"), "missing tool id", True)
            require(all(c["id"] != identifier for c in self.calls),
                    "duplicate tool id")
            _text(function.get("name"), "missing function name", True)
            self.calls.append(dict(id=identifier, type="function", name=[],
                                   arguments=[]))
        call = self.calls[index]
        require(delta.get("id", call["id"]) == call["id"] and
                delta.get("type", "function") == "function",
                "tool identity changed mid-stream")
        for name in ("name", "arguments"):
            if name in function:
                call[name].append(_text(function[name], "invalid tool " + name))

    def result(self):
        require(self.role and self.finish is not None and self.usage is not None,
                "incomplete SSE terminal")
        content, reasoning = "".join(self.content), "".join(self.reasoning)
        message = dict(role="assistant", content=content)
        if reasoning:
            message["reasoning_content"] = reasoning
        if self.calls:
            message["tool_calls"] = [dict(id=c["id"], type="function",
                function=dict(name="".join(c["name"]),
                              arguments="".join(c["arguments"])))
                for c in self.calls]
            if not content:
                message["content"] = None
        return _completion(self.identifier, self.model, message,
                           self.finish, self.usage)


def read_sse(response, *, sink=None, max_bytes=MAX_RESPONSE_BYTES,
             max_event_bytes=MAX_EVENT_BYTES):
    """Return only after successful terminal AND EOF; no partial tool callback."""
    require(type(max_bytes) is int and max_bytes > 0 and
            type(max_event_bytes) is int and max_event_bytes > 0,
            "byte limits must be positive integers")
    state, data, done = _Stream(), [], False
    total = event_size = 0
    while True:
        raw = response.readline(min(max_event_bytes, max_bytes - total) + 1)
        if not raw:
            break
        total += len(raw)
        require(total <= max_bytes, "response byte budget exceeded")
        event_size += len(raw)
        require(event_size <= max_event_bytes, "SSE event byte budget exceeded")
        if sink:
            sink(raw)
        require(raw.endswith(b"\n"), "incomplete or oversized SSE line")
        line = raw[:-1].removesuffix(b"\r")
        if not line:
            if data:
                require(not done, "SSE event after DONE")
                payload = b"\n".join(data)
                if payload == b"[DONE]":
                    require(state.finish is not None and state.usage is not None,
                            "DONE before finish and usage")
                    done = True
                else:
                    state.feed(load_json(payload))
            data, event_size = [], 0
        elif line.startswith(b":"):
            continue
        elif line.startswith(b"data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(b" ") else value)
        else:
            raise ProtocolError("unsupported SSE field")
    require(done and not data, "missing DONE or incomplete SSE event at EOF")
    return state.result()


def read_json(response, *, sink=None, max_bytes=MAX_RESPONSE_BYTES):
    require(type(max_bytes) is int and max_bytes > 0,
            "byte limit must be a positive integer")
    raw = response.read(max_bytes + 1)
    require(len(raw) <= max_bytes, "response byte budget exceeded")
    if sink:
        sink(raw)
    event = load_json(raw)
    _envelope(event, "chat.completion")
    choice = _choice(event["choices"])
    return _completion(event["id"], event["model"], choice.get("message"),
                       choice.get("finish_reason"), event.get("usage"))


def make_request(messages, tools, *, model="qwen3.8-flash-next", stream=True,
                 thinking=False, max_tokens=512, timeout_ms=120000,
                 session_id=None):
    require(isinstance(messages, list) and messages, "messages are required")
    require(isinstance(tools, list), "tools must be a list")
    require(type(stream) is bool and type(thinking) is bool, "invalid mode")
    require(type(max_tokens) is int and max_tokens > 0, "invalid output budget")
    require(type(timeout_ms) is int and 1 <= timeout_ms <= 1200000,
            "invalid request deadline")
    body = dict(model=model, messages=deepcopy(messages), tools=deepcopy(tools),
                parse_response=True, temperature=0, top_p=1, n=1,
                stream=stream, max_tokens=max_tokens, tool_choice="auto",
                parallel_tool_calls=True, request_timeout_ms=timeout_ms,
                request_id="agent-" + secrets.token_hex(16),
                cancel_token=secrets.token_hex(32), chat_template_kwargs=dict(
                    enable_thinking=thinking, preserve_thinking=True,
                    reasoning_effort="low"))
    if stream:
        body["stream_options"] = dict(include_usage=True)
    if session_id is not None:
        require(isinstance(session_id, str) and len(session_id) == 64 and
                all(c in "0123456789abcdef" for c in session_id),
                "invalid session capability")
        body["session_id"] = session_id
    return body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_capabilities(value, model, *, protocol_sha256=None,
                          template_sha256=None):
    """Validate the features this client needs; this is not release approval."""
    require(isinstance(value, dict) and
            value.get("object") == "q4t.capabilities" and
            type(value.get("schema_version")) is int and
            value["schema_version"] == 1, "unsupported capability document")
    require(value.get("model") == model, "capability model mismatch")
    chat, tools, template = (value.get(key) for key in
                             ("chat", "tools", "template"))
    require(all(isinstance(item, dict) for item in (chat, tools, template)),
            "missing capability sections")
    for key in ("messages", "reasoning_content", "tool_calls", "stream",
                "stream_include_usage"):
        require(chat.get(key) is True, "required chat capability: " + key)
    require(tools.get("type") == "function" and
            tools.get("execution") == "client" and
            isinstance(tools.get("tool_choice"), list) and
            all(isinstance(item, str) for item in tools["tool_choice"]) and
            "auto" in tools["tool_choice"] and
            isinstance(tools.get("parallel_tool_calls"), list) and
            all(type(item) is bool for item in tools["parallel_tool_calls"]) and
            any(item is True for item in tools["parallel_tool_calls"]),
            "incompatible tool protocol")
    require(isinstance(template.get("id"), str) and template["id"],
            "missing template identity")
    for section, keys in ((value, ("protocol_source_sha256",)), (template,
            ("reference_sha256", "renderer_source_sha256",
             "tool_history_source_sha256", "fixture_sha256"))):
        for key in keys:
            text = section.get(key)
            require(isinstance(text, str) and len(text) == 64 and
                    all(c in "0123456789abcdef" for c in text),
                    "invalid capability identity: " + key)
    require(protocol_sha256 is None or
            value["protocol_source_sha256"] == protocol_sha256,
            "protocol source identity mismatch")
    require(template_sha256 is None or
            template["reference_sha256"] == template_sha256,
            "native template reference mismatch")
    return deepcopy(value)


class ChatClient:
    def __init__(self, base_url, *, api_key=None):
        url = urllib.parse.urlsplit(base_url)
        require(url.scheme in ("http", "https") and url.hostname and
                not url.username and not url.password and not url.query and
                not url.fragment and url.path.rstrip("/") in ("", "/v1"),
                "base URL must be an http(s) origin or /v1 endpoint")
        self.base = urllib.parse.urlunsplit((url.scheme, url.netloc, "/v1", "", ""))
        self.api_key = api_key
        self.opener = urllib.request.build_opener(_NoRedirect())

    def _post(self, path, body, timeout):
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        require(len(data) <= 16 * 1024 * 1024, "request exceeds 16 MiB")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(self.base + path, data=data,
                                         headers=headers, method="POST")
        return self._open(request, timeout)

    def _open(self, request, timeout):
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            with error:
                raw = error.read(65536).decode("utf-8", errors="replace")
            raise RemoteError(error.code, raw) from error

    def capabilities(self, *, model="qwen3.8-flash-next", protocol_sha256=None,
                     template_sha256=None):
        """Explicit GET; 404/invalid metadata never silently assumes support."""
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(self.base + "/capabilities",
                                         headers=headers, method="GET")
        with self._open(request, 10) as response:
            require(response.status == 200 and
                    response.headers.get_content_type() == "application/json",
                    "unexpected capability response")
            raw = response.read(65537)
            require(len(raw) <= 65536, "oversized capability response")
        return validate_capabilities(load_json(raw), model,
            protocol_sha256=protocol_sha256, template_sha256=template_sha256)

    def complete(self, body, *, sink=None):
        require(body.get("parse_response") is True and "messages" in body,
                "this client requires parsed messages")
        stream = body.get("stream", False)
        require(type(stream) is bool, "invalid stream flag")
        if stream:
            require(body.get("stream_options") == {"include_usage": True},
                    "this client requires stream usage")
        timeout = body.get("request_timeout_ms")
        require(type(timeout) is int and 1 <= timeout <= 1200000,
                "explicit bounded request deadline required")
        with self._post("/chat/completions", body, timeout / 1000 + 10) as response:
            require(response.status == 200, "unexpected HTTP success status")
            expected = "text/event-stream" if stream else "application/json"
            require(response.headers.get_content_type() == expected,
                    "unexpected response content type")
            result = (read_sse if stream else read_json)(response, sink=sink)
        require(result.id == body.get("request_id") and
                result.model == body.get("model"), "response identity mismatch")
        return result

    def cancel(self, body):
        """Explicit cancellation only; 202 is acceptance, not completed cleanup."""
        payload = {key: body[key] for key in ("request_id", "cancel_token")}
        with self._post("/requests/cancel", payload, 10) as response:
            require(response.status == 202, "cancellation was not accepted")
            raw = response.read(65537)
            require(len(raw) <= 65536, "oversized cancel response")
            return load_json(raw)


@dataclass(frozen=True)
class ToolInvocation:
    call_id: str
    name: str
    arguments: dict


def prepare_tool_calls(completion, validators):
    """Validate the entire batch without executing any tool or changing history.

    validators maps allowed names to pure checks: return None or raise.
    The application, not this helper, owns execution and any side effects.
    """
    require(completion.finish_reason == "tool_calls", "not a completed tool turn")
    calls = _message(completion.message).get("tool_calls", [])
    require(calls, "missing tool calls")
    planned = []
    for call in calls:
        name = call["function"]["name"]
        require(name in validators and callable(validators[name]),
                "unregistered tool: " + name)
        args = _arguments(call["function"]["arguments"])
        verdict = validators[name](deepcopy(args))
        require(verdict is None, "tool validator must return None or raise")
        planned.append(ToolInvocation(call["id"], name, args))
    return planned


def append_tool_results(history, completion, results):
    require(completion.finish_reason == "tool_calls", "not a completed tool turn")
    message = _message(completion.message)
    calls = message.get("tool_calls", [])
    require(calls and isinstance(results, dict) and
            set(results) == {c["id"] for c in calls},
            "provide exactly one result for every tool call id")
    following = [message]
    for call in calls:
        content = _text(results[call["id"]], "tool results must be text")
        following.append(dict(role="tool", tool_call_id=call["id"],
                              content=content))
    return deepcopy(history) + following


def append_final(history, completion):
    require(completion.finish_reason == "stop" and
            not completion.message.get("tool_calls"),
            "length or tool turn is not a final answer")
    return deepcopy(history) + [_message(completion.message)]
