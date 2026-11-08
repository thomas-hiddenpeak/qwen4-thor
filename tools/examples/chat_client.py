"""Small standard-library client for Q4T's parsed Chat API.

Development candidate; see docs/AGENT_TOOL_CLIENT_2026-11-02.md. No requests,
tool execution, retries, or model imports happen when this module is imported.
"""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
import secrets
import ipaddress
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

try:  # Both package import and the standalone examples entrypoints.
    from . import deadline_http
except ImportError:
    import deadline_http


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


def _exact_number(token):
    require(len(token) <= 16384, "JSON number spelling limit")
    exact = Decimal(token)
    require(exact.is_finite(), "non-JSON number")
    if not any(c in token for c in ".eE") and len(token) <= 4096:
        return int(token)
    rounded = float(exact)
    if math.isfinite(rounded) and Decimal(str(rounded)) == exact:
        return rounded
    return exact


def dump_json(value):
    """Bounded JSON encoder retaining Decimal and arbitrary integer data."""
    nodes = 0
    def encode(item, depth=0):
        nonlocal nodes
        nodes += 1
        require(depth <= 64 and nodes <= 65536, "JSON complexity limit")
        if item is None: return "null"
        if type(item) is bool: return "true" if item else "false"
        if isinstance(item, str):
            return json.dumps(_text(item, "invalid JSON text"), ensure_ascii=False)
        if isinstance(item, Decimal) or type(item) is int:
            if type(item) is int:
                require(item.bit_length() <= 54430, "JSON number spelling limit")
                encoded = str(Decimal(item))
            else:
                require(item.is_finite(), "non-JSON number")
                encoded = str(item)
            require(len(encoded) <= 16384, "JSON number spelling limit")
            return encoded
        if type(item) is float:
            require(math.isfinite(item), "non-JSON number")
            return json.dumps(item, allow_nan=False)
        if isinstance(item, list):
            return "[" + ",".join(encode(v, depth + 1) for v in item) + "]"
        require(isinstance(item, dict), "value must be JSON")
        require(all(isinstance(k, str) for k in item), "JSON keys must be text")
        return "{" + ",".join(encode(k, depth + 1) + ":" + encode(v, depth + 1)
                              for k, v in item.items()) + "}"
    try:
        encoded = encode(value)
        require(len(encoded.encode("utf-8")) <= 16 * 1024 * 1024,
                "JSON exceeds 16 MiB")
        return encoded
    except (ValueError, RecursionError, OverflowError) as error:
        raise ProtocolError("invalid JSON: " + str(error)) from error


def load_json(value, *, exact_numbers=False):
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        return json.loads(value, object_pairs_hook=_object,
                          parse_float=_exact_number if exact_numbers else _float,
                          parse_int=_exact_number if exact_numbers else int,
                          parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError, InvalidOperation,
            OverflowError) as error:
        raise ProtocolError("invalid JSON: " + str(error)) from error


def _arguments(value):
    _text(value, "tool arguments must be a JSON string")
    args = load_json(value, exact_numbers=True)
    require(isinstance(args, dict), "tool arguments must contain an object")
    return args


