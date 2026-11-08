"""Reviewed positive/negative input contracts, independent of schema execution."""
from copy import deepcopy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEX = "a" * 64
EMPTY = dict(type="object", properties={}, required=[], additionalProperties=False)


def base():
    return dict(messages=[dict(role="user", content="q")], enable_thinking=False)


def tool(name="read", *, strict=False, parameters=None):
    return dict(type="function", function=dict(name=name, strict=strict,
        parameters=deepcopy(EMPTY if parameters is None else parameters)))


def history():
    return dict(messages=[dict(role="user", content="q"), dict(role="assistant",
        content=None, tool_calls=[dict(id="a", type="function", function=dict(
            name="read", arguments="{}")), dict(id="b", type="function",
            function=dict(name="read", arguments={}))]),
        dict(role="tool", tool_call_id="b", content="second"),
        dict(role="tool", tool_call_id="a", content="first")], enable_thinking=False)


def cases():
    rows = []

    def add(name, request, expected=True, *, field=None, semantic=False, raw=None, **config):
        rows.append(dict(id=name, expected=expected,
            shape_expected=None if semantic else expected, field=field,
            fixture=dict(raw=json.dumps(request, ensure_ascii=False, separators=(",", ":"))
                         if raw is None else raw, **config)))

    values = dict(model="qwen3.8-flash-next", prompt="q", messages=base()["messages"],
        max_tokens=2, max_completion_tokens=2, stream=True,
        stream_options=dict(include_usage=True), request_id="client-1", cancel_token=HEX,
        request_timeout_ms=500, tools=[tool()], chat_template_kwargs=dict(enable_thinking=False),
        enable_thinking=False, preserve_thinking=True, reasoning_effort="low",
        add_vision_id=True, reasoning_field="reasoning", include_reasoning=False,
        thinking_token_budget=1, temperature=0, top_p=1, n=1, frequency_penalty=0,
        presence_penalty=0, repetition_penalty=1, seed=42, stop="END", logprobs=False,
        top_logprobs=0, logit_bias={}, response_format=dict(type="json_object"), user="audit",
        session_id=HEX, prefix_cache_scope=HEX, prefix_cache_write_tokens=1024,
        parse_response=False, tool_choice="auto", parallel_tool_calls=False)
    nulls = {"max_tokens", "max_completion_tokens", "stream", "stream_options", "tools",
        "thinking_token_budget", "temperature", "top_p", "n", "frequency_penalty",
        "presence_penalty", "seed", "stop", "logprobs", "top_logprobs", "logit_bias",
        "response_format", "user"}
    for field, valid in values.items():
        request, config = base(), {}
        if field == "top_logprobs":
            request["logprobs"] = True
        if field == "prompt":
            request = {}
        if field in ("request_id", "cancel_token"):
            request.update(request_id="client-1", cancel_token=HEX)
        if field in ("session_id", "prefix_cache_scope", "prefix_cache_write_tokens"):
            config = dict(session_cache=True, prefix_cache=True)
        if field == "prefix_cache_write_tokens":
            request["prefix_cache_scope"] = HEX
        for label, value, accepted in (("valid", valid, True), ("null", None, field in nulls),
                                        ("wrong-shape", {"unexpected": True}, False)):
            body = dict(request, **{field: value})
            add(field+"-"+label, body, accepted, field=field, **config)
        request.pop(field, None)
        add(field+"-omitted", request, field not in ("messages", "prompt", "request_id", "cancel_token"),
            field=field, **config)
    for key, candidates in {
        "temperature": [(-0.1, False), (0.5, True), (2, True), (2.1, False)],
        "top_p": [(0, True), (0.5, True), (1, True), (1.1, False)],
        "n": [(2, True), (128, True), (129, False), (1.1, False)],
        "presence_penalty": [(-2, True), (2, True), (2.1, False)],
        "frequency_penalty": [(-2, True), (2, True), (-2.1, False)],
    }.items():
        for index, (value, accepted) in enumerate(candidates):
            add(f"sampling-{key}-{index}", dict(base(), **{key: value}), accepted)
    add("sampling-logprobs-top20", dict(base(), logprobs=True, top_logprobs=20))
    add("sampling-top-without-logprobs", dict(base(), top_logprobs=0), False)
    add("sampling-top21", dict(base(), logprobs=True, top_logprobs=21), False)
    add("sampling-bias", dict(base(), logit_bias={"22": 100, "0": -100}))
    add("sampling-bias-alias", dict(base(), logit_bias={"1": 1, "01": 2}), False, semantic=True)
    add("sampling-bias-oov", dict(base(), logit_bias={"100": 0}), False, semantic=True, token_vocab=100)
    add("sampling-seed-int64-exponent", None, True,
        raw=json.dumps(base())[:-1]+',"seed":9.223372036854775807e18}')
    add("sampling-underflow", None, False, semantic=True,
        raw=json.dumps(base())[:-1]+',"temperature":1e-400}')
    add("root-array", [], False)
    add("unknown-field", dict(base(), unknown=True), False)
    add("both-inputs", dict(base(), prompt="q"), False)
    add("raw-template-option", dict(prompt="q", enable_thinking=False), False)
    add("raw-null-thinking-budget", dict(prompt="q", thinking_token_budget=None))
    add("raw-active-thinking-budget", dict(prompt="q", thinking_token_budget=0), False)
    add("raw-unparsed", dict(prompt="q", parse_response=False))
    add("raw-parsed", dict(prompt="q", parse_response=True), False)
    add("limit-both-active", dict(base(), max_tokens=1, max_completion_tokens=2), False)
    for left, right in ((None, 2), (2, None), (None, None)):
        add(f"limit-null-alias-{left}-{right}", dict(base(), max_tokens=left, max_completion_tokens=right))
    for key, numbers in (("max_completion_tokens", (0, 1, 2147483647, 2147483648)),
                         ("thinking_token_budget", (-1, 0, 2147483647, 2147483648)),
                         ("request_timeout_ms", (0, 1, 10800000, 10800001)),
                         ("seed", (-9223372036854775809, -9223372036854775808, 9223372036854775807, 9223372036854775808))):
        for index, number in enumerate(numbers):
            add(f"{key}-edge-{index}", dict(base(), **{key: number}), index in (1, 2))
    for spelling, accepted in (("2.0", True), ("2e0", True), ("20e-1", True),
                               ("2.0000000000000000001", False), ("1.9999999999999999999", False)):
        raw = json.dumps(dict(base(), max_completion_tokens="NUMBER")).replace('"NUMBER"', spelling)
        add("exact-number-"+spelling, None, accepted, raw=raw)
    for raw in ('{"prompt":"q","prompt":"r"}', '{"prompt":"q","temperature":NaN}',
                '{"prompt":"q"} trailing', '{"prompt":"\\ud800"}',
                '{"messages": [', '{"prompt":"q","seed":1e500}'):
        add("invalid-json-"+str(len(rows)), None, False, raw=raw)
    for value, accepted in (("中"*85, True), ("中"*86, False), ("", False),
                            ([], True), (["a", "b", "c", "d"], True), (["a"]*5, False)):
        add("stop-bytes-"+str(len(rows)), dict(base(), stop=value), accepted)
    add("stop-cache", dict(base(), stop="end", session_id=HEX), False, session_cache=True)
    add("stop-inactive-cache-seed", dict(base(), stop="end", prefix_cache_write_tokens=0))
    for field in ("session_id", "prefix_cache_scope"):
        add(field+"-disabled", dict(base(), **{field: HEX}), False, semantic=True)
    add("different-served-model", dict(base(), model="other"), False, semantic=True)
    add("configured-served-alias", dict(base(), model="alias"), model="alias")
    add("id-reserved-prefix", dict(base(), request_id="chatcmpl-auto-test", cancel_token=HEX), False)
    add("template-overrides", dict(base(), enable_thinking=True,
                                  chat_template_kwargs=dict(enable_thinking=False)))
    add("null-nested-template", dict(base(), chat_template_kwargs=dict(enable_thinking=None)), False)
    for role in ("system", "developer"):
        add(role+"-first", dict(messages=[dict(role=role, content="rules"), *base()["messages"]]))
        add(role+"-late", dict(messages=[*base()["messages"], dict(role=role, content="rules")]))
    add("mixed-instructions", dict(messages=[dict(role="system", content="a"), dict(role="developer", content="b"), *base()["messages"]]))
    add("no-user", dict(messages=[dict(role="assistant", content="hi")]))
    add("tool-wrapper-not-query", dict(messages=[dict(role="user", content=" <tool_response>x</tool_response> ")]))
    for value, accepted in ((None, True), ("", True), ("name", True), (17, False)):
        add("message-name-"+str(value), dict(messages=[dict(role="user", content="q", name=value)]), accepted)
    for label, value, accepted in (("4096", "a"*4096, True), ("4097", "a"*4097, False),
            ("unicode", "名"*1365+"a", True), ("unicode-overflow", "名"*1366, False)):
        add("message-name-"+label, dict(messages=[dict(role="user", content="q", name=value)]), accepted)
    for label, message, accepted in (
        ("text", dict(role="assistant", refusal="no"), True),
        ("null-content", dict(role="assistant", content=None, refusal="no"), True),
        ("part", dict(role="assistant", content=[dict(type="refusal", refusal="no")]), True),
        ("missing", dict(role="assistant", refusal=None), False),
        ("wrong-role", dict(role="user", content="q", refusal=None), False),
        ("wrong-part-role", dict(role="user", content=[dict(type="refusal", refusal="no")]), False),
        ("mixed-parts", dict(role="assistant", content=[dict(type="refusal", refusal="no"), dict(type="text", text="q")]), False),
        ("bad-part-type", dict(role="assistant", content=[dict(type="refusal", refusal=7)]), False),
        ("unknown-part-field", dict(role="assistant", content=[dict(type="refusal", refusal="no", ignored=True)]), False)):
        add("message-refusal-"+label, dict(messages=[message]), accepted)
    for role in ("system", "developer", "user", "assistant", "tool"):
        for label, content in (("null", None), ("empty-parts", []), ("number", 4)):
            add("message-content-"+role+"-"+label, dict(messages=[dict(role=role, content=content)]), False)
        add("message-content-"+role+"-missing", dict(messages=[dict(role=role)]), False)
    add("text-parts", dict(messages=[dict(role="user", content=[dict(text="q"), dict(type="text", text="more")])]))
    add("media-disabled", dict(messages=[dict(role="user", content=[dict(type="image_url", image_url=dict(url="AA=="))])]), False, semantic=True)
    for aliases, accepted in ((dict(reasoning="r", reasoning_content="r"), True),
                              (dict(reasoning=None, reasoning_content="r"), True),
                              (dict(reasoning="r", reasoning_content="different"), False)):
        add("reason-alias-"+str(len(rows)), dict(messages=[dict(role="assistant", content="a", **aliases), *base()["messages"]]), accepted, semantic=not accepted)
    add("reason-on-user", dict(messages=[dict(role="user", content="q", reasoning=None)]), False)
    add("history-reordered", history())
    changed = history(); changed["messages"].pop()
    add("history-missing-result", changed, False, semantic=True)
    changed = history(); changed["messages"][2]["tool_call_id"] = "a"
    add("history-duplicate-result", changed, False, semantic=True)
    changed = history(); changed["messages"][2]["tool_call_id"] = "foreign"
    add("history-foreign-result", changed, False, semantic=True)
    changed = history(); changed["messages"][1]["tool_calls"][1]["id"] = "a"
    add("history-duplicate-call", changed, False, semantic=True)
    changed = history(); del changed["messages"][1]["tool_calls"][1]["id"]
    add("history-mixed-ids", changed, False, semantic=True)
    changed = history(); del changed["messages"][1]["tool_calls"][1]["type"]
    add("history-id-missing-type", changed, False, semantic=True)
    changed = history(); changed["messages"][2]["content"] = None
    add("history-null-result", changed, False)
    changed = history(); changed["messages"] += deepcopy(changed["messages"][1:])
    add("history-id-reused-next-batch", changed)
    for arguments, accepted in (("{}", True), ({}, True), ("[]", False),
        ('{"a":1,"a":2}', False), ('{"bad<name":1}', False), (None, False)):
        changed = history(); changed["messages"][1]["tool_calls"][0]["function"]["arguments"] = arguments
        add("history-args-"+str(len(rows)), changed, accepted, semantic=not accepted)
    add("legacy-flat-history", dict(messages=[*base()["messages"], dict(role="assistant",
        tool_calls=[dict(name="read", arguments={})]), dict(role="tool", content="result")]))
    add("orphan-tool-id", dict(messages=[*base()["messages"], dict(role="tool", content="x", tool_call_id="a")]), False, semantic=True)
    add("tools-duplicate-names", dict(base(), tools=[tool(), tool()]), False, semantic=True)
    add("tools-unicode-name", dict(base(), tools=[tool("函"*85)]))
    add("tools-name-byte-overflow", dict(base(), tools=[tool("函"*86)]), False)
    advisory = dict(type="object", properties={}, unsupported="advisory")
    add("advisory-schema", dict(base(), tools=[tool(parameters=advisory)]))
    add("none-ignores-argument-schema", dict(base(), tools=[tool(strict=True, parameters=advisory)], tool_choice="none"))
    add("strict-compiles-schema", dict(base(), tools=[tool(strict=True, parameters=advisory)]), False, semantic=True)
    for choice in ("none", "required", dict(type="function", function=dict(name="read"))):
        add("tool-choice-"+str(len(rows)), dict(base(), tools=[tool()], tool_choice=choice))
    add("named-not-declared", dict(base(), tools=[tool()], tool_choice=dict(type="function", function=dict(name="other"))), False, semantic=True)
    add("required-no-tools", dict(base(), tool_choice="required"), False)
    add("strict-unparsed", dict(base(), tools=[tool(strict=True)], parse_response=False), False)
    add("none-backend-missing", dict(base(), tool_choice="none"), False, semantic=True, structured=False)
    add("none-stop", dict(base(), tool_choice="none", stop="END"), False)
    add("none-empty-stop", dict(base(), tool_choice="none", stop=[]))
    add("json-with-tools-auto", dict(base(), tools=[tool()], response_format=dict(type="json_object")), False)
    add("json-with-tools-none", dict(base(), tools=[tool()], response_format=dict(type="json_object"), tool_choice="none"))
    for schema, accepted in ((EMPTY, True), (dict(EMPTY, maximum=1), False),
        (dict(EMPTY, properties=dict(a=dict(type="string"))), True),
        (dict(EMPTY, properties=dict(a=dict(type="integer", enum=[1]))), False),
        (dict(type="object", properties={}, additionalProperties=True), False)):
        add("output-schema-"+str(len(rows)), dict(base(), response_format=dict(type="json_schema",
            json_schema=dict(name="answer", schema=schema))), accepted)
    schema = dict(EMPTY, properties=dict(a=dict(type="string")))
    add("strict-all-required", dict(base(), response_format=dict(type="json_schema",
        json_schema=dict(name="answer", strict=True, schema=schema))), False, semantic=True)
    xml = dict(EMPTY, properties=dict(a=dict(type=["string", "null"])), required=["a"])
    add("json-nullable-string", dict(base(), response_format=dict(type="json_schema",
        json_schema=dict(name="answer", strict=True, schema=xml))))
    add("xml-nullable-string", dict(base(), tools=[tool(strict=True, parameters=xml)]), False, semantic=True)
    for request, accepted in ((dict(request_id="client", cancel_token=HEX), True),
        (dict(request_id="client"), False), (dict(request_id="client", cancel_token=HEX, unknown=True), False)):
        add("cancel-"+str(len(rows)), request, accepted, kind="cancel")
    for body, accepted in ((dict(request=base()), True), (dict(request=base(), include_prompt=True), True),
        (dict(request=base(), include_prompt=None), False), (dict(request=base(), unknown=True), False)):
        add("preflight-"+str(len(rows)), body, accepted, kind="preflight")
    # Accepted template fixtures are reused only as request-shape/semantics
    # inputs; no new tokenizer or model evidence is claimed.
    prior = json.loads((ROOT / "docs/evidence/serve-preflight-20261103/cpu/template-diff/cases.json").read_text())
    for row in prior:
        add("reused-shape-"+row["id"], row["request"], "reject_param" not in row)
    assert len(rows) <= 650
    return rows