def _usage(value, choice_count=1):
    require(isinstance(value, dict), "missing usage")
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        require(type(value.get(key)) is int and value[key] >= 0,
                "invalid usage field: " + key)
    require(value["total_tokens"] ==
            value["prompt_tokens"] + value["completion_tokens"],
            "inconsistent usage total")
    details = value.get("completion_tokens_details")
    if details is not None:
        require(isinstance(details, dict), "invalid completion token details")
        reasoning = details.get("reasoning_tokens")
        require(type(reasoning) is int and
                0 <= reasoning <= value["completion_tokens"],
                "invalid reasoning token count")
        prediction_keys = ("accepted_prediction_tokens", "rejected_prediction_tokens")
        if any(key in details for key in prediction_keys):
            require(all(type(details.get(key)) is int and details[key] >= 0
                        for key in prediction_keys) and
                    sum(details[key] for key in prediction_keys) <= value["completion_tokens"],
                    "invalid prediction token count")
    budget = value.get("q4t_thinking_budget")
    if budget is not None:
        require(isinstance(budget, dict) and details is not None,
                "invalid thinking budget accounting")
        for key in ("limit", "consumed", "forced_closure_tokens"):
            require(type(budget.get(key)) is int and budget[key] >= 0,
                    "invalid thinking budget field: " + key)
        require(budget["consumed"] <= budget["limit"] * choice_count and
                budget["limit"] <= 2147483647 and
                budget["forced_closure_tokens"] <= 14 * choice_count and
                budget["consumed"] + budget["forced_closure_tokens"] ==
                details["reasoning_tokens"], "inconsistent thinking budget usage")
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
    require(not (set(value) - {"role", "content", "reasoning_content", "reasoning",
                               "tool_calls", "function_call"}), "unsupported message field")
    require("content" in value, "missing message content")
    if value["content"] is not None:
        _text(value["content"], "invalid content")
    reasoning_fields = [key for key in ("reasoning_content", "reasoning")
                        if value.get(key) is not None]
    require(len(reasoning_fields) <= 1, "mixed reasoning response fields")
    for key in reasoning_fields:
        _text(value[key], "invalid " + key)
    calls = value.get("tool_calls", [])
    require(isinstance(calls, list) and len(calls) <= MAX_TOOL_CALLS,
            "invalid tool call list")
    ids = set()
    function = value.get("function_call")
    if function is not None:
        require(not calls and isinstance(function, dict) and
                set(function) == {"name", "arguments"}, "invalid legacy function call")
        _text(function.get("name"), "missing function name", True)
        _arguments(function.get("arguments"))
    for call in calls:
        require(isinstance(call, dict) and call.get("type") in ("function", "custom"),
                "expected function or custom tool call")
        kind = call["type"]
        require(set(call) == {"id", "type", kind}, "invalid tool call fields")
        identifier = _text(call.get("id"), "missing tool call id", True)
        require(identifier not in ids, "duplicate tool call id")
        ids.add(identifier)
        value_field = "input" if kind == "custom" else "arguments"
        definition = call.get(kind)
        require(isinstance(definition, dict) and set(definition) == {"name", value_field},
                "invalid tool payload")
        _text(definition.get("name"), "missing tool name", True)
        if kind == "custom":
            _text(definition.get("input"), "custom input must be text")
        else:
            _arguments(definition.get("arguments"))
    return deepcopy(value)


def _logprobs(value):
    if value is None:
        return None
    require(isinstance(value, dict) and set(value) == {"content", "refusal"},
            "invalid logprobs container")

    def token(item, top=False):
        required = {"token", "logprob", "bytes"} | (set() if top else {"top_logprobs"})
        require(isinstance(item, dict) and set(item) == required,
                "invalid token logprobs")
        _text(item["token"], "invalid token string")
        probability = item["logprob"]
        require(type(probability) in (int, float) and math.isfinite(probability)
                and probability <= 0, "invalid token probability")
        require(item["bytes"] is None or (isinstance(item["bytes"], list) and
            all(type(byte) is int and 0 <= byte <= 255 for byte in item["bytes"])),
            "invalid token bytes")
        if not top:
            require(isinstance(item["top_logprobs"], list) and
                    len(item["top_logprobs"]) <= 20, "invalid alternatives")
            for alternative in item["top_logprobs"]:
                token(alternative, True)
    for entries in value.values():
        require(entries is None or isinstance(entries, list), "invalid logprobs list")
        for item in entries or []:
            token(item)
    return deepcopy(value)


@dataclass(frozen=True)
class Completion:
    id: str
    model: str
    message: dict
    finish_reason: str
    usage: dict
    logprobs: dict | None = None


def _completion(identifier, model, message, finish, usage, logprobs=None,
                choice_count=1):
    _text(identifier, "missing response id", True)
    _text(model, "missing response model", True)
    message = _message(message)
    require(finish in ("stop", "tool_calls", "function_call", "length"),
            "unsupported or missing finish_reason")
    has_tools = bool(message.get("tool_calls"))
    require(finish != "tool_calls" or has_tools, "tool finish without calls")
    legacy = message.get("function_call") is not None
    require(finish != "function_call" or legacy, "function finish without call")
    require(not legacy or finish in ("function_call", "length"), "legacy call finish mismatch")
    require(finish != "stop" or not (has_tools or legacy), "tool calls with stop finish")
    return Completion(identifier, model, message, finish, _usage(usage, choice_count),
                      _logprobs(logprobs))


@dataclass(frozen=True)
class MultiCompletion:
    id: str
    model: str
    choices: list
    usage: dict

    def select(self, index):
        require(type(index) is int and 0 <= index < len(self.choices),
                "invalid choice selection")
        choice = self.choices[index]
        return _completion(self.id, self.model, choice["message"],
                           choice["finish_reason"], self.usage,
                           choice["logprobs"], len(self.choices))


def _choices_completion(identifier, model, choices, usage):
    require(isinstance(choices, list) and 1 <= len(choices) <= 128,
            "invalid choice count")
    require(all(isinstance(choice, dict) and type(choice.get("index")) is int
                for choice in choices), "invalid choice index")
    choices = sorted(choices, key=lambda choice: choice["index"])
    require([choice["index"] for choice in choices] == list(range(len(choices))),
            "missing or duplicate choice index")
    result = []
    for choice in choices:
        item = _completion(identifier, model, choice.get("message"),
                           choice.get("finish_reason"), usage,
                           choice.get("logprobs"), len(choices))
        result.append(dict(index=choice["index"], message=item.message,
                           finish_reason=item.finish_reason, logprobs=item.logprobs))
    if len(choices) == 1:
        return item
    return MultiCompletion(identifier, model, result, _usage(usage, len(choices)))


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


class _ChoiceStream:
    def __init__(self):
        self.identifier = self.model = self.finish = self.usage = None
        self.role = False
        self.logprobs = None
        self.choice_count = 1
        self.reasoning_field = None
        self.content, self.reasoning, self.calls = [], [], []
        self.function_call = None

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
        probabilities = _logprobs(choice.get("logprobs"))
        if probabilities is not None:
            if self.logprobs is None:
                self.logprobs = dict(content=[], refusal=None)
            for key in ("content", "refusal"):
                if probabilities[key] is not None:
                    if self.logprobs[key] is None:
                        self.logprobs[key] = []
                    self.logprobs[key].extend(probabilities[key])
        delta = choice.get("delta")
        require(isinstance(delta, dict), "missing delta")
        require(not (set(delta) - {"role", "content", "reasoning_content", "reasoning",
                                   "tool_calls", "function_call"}), "unsupported delta field")
        if "role" in delta:
            require(not self.role and delta["role"] == "assistant",
                    "invalid or duplicate assistant role")
            self.role = True
        require(self.role, "delta before assistant role")
        if delta.get("content") is not None:
            self.content.append(_text(delta["content"], "invalid content"))
        for name in ("reasoning_content", "reasoning"):
            if delta.get(name) is None:
                continue
            require(self.reasoning_field in (None, name),
                    "reasoning response field changed mid-stream")
            self.reasoning_field = name
            self.reasoning.append(_text(delta[name], "invalid " + name))
        calls = delta.get("tool_calls", [])
        require(isinstance(calls, list), "invalid tool delta list")
        for call in calls:
            self.tool(call)
        function = delta.get("function_call")
        if function is not None:
            require(not self.calls and isinstance(function, dict) and
                    not (set(function) - {"name", "arguments"}), "invalid legacy function delta")
            if self.function_call is None:
                self.function_call = dict(name=[], arguments=[])
            for name, value in function.items():
                self.function_call[name].append(_text(value, "invalid function " + name))
        finish = choice.get("finish_reason")
        if finish is not None:
            require(finish in ("stop", "tool_calls", "function_call", "length"),
                    "unsupported finish_reason")
            self.finish = finish

    def tool(self, delta):
        require(self.function_call is None, "mixed function and tool delta dialects")
        require(isinstance(delta, dict), "invalid tool delta")
        index = delta.get("index")
        require(type(index) is int and 0 <= index <= len(self.calls),
                "non-contiguous tool index")
        if index == len(self.calls):
            require(index < MAX_TOOL_CALLS and delta.get("type") in ("function", "custom"),
                    "invalid tool type or too many calls")
            identifier = _text(delta.get("id"), "missing tool id", True)
            require(all(c["id"] != identifier for c in self.calls), "duplicate tool id")
            kind = delta["type"]
            require(isinstance(delta.get(kind), dict), "missing initial tool payload")
            _text(delta[kind].get("name"), "missing tool name", True)
            self.calls.append(dict(id=identifier, type=kind, name=[], value=[]))
        call = self.calls[index]
        kind = call["type"]
        value_field = "input" if kind == "custom" else "arguments"
        require(not (set(delta) - {"index", "id", "type", kind}) and
                delta.get("id", call["id"]) == call["id"] and
                delta.get("type", kind) == kind, "tool identity changed mid-stream")
        definition = delta.get(kind, {})
        require(isinstance(definition, dict) and not (set(definition) - {"name", value_field}),
                "invalid tool delta fields")
        for source, target in (("name", "name"), (value_field, "value")):
            if source in definition:
                call[target].append(_text(definition[source], "invalid tool " + source))

    def result(self):
        require(self.role and self.finish is not None and self.usage is not None,
                "incomplete SSE terminal")
        content, reasoning = "".join(self.content), "".join(self.reasoning)
        message = dict(role="assistant", content=content)
        if reasoning:
            message[self.reasoning_field] = reasoning
        if self.calls:
            message["tool_calls"] = [dict(id=c["id"], type=c["type"], **{c["type"]:
                dict(name="".join(c["name"]), **{
                    "input" if c["type"] == "custom" else "arguments": "".join(c["value"])})})
                for c in self.calls]
            if not content:
                message["content"] = None
        if self.function_call is not None:
            message["function_call"] = {k: "".join(v) for k,v in self.function_call.items()}
            if not content:
                message["content"] = None
        return _completion(self.identifier, self.model, message,
                           self.finish, self.usage, self.logprobs, self.choice_count)


class _Stream:
    def __init__(self):
        self.identity = None
        self.choices = {}
        self.usage = None

    @property
    def finish(self):
        return True if self.choices and all(state.finish is not None
                        for state in self.choices.values()) else None

    def feed(self, event):
        _envelope(event, "chat.completion.chunk")
        identity = (event["id"], event["model"], event.get("created"))
        if self.identity is None:
            self.identity = identity
        require(self.identity == identity, "response identity changed mid-stream")
        choices = event["choices"]
        if not choices:
            require(self.finish is not None and self.usage is None,
                    "unexpected or duplicate usage terminal")
            self.usage = _usage(event.get("usage"), len(self.choices))
            return
        require(self.usage is None and event.get("usage") is None and
                len(choices) <= 128, "choice after usage or mixed usage")
        seen = set()
        for choice in choices:
            require(isinstance(choice, dict), "invalid choice")
            index = choice.get("index")
            require(type(index) is int and 0 <= index < 128 and index not in seen,
                    "invalid or duplicate choice index")
            seen.add(index)
            state = self.choices.setdefault(index, _ChoiceStream())
            state.feed(dict(event, choices=[dict(choice, index=0)]))

    def result(self):
        require(self.finish is not None and self.usage is not None,
                "incomplete SSE terminal")
        values = []
        for index, state in self.choices.items():
            state.usage = self.usage
            state.choice_count = len(self.choices)
            item = state.result()
            values.append(dict(index=index, message=item.message,
                               finish_reason=item.finish_reason, logprobs=item.logprobs))
        return _choices_completion(*self.identity[:2], values, self.usage)


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
    return _choices_completion(event["id"], event["model"], event["choices"],
                               event.get("usage"))


SCHEMA_INSTRUCTION = (
    "Return exactly one JSON object matching the following JSON Schema. "
    "Use its descriptions to interpret the fields and enum values. "
    "Do not include markdown fences.\n")


def with_json_schema(body, schema, *, name="answer"):
    """Opt in to schema semantics AND decoding, for text/media/final tool turns.

    Append to the last user message, preserving actual tool results. This is
    application prompt construction, not a server guarantee of factual truth.
    Tools may remain active; their successful terminal still gates execution.
    Decimal and integer schema values retain exact JSON spelling. No retries.
    """
    require(isinstance(body, dict) and "response_format" not in body,
            "response_format already set or invalid request")
    require(isinstance(schema, dict) and
            (schema.get("type") == "object" or isinstance(schema.get("$ref"), str)),
            "an object JSON Schema is required")
    require(isinstance(name, str) and 1 <= len(name) <= 64 and
            all(c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789_-" for c in name), "invalid schema name")
    try:
        encoded = dump_json(schema)
        require(len(encoded.encode("utf-8")) <= 2 * 1024 * 1024,
                "schema exceeds 2 MiB")
        checked = load_json(encoded, exact_numbers=True)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError) as error:
        raise ProtocolError("invalid JSON schema: " + str(error)) from error
    tools = body.get("tools")
    require(tools is None or (isinstance(tools, list) and len(tools) <= 128),
            "tools must be an array with at most 128 declarations")
    for tool in tools or []:
        require(isinstance(tool, dict) and tool.get("type") in ("function", "custom"),
                "invalid tool definition")
        definition = tool.get(tool["type"])
        require(isinstance(definition, dict) and
                isinstance(definition.get("name"), str) and definition["name"],
                "tool definition requires name")
    require(body.get("parse_response") is not False,
            "schema requires parsed messages")
    messages = body.get("messages")
    require(isinstance(messages, list) and messages and
            all(isinstance(m, dict) for m in messages) and
            any(m.get("role") in ("system", "developer", "user", "assistant")
                for m in messages), "schema requires a valid message history")
    result = deepcopy(body)
    user = next((m for m in reversed(result["messages"])
                 if m.get("role") == "user"), None)
    instruction = SCHEMA_INSTRUCTION + encoded
    if user is None:
        result["messages"].insert(0, dict(role="system", content=instruction))
    elif isinstance(user.get("content"), str):
        user["content"] += "\n\n" + instruction
    else:
        require(isinstance(user.get("content"), list), "invalid user content")
        user["content"].append(dict(type="text", text=instruction))
    result["response_format"] = dict(type="json_schema", json_schema=dict(
        name=name, strict=True, schema=checked))
    return result


def make_request(messages, tools, *, model="qwen3.8-flash-next", stream=True,
                 thinking=False, max_tokens=512, timeout_ms=120000,
                 session_id=None, reasoning_field=None, include_reasoning=None,
                 thinking_token_budget=None, stop=None, json_schema=None,
                 schema_name="answer"):
    require(isinstance(messages, list) and messages, "messages are required")
    require(isinstance(tools, list), "tools must be a list")
    require(type(stream) is bool and type(thinking) is bool, "invalid mode")
    require(type(max_tokens) is int and 1 <= max_tokens <= 2147483647,
            "invalid output budget")
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
    if reasoning_field is not None:
        require(reasoning_field in ("reasoning_content", "reasoning"),
                "invalid reasoning field")
        body["reasoning_field"] = reasoning_field
    if include_reasoning is not None:
        require(type(include_reasoning) is bool, "invalid include_reasoning")
        body["include_reasoning"] = include_reasoning
    if thinking_token_budget is not None:
        require(type(thinking_token_budget) is int and
                0 <= thinking_token_budget <= 2147483647,
                "invalid thinking token budget")
        body["thinking_token_budget"] = thinking_token_budget
    if stop is not None:
        require(isinstance(stop, (str, list)), "invalid stop type")
        stops = [stop] if isinstance(stop, str) else stop
        require(len(stops) <= 4, "stop allows at most 4 strings")
        for item in stops:
            require(isinstance(item, str), "invalid stop entry")
            try:
                length = len(item.encode("utf-8", errors="strict"))
            except UnicodeError as exc:
                raise ProtocolError("stop must be valid UTF-8") from exc
            require(1 <= length <= 256, "invalid stop byte length")
        require(not stops or session_id is None, "stop does not support cache")
        body["stop"] = deepcopy(stop)
    if session_id is not None:
        require(isinstance(session_id, str) and len(session_id) == 64 and
                all(c in "0123456789abcdef" for c in session_id),
                "invalid session capability")
        body["session_id"] = session_id
    return (with_json_schema(body, json_schema, name=schema_name)
            if json_schema is not None else body)


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
    def __init__(self, base_url, *, api_key=None, ca_file=None,
                 connect_host=None, timeout=None):
        url = urllib.parse.urlsplit(base_url)
        require(url.scheme in ("http", "https") and url.hostname and
                not url.username and not url.password and not url.query and
                not url.fragment and url.path.rstrip("/") in ("", "/v1"),
                "base URL must be an http(s) origin or /v1 endpoint")
        self.base = urllib.parse.urlunsplit((url.scheme, url.netloc, "/v1", "", ""))
        self.api_key = api_key
        require(api_key is None or isinstance(api_key, str) and 1 <= len(api_key) <= 4096 and
                all(33 <= ord(c) <= 126 for c in api_key), "invalid API key")
        require(timeout is None or type(timeout) in (int, float) and
                math.isfinite(timeout) and 0 < timeout <= 1210,
                "invalid client network deadline")
        require(ca_file is None or url.scheme == "https", "CA requires HTTPS")
        if connect_host is not None:
            ipaddress.ip_address(connect_host)
        self.timeout = timeout
        context = ssl.create_default_context(cafile=ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            deadline_http.HTTPHandler(connect_host),
            deadline_http.HTTPSHandler(context, connect_host))

    def _post(self, path, body, timeout):
        data = dump_json(body).encode()
        require(len(data) <= 16 * 1024 * 1024, "request exceeds 16 MiB")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(self.base + path, data=data,
                                         headers=headers, method="POST")
        return self._open(request, timeout)

    def _open(self, request, timeout):
        require(type(timeout) in (int, float) and math.isfinite(timeout) and
                timeout > 0, "positive finite request deadline required")
        if self.timeout is not None:
            timeout = min(timeout, self.timeout)
        deadline = request.q4t_deadline = time.monotonic() + timeout
        try:
            return deadline_http.Response(
                self.opener.open(request, timeout=timeout), deadline)
        except urllib.error.HTTPError as error:
            with deadline_http.Response(error, deadline) as response:
                raw = response.read(65536).decode("utf-8", errors="replace")
            raise RemoteError(error.code, raw) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TimeoutError("request network deadline") from error
            raise

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

    @staticmethod
    def _extension(response, *, object_name, profile, max_bytes):
        require(response.status == 200 and
                response.headers.get_content_type() == "application/json",
                "unexpected extension response")
        raw = response.read(max_bytes + 1)
        require(len(raw) <= max_bytes, "oversized extension response")
        result = load_json(raw)
        require(isinstance(result, dict) and result.get("object") == object_name
                and result.get("profile") == profile,
                "unsupported extension profile")
        digest = result.get("protocol_source_sha256")
        require(isinstance(digest, str) and len(digest) == 64 and
                all(c in "0123456789abcdef" for c in digest),
                "invalid extension protocol identity")
        return result

    def configuration(self):
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(self.base + "/config", headers=headers,
                                         method="GET")
        with self._open(request, 10) as response:
            return self._extension(response, object_name="q4t.serve.config",
                                   profile="q4t-config-v1", max_bytes=65536)

    def preflight(self, body, *, include_prompt=False, include_token_ids=False,
                  protocol_sha256=None):
        """Read-only planning; never executes, reserves, or automatically retries."""
        require(isinstance(body, dict) and type(include_prompt) is bool and
                type(include_token_ids) is bool, "invalid preflight arguments")
        envelope = dict(request=body, include_prompt=include_prompt,
                        include_token_ids=include_token_ids)
        with self._post("/chat/preflight", envelope, 30) as response:
            result = self._extension(response, object_name="q4t.chat.preflight",
                profile="q4t-preflight-v1", max_bytes=8 * 1024 * 1024)
        require(result.get("execution_guaranteed") is False,
                "preflight cannot guarantee execution")
        count = result.get("prompt_tokens")
        require(type(count) is int and count > 0, "invalid preflight token count")
        require(("prompt" in result) == include_prompt and
                ("token_ids" in result) == include_token_ids,
                "preview selection mismatch")
        if include_prompt:
            _text(result["prompt"], "preflight prompt", nonempty=True)
        if include_token_ids:
            ids = result["token_ids"]
            require(isinstance(ids, list) and len(ids) == count and
                    all(type(n) is int and n >= 0 for n in ids),
                    "invalid preflight token IDs")
        if "model" in body:
            require(result.get("model") == body["model"], "preflight model mismatch")
        if protocol_sha256 is not None:
            require(result["protocol_source_sha256"] == protocol_sha256,
                    "preflight protocol identity mismatch")
        return result

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
        require((len(result.choices) if isinstance(result, MultiCompletion) else 1) ==
                (body.get("n") or 1), "response choice count mismatch")
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


@dataclass(frozen=True)
class CustomToolInvocation:
    call_id: str
    name: str
    input: str
    type: str = "custom"


def _invocation_calls(message):
    if message.get("function_call") is not None:
        # Legacy result association uses the function name, not a wire ID.
        function = message["function_call"]
        return [dict(id=function["name"], type="function", function=function)]
    return message.get("tool_calls", [])


def prepare_tool_calls(completion, validators, *, custom_validators=None):
    require(hasattr(completion, "message") and hasattr(completion, "finish_reason"),
            "select an explicit choice before preparing tools")
    """Validate the entire batch without executing any tool or changing history.

    validators maps allowed names to pure checks: return None or raise.
    The application, not this helper, owns execution and any side effects.
    """
    require(completion.finish_reason in ("tool_calls", "function_call"), "not a completed tool turn")
    calls = _invocation_calls(_message(completion.message))
    require(calls, "missing tool calls")
    planned = []
    for call in calls:
        if call["type"] == "custom":
            name, value = call["custom"]["name"], call["custom"]["input"]
            registry = custom_validators or {}
            require(name in registry and callable(registry[name]),
                    "unregistered custom tool: " + name)
            require(registry[name](value) is None, "custom validator must return None or raise")
            planned.append(CustomToolInvocation(call["id"], name, value))
            continue
        name = call["function"]["name"]
        require(name in validators and callable(validators[name]),
                "unregistered tool: " + name)
        args = _arguments(call["function"]["arguments"])
        verdict = validators[name](deepcopy(args))
        require(verdict is None, "tool validator must return None or raise")
        planned.append(ToolInvocation(call["id"], name, args))
    return planned


def append_tool_results(history, completion, results):
    require(completion.finish_reason in ("tool_calls", "function_call"), "not a completed tool turn")
    message = _message(completion.message)
    calls = _invocation_calls(message)
    require(calls and isinstance(results, dict) and
            set(results) == {c["id"] for c in calls},
            "provide exactly one result for every tool call id")
    following = [message]
    for call in calls:
        content = _text(results[call["id"]], "tool results must be text")
        following.append(dict(role="function", name=call["function"]["name"], content=content)
                         if message.get("function_call") is not None else
                         dict(role="tool", tool_call_id=call["id"], content=content))
    return deepcopy(history) + following


def append_final(history, completion):
    require(completion.finish_reason == "stop" and
            not completion.message.get("tool_calls") and
            not completion.message.get("function_call"),
            "length or tool turn is not a final answer")
    return deepcopy(history) + [_message(completion.message)]
