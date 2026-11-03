"""Publish reviewed request shapes; production semantics remain authoritative."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRAFT = "https://json-schema.org/draft/2020-12/schema"


def obj(properties, required=()):
    return dict(type="object", properties=properties, required=list(required),
                additionalProperties=False)


def ref(name):
    return {"$ref": "#/$defs/" + name}


def nullable(value):
    return dict(anyOf=[value, dict(type="null")])


def present(key, value=None):
    return dict(required=[key], properties={key: {} if value is None else value})


def any_present(keys):
    return dict(anyOf=[present(key) for key in keys])


def forbid(keys):
    return {"not": any_present(keys)}


def when(condition, consequence):
    return {"if": condition, "then": consequence}


def text_name(limit=256):
    return dict(type="string", minLength=1, maxLength=limit,
                pattern=r"^[^\u0000-\u001f\u007f<>]+$",
                **{"x-q4t-max-utf8-bytes": limit})


def schema_node():
    types = ["object", "array", "string", "integer", "number", "boolean", "null"]
    node = obj(dict(type=dict(anyOf=[dict(enum=types), dict(type="array",
        minItems=1, maxItems=2, uniqueItems=True, items=dict(enum=types),
        allOf=[when(dict(minItems=2), dict(contains=dict(const="null")))])]),
        title=dict(type="string", **{"x-q4t-max-utf8-bytes": 2048}),
        description=dict(type="string", **{"x-q4t-max-utf8-bytes": 2048}),
        properties=dict(type="object", maxProperties=32,
            propertyNames=dict(**{"x-q4t-max-utf8-bytes": 128}),
            additionalProperties=ref("schemaNode")),
        required=dict(type="array", uniqueItems=True, items=dict(type="string")),
        additionalProperties=dict(const=False), items=ref("schemaNode"),
        minItems=dict(type="integer", minimum=0, maximum=64),
        maxItems=dict(type="integer", minimum=0, maximum=64),
        enum=dict(type="array", minItems=1, maxItems=32, uniqueItems=True,
            items=dict(type=["string", "boolean", "null"],
                       **{"x-q4t-max-utf8-bytes": 256}))), ("type",))
    def typed(kind):
        return present("type", dict(anyOf=[dict(const=kind), dict(type="array",
            contains=dict(const=kind))]))
    node["allOf"] = [
        when(typed("object"), dict(required=["properties", "additionalProperties"])),
        when({"not": typed("object")}, forbid(["properties", "required", "additionalProperties"])),
        when(typed("array"), dict(required=["items"])),
        when({"not": typed("array")}, forbid(["items", "minItems", "maxItems"]))]
    return node


def chat_schema():
    safe = dict(type="string", minLength=1, maxLength=64, pattern="^[A-Za-z0-9_-]+$")
    hex64 = dict(type="string", pattern="^[0-9a-f]{64}$")
    identifier = dict(type="string", minLength=1, maxLength=128,
        pattern="^[A-Za-z0-9_-]+$", **{"not": dict(pattern="^chatcmpl-auto-")})
    text_part = obj(dict(type=dict(const="text"), text=dict(type="string")), ("text",))
    content = dict(anyOf=[dict(type=["string", "null"]),
                          dict(type="array", items=ref("textPart"))])
    arguments = dict(anyOf=[dict(type="object", propertyNames=text_name()),
        dict(type="string", **{"x-q4t-json-object-text": True})])
    call = dict(oneOf=[obj(dict(id=dict(type="string", minLength=1, maxLength=256,
        **{"x-q4t-max-utf8-bytes": 256}), type=dict(const="function"),
        function=obj(dict(name=text_name(), arguments=arguments), ("name", "arguments"))),
        ("function",)), obj(dict(type=dict(const="function"), name=text_name(),
                                arguments=arguments), ("name", "arguments"))])
    message = obj(dict(role=dict(enum=["system", "developer", "user", "assistant", "tool"]),
        content=content, name=dict(type="null"), refusal=dict(type="null"),
        audio=dict(type="null"), annotations=dict(type="null"), function_call=dict(type="null"),
        reasoning=dict(type=["string", "null"]), reasoning_content=dict(type=["string", "null"]),
        tool_calls=nullable(dict(type="array", maxItems=128, items=ref("historyCall"))),
        tool_call_id=dict(type="string", minLength=1, maxLength=256,
                          **{"x-q4t-max-utf8-bytes": 256})), ("role",))
    message["allOf"] = [
        when(present("role", {"not": dict(const="assistant")}),
             forbid(["reasoning", "reasoning_content", "tool_calls"])),
        when(present("role", {"not": dict(const="tool")}), forbid(["tool_call_id"])),
        when(present("role", dict(const="tool")), dict(required=["content"],
            properties=dict(content=dict(type=["string", "array"]))))]
    fn = obj(dict(name=text_name(), description=dict(type="string"),
        strict=dict(type=["boolean", "null"]), parameters=dict(type="object",
            properties=dict(properties=dict(type="object")))), ("name",))
    tool = obj(dict(type=dict(const="function"), function=fn), ("type", "function"))
    node = schema_node()
    root_schema = dict(allOf=[ref("schemaNode"), dict(properties=dict(type=dict(const="object")))])
    response_format = nullable(dict(oneOf=[obj(dict(type=dict(enum=["text", "json_object"])), ("type",)),
        obj(dict(type=dict(const="json_schema"), json_schema=obj(dict(name=safe,
            description=dict(type="string"), strict=dict(type=["boolean", "null"]),
            schema=root_schema), ("name", "schema"))), ("type", "json_schema"))]))
    template = obj(dict(enable_thinking=dict(type="boolean", default=True),
        preserve_thinking=dict(type="boolean", default=True),
        reasoning_effort=dict(enum=["low", "medium", "xhigh"], default="xhigh"),
        add_vision_id=dict(type="boolean", default=False)))
    nonempty_tools = dict(type="array", minItems=1)
    parsed = dict(required=["messages"], properties=dict(parse_response=dict(const=True)))
    json_output = present("response_format", present("type", dict(enum=["json_object", "json_schema"])))
    forced = present("tool_choice", dict(anyOf=[dict(const="required"), dict(type="object")]))
    strict_tools = present("tools", dict(type="array", contains=present("function", present("strict", dict(const=True)))))
    constrained = dict(anyOf=[json_output, forced, strict_tools,
        present("tool_choice", dict(const="none")), dict(allOf=[present("parallel_tool_calls", dict(const=False)),
            present("tools", nonempty_tools)])])
    stops = nullable(dict(anyOf=[dict(type="string", minLength=1, maxLength=256,
        **{"x-q4t-max-utf8-bytes": 256}), dict(type="array", maxItems=4,
            items=dict(type="string", minLength=1, maxLength=256,
                       **{"x-q4t-max-utf8-bytes": 256}))]))
    properties = dict(
        model=dict(type="string", **{"x-q4t-equals-served-model": True}),
        prompt=dict(type="string", minLength=1),
        messages=dict(type="array", minItems=1, prefixItems=[ref("message")],
            items=dict(allOf=[ref("message"), dict(properties=dict(role=dict(enum=["user", "assistant", "tool"])))]),
            contains=present("role", dict(const="user"))),
        max_tokens=nullable(dict(type="integer", minimum=1, maximum=2147483647)),
        max_completion_tokens=nullable(dict(type="integer", minimum=1, maximum=2147483647)),
        stream=dict(type=["boolean", "null"], default=False),
        stream_options=nullable(obj(dict(include_usage=dict(type="boolean", default=False)))),
        request_id=identifier, cancel_token=hex64,
        request_timeout_ms=dict(type="integer", minimum=1, maximum=10800000),
        tools=nullable(dict(type="array", items=ref("tool"))),
        chat_template_kwargs=template, **template["properties"],
        reasoning_field=dict(enum=["reasoning_content", "reasoning"], default="reasoning_content"),
        include_reasoning=dict(type="boolean", default=True),
        thinking_token_budget=nullable(dict(type="integer", minimum=0, maximum=2147483647)),
        temperature=dict(enum=[0, None]), top_p=dict(enum=[1, None]), n=dict(enum=[1, None]),
        frequency_penalty=dict(enum=[0, None]), presence_penalty=dict(enum=[0, None]),
        repetition_penalty=dict(const=1), seed=nullable(dict(type="integer",
            minimum=-9007199254740991, maximum=9007199254740991)), stop=stops,
        logprobs=dict(enum=[False, None]), top_logprobs=dict(enum=[0, None]),
        logit_bias=nullable(dict(type="object", maxProperties=0)),
        response_format=response_format, user=dict(type=["string", "null"]),
        session_id=hex64, prefix_cache_scope=hex64,
        prefix_cache_write_tokens=dict(anyOf=[dict(const=0), dict(type="integer", minimum=1024, maximum=131072)]),
        parse_response=dict(type="boolean"),
        tool_choice=dict(oneOf=[dict(enum=["auto", "none", "required"]),
            obj(dict(type=dict(const="function"), function=obj(dict(name=safe), ("name",))), ("type", "function"))]),
        parallel_tool_calls=dict(type="boolean", default=True))
    result = obj(properties)
    result.update({"$schema": DRAFT, "$id": "urn:q4t:serve-chat-request:v1",
        "title": "Q4T complete text Chat request structure v1",
        "description": "All supported text request fields. Required semantic and runtime checks are enumerated by x-q4t; schema validation alone is not admission or execution acceptance.",
        "$defs": dict(textPart=text_part, message=message, historyCall=call,
                       tool=tool, schemaNode=node),
        "oneOf": [present("messages"), present("prompt")],
        "dependentRequired": dict(request_id=["cancel_token"], cancel_token=["request_id"]),
        "allOf": [
            {"not": dict(required=["max_tokens", "max_completion_tokens"],
                properties={key: {"not": dict(type="null")} for key in ("max_tokens", "max_completion_tokens")})},
            when(present("prompt"), forbid(["tools", "chat_template_kwargs", *template["properties"]])),
            when(present("parse_response", dict(const=True)), dict(required=["messages"])),
            when(any_present(["reasoning_field", "include_reasoning", "tool_choice", "parallel_tool_calls"]), parsed),
            when(present("thinking_token_budget", {"not": dict(type="null")}), parsed),
            when(present("prefix_cache_write_tokens", dict(type="integer", minimum=1)), dict(required=["prefix_cache_scope"])),
            when(forced, present("tools", nonempty_tools)),
            when(constrained, dict(allOf=[parsed, forbid(["session_id", "prefix_cache_scope"]),
                dict(properties=dict(stop=dict(enum=[None, []])))])),
            when(dict(allOf=[json_output, present("tools", nonempty_tools)]), present("tool_choice", dict(const="none"))),
            when(any_present(["session_id", "prefix_cache_scope"]),
                 dict(properties=dict(stop=dict(enum=[None, []]))))],
        "x-q4t": dict(profile="qwen3.8-native-v2", scope="text; experimental media excluded",
            semantic_contract="serve-input-semantics-v1.json", required_semantic_validation=True,
            runtime_preflight="POST /v1/chat/preflight", defaults_are_annotations=True,
            raw_prompt_always_unparsed=True, parsed_messages_default=True,
            template_option_precedence="chat_template_kwargs overrides top level after both are validated",
            model_omitted="effective served model", output_omitted_or_null="effective configured output default",
            deadline_omitted="effective configured request deadline", finite_numbers_only=True,
            duplicate_keys="reject at every JSON object, including historical argument text")})
    return result


def publish(path, value, check):
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if check:
        assert path.read_text() == text, "generated contract drift: " + str(path)
    else:
        path.write_text(text)


def semantics():
    # Standard JSON Schema validators ignore x-q4t annotations. These rules
    # are mandatory and evaluated by production C++, never inferred as passed.
    rules = [
        ("json-lexical", ["src/io/json.cpp", "include/q4t/server/request_json.h"],
         "UTF-8 JSON; no duplicate keys, nonfinite numbers or trailing data; depth64/65536 values/16MiB decoded string bytes. Integer fields use exact source lexemes, not rounded binary64 values."),
        ("transport-body", ["src/server/http_request.cpp", "src/server/chat_http.cpp",
             "include/q4t/server/http_observation.h", "include/q4t/server/http_connection.h"],
         "16MiB Chat/preflight body, 48MiB aggregate reservation; capacity failure is HTTP503/body_capacity_exhausted/param=body before body read. This is not a RAM measurement. Exact GET metrics/config/capabilities/models share four observation connections, independent of four health/cancel controls and business capacity; observation overflow is HTTP503/observation_capacity_exhausted. Unclassified headers share the summed handler limit, not flood isolation. Cancel body4096. POST application/json UTF-8, no query strings, GET no body, methods exact; framing and header limits apply before parsing. Fixed first receive-failure counters exclude business validation."),
        ("model-and-credentials", ["src/server/input_contract.cpp", "src/server/chat_contract.cpp"],
         "If present model equals served identifier. request_id and fresh lowercase64hex cancel_token together; reserved auto prefix forbidden. Active ID registry/cancel authority are runtime checks, not idempotency."),
        ("utf8-byte-length", ["src/server/input_contract.cpp", "src/server/text_stop.cpp"],
         "x-q4t-max-utf8-bytes counts encoded bytes, not codepoints/UTF-16. Historical names/parameter keys forbid C0, DEL and angle brackets; stop valid UTF-8 1..256 bytes, up to4."),
        ("real-user-query", ["src/server/chat_template.cpp"],
         "At least one user message remains after native Unicode trim and exclusion of whole <tool_response>...</tool_response> user wrappers. Instruction slot only first system OR developer."),
        ("reasoning-aliases", ["src/server/reasoning_options.cpp"],
         "Only assistant may carry reasoning aliases; two string values must agree byte-for-byte. Null alias may defer to the other. Hidden output preserves computation/usage."),
        ("historical-arguments", ["src/server/chat_tool_history.cpp"],
         "Arguments are object or JSON text of an object using production parser budgets. Every top-level argument name is valid. No silent parameter dropping."),
        ("historical-batches", ["src/server/chat_tool_history.cpp"],
         "Any ID in a batch requires all nested calls to have valid unique IDs and type=function. Exactly one contiguous result for every ID; no orphan/duplicate/missing/mixed IDs. Text results may reorder. Unlabelled native legacy has distinct preserved rules; IDs are unique per batch, not globally."),
        ("tool-definitions", ["src/server/input_contract.cpp", "src/server/output_constraints.cpp"],
         "Function names unique. Auto/raw parameter schemas advisory, but parameters/properties must be objects. A named choice must exist when compiling tool arguments; none mode removes tools and intentionally does not compile their argument schemas."),
        ("constraint-combinations", ["src/server/output_constraints.cpp", "src/server/text_stop.cpp"],
         "JSON output/none/required/named/strict tools/nonparallel nonempty tools activate constraints. Parsed text required, state caches and effective stop forbidden; JSON plus tools requires none. Compiled tool branches max16 tools and ASCII safe names. Compiled structural tag <=65536 bytes."),
        ("finite-schema", ["src/server/output_schema.cpp"],
         "Root exact object; serialized schema<=16384 bytes; depth<=8/<=128 nodes; object<=32 properties/additionalProperties=false; required unique declared keys, all required in strict mode. Enum1..32 distinct matching string/bool/null; no numeric enums. minItems<=maxItems; bounds0..64. XML top properties ASCII1..64, no reserved XML delimiters in names/enums, no nullable top-level XML string parameters."),
        ("response-parser-prefix", ["src/server/chat_response_parser.cpp", "src/server/thinking_budget.cpp"],
         "Production parser and thinking budget initialization must accept native generation prefix. Real thinking closure token preparation remains execution-time validation."),
        ("configured-capabilities", ["src/server/chat_plan.cpp"],
         "Structured backend must actually be built; state caches require their effective enabled configuration. Probe flags describe hypothetical config; they do not prove a running backend or cache."),
        ("token-and-slot-budget", ["src/server/chat_plan.cpp", "include/q4t/server/prefix_cache_policy.h"],
         "Production tokenizer and max_len determine input/output allowance. Uniform clips output, explicit capacity fits full capped budget; prefix seed must match configured checkpoint and fit prompt. Optional preflight preview has conservative encoded8MiB bound."),
        ("startup-memory-admission", ["include/q4t/server/startup_admission.h",
             "src/server/server_options.cpp", "src/server/chat_server.cpp",
             "src/server/chat_plan.cpp"],
         "Fresh global MemAvailable checks before metadata, before either model allocation path, and before readiness. Default2048MiB headroom; preallocation also requires the existing allocation peak estimate. Missing/inconsistent observation, missing estimate or overflow stops startup. Zero or --no-budget explicitly bypasses this guard; request memory admission remains independent. Effective configuration exposes all three snapshots. No reservation, silent capacity retry or physical RAM hard-limit guarantee."),
        ("device-memory-failure", ["include/q4t/server/device_memory.h",
             "src/server/cuda_memory_backend.h", "src/server/chat_device_memory.cpp",
             "src/server/chat_server.cpp", "src/server/chat_generation.cpp"],
         "Serve-owned text and scheduler buffers distinguish proven capacity exhaustion from other device failures. Only a known OOM indicator may be consumed: optional buffers fall back, required request buffers return device_memory_exhausted (JSON503 or SSE error). Other device or any release failure closes readiness and stops serve with nonzero exit. Release ownership is consumed once and checked before successful terminal/cache publication. Untyped MTP preparation failure drains and stops. Media and model-internal allocations/releases are not fully covered. This is not physical RAM reservation or a hard limit."),
        ("backend-cleanup", ["include/q4t/runtime/cleanup_policy.h",
             "src/runtime/startup_reservation.cpp", "cmake/CudaCleanupBoundary.cmake",
             "src/model/model.cu", "src/model/model_owner.cpp", "src/mtp/mtp.cu",
             "src/server/chat_server.cpp"],
         "Production q4t links checked direct CUDA free/freeAsync/freeHost/eventDestroy/streamDestroy/streamSynchronize/deviceSynchronize/eventSynchronize calls. Unknown cleanup/completion fails closed with immediate exit1, no destructor retry and no guaranteed HTTP error. Only the explicit serving cudaFree owner may receive errors to stop/drain and publish its existing error protocol. External shared-library internals and other executables are outside this link boundary. Successful async free/destroy does not prove completion or physical reclaim. Requested MTP load failure stops either startup path; --no-mtp remains explicit opt-out. Model load completion is checked before source release; failed MTP load drains before loader destruction. No model arithmetic change or complete media allocation guarantee."),
        ("experimental-media-admission", ["include/q4t/vision/media_plan.h",
             "src/vision/media_plan.cpp", "include/q4t/vision/processor.h",
             "src/vision/processor.cpp", "src/vision/vision.cu",
             "include/q4t/server/media_admission.h", "src/server/media_admission.cpp",
             "src/server/chat_contract.cpp", "src/server/chat_prompt.cpp",
             "src/server/chat_input.cpp", "src/server/chat_generation.cpp"],
         "Explicit experimental media: user only, closed image_url/video_frames parts, auto detail only, canonical standard padded base64 or exact PNG/JPEG data URLs with matching headers. All item/frame source and geometry limits precede decoding. Shared host plan expands tokens before slot/output budgeting; processor reuses the geometry and verifies decoded size. Decoder owned C allocations including prefixes have a 256MiB quota, nonthrowing callbacks and cleanup before quota400 or actual bad_alloc503. Cancellation checks item/frame boundaries and before GPU, not preemption of a C decode/kernel. Attention limit is queried from current device default minus static shared and function dynamic maximum; unknown fails requested media startup. Policy values are exposed in configuration/discovery. These logical limits do not bound allocator internal realloc peaks or reserve whole-server physical RAM. GPU/HTTP acceptance is separate."),
        ("runtime-memory-admission", ["src/runtime/system_memory.cpp",
             "src/server/memory_admission.cpp", "src/server/chat_memory.cpp",
             "src/server/chat_http.cpp", "src/server/chat_admission.cpp"],
         "Configurable Linux-wide MemAvailable gate, independent of startup allocation estimates; default2048MiB floor, 512MiB recovery margin, 250ms cached observation with fresh preparation and slot handoff. Missing/invalid samples reject new chat/preflight work, including queued requests; readiness closes, liveness/control remain available and active generations continue. No byte reservation, CUDA allocation or whole-server/cgroup hard-limit guarantee."),
        ("runtime-admission", ["src/server/chat_generation.cpp", "src/server/chat_preflight.cpp",
             "src/server/chat_admission.cpp", "include/q4t/server/request_admission.h",
             "include/q4t/server/sequence_admission.h", "src/server/server_options.cpp"],
         "Availability/registry/cache lease/GPU health/deadline/cancellation/grammar compilation are not established by schema or offline probe. Chat/preflight share a configured inflight bound through output cleanup; only generation enters the separately bounded capacity-FIFO slot queue after preparation. Queue fullness can reject independently of whole-server RAM and connection/body limits. Preflight does not reserve slots or guarantee execution or answer quality.")]
    paths = sorted({p for _, sources, _ in rules for p in sources})
    return dict(profile="q4t-input-semantics-v1", required=True,
        structural_schema="serve-chat-request-v1.schema.json",
        offline_probe="tools/serve_protocol/contract_probe.cpp",
        probe_excludes=["HTTP framing", "tokenization", "token/slot admission",
            "grammar compilation", "runtime availability", "model execution"],
        runtime_preflight="POST /v1/chat/preflight",
        rules=[dict(id=name, sources=sources, contract=description)
               for name, sources, description in rules],
        sources_sha256={p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths})


def openapi():
    external = lambda file, fragment="": {"$ref": file + fragment}
    response = lambda schema, description="JSON response": dict(description=description,
        headers={"X-Request-Id": dict(schema=dict(type="string",
            pattern="^[A-Za-z0-9_.:-]{1,128}$"), description="Trace only; not cancellation credentials or an idempotency key")},
        content={"application/json": dict(schema=schema)})
    error = external("serve-transport-v1.schema.json", "#/$defs/error")
    chat = external("serve-chat-request-v1.schema.json")
    cancel = obj(dict(request_id=chat_schema()["properties"]["request_id"],
        cancel_token=dict(type="string", pattern="^[0-9a-f]{64}$")), ("request_id", "cancel_token"))
    preflight = obj(dict(request=chat, include_prompt=dict(type="boolean", default=False),
                         include_token_ids=dict(type="boolean", default=False)), ("request",))
    paths = {}
    models = obj(dict(object=dict(const="list"), data=dict(type="array", minItems=1, maxItems=1,
        items=obj(dict(id=dict(type="string"), object=dict(const="model"), created=dict(type="integer"),
                       owned_by=dict(const="q4t")), ("id", "object", "created", "owned_by")))), ("object", "data"))
    health = obj(dict(status=dict(enum=["ok", "unhealthy"]), gpu_healthy=dict(type="boolean"),
        requests_running=dict(type="integer", minimum=0), seq_slots_free=dict(type="integer", minimum=0),
        seq_slots_total=dict(type="integer", minimum=1)),
        ("status", "gpu_healthy", "requests_running", "seq_slots_free", "seq_slots_total"))
    endpoints = [
        ("/v1/chat/completions", "post", chat, external("serve-transport-v1.schema.json", "#/$defs/completion")),
        ("/v1/chat/preflight", "post", preflight, external("serve-preflight-v1.schema.json")),
        ("/v1/requests/cancel", "post", cancel,
         obj(dict(cancel_requested=dict(const=True)), ("cancel_requested",))),
        ("/v1/models", "get", None, models),
        ("/v1/capabilities", "get", None, dict(type="object", description="Additive implementation/profile discovery; validate profile and protocol_source_sha256. Not acceptance or model quality evidence.")),
        ("/v1/config", "get", None, external("serve-config-v1.schema.json")),
        ("/healthz", "get", None, health),
        ("/livez", "get", None, external("serve-transport-v1.schema.json", "#/$defs/readiness")),
        ("/readyz", "get", None, external("serve-transport-v1.schema.json", "#/$defs/readiness")),
        ("/metrics", "get", None, None)]
    for path, method, body, schema in endpoints:
        status = "202" if path.endswith("/cancel") else "200"
        success = response(schema) if schema else dict(description="Prometheus metrics",
            content={"text/plain; version=0.0.4": dict(schema=dict(type="string"))})
        operation = dict(operationId=path.strip("/").replace("/", "_"),
            parameters=[dict(name="X-Request-Id", **{"in": "header"}, required=False,
                             schema=dict(type="string", pattern="^[A-Za-z0-9_.:-]{1,128}$"))],
            responses={status: success, "default": response(error, "Error before stream starts; exact code/param come from production validation")})
        if body:
            operation["requestBody"] = dict(required=True, content={"application/json": dict(schema=body)})
        if path.endswith("/completions"):
            success["content"]["text/event-stream"] = dict(schema=dict(type="string"),
                **{"x-q4t-event-schema": external("serve-transport-v1.schema.json", "#/$defs/chunk"),
                   "x-q4t-stream-contract": "role/content/tool deltas; exactly one finish; optional usage; DONE then EOF. After HTTP200, error envelope plus DONE when writable, otherwise truncated. No tool dispatch before complete validated terminal."})
        if path in ("/healthz", "/livez", "/readyz"):
            operation["responses"]["503"] = response(schema, "Unhealthy, not ready, or draining")
        paths[path] = {method: operation}
    return dict(openapi="3.1.0", info=dict(title="Q4T text serving contract",
        version="q4t-serve-v1", description="Static supported surface, not full OpenAI API compatibility. See required native semantics and version-bound acceptance matrix."),
        servers=[dict(url="http://127.0.0.1:8080", description="Example only; use effective configured address")],
        paths=paths, **{"x-q4t": dict(semantic_contract="serve-input-semantics-v1.json",
            query_parameters="unsupported;400", get_request_body="unsupported;400",
            wrong_method="405 with Allow", post_content_type="application/json; optional charset=utf-8",
            unsupported_media_type=415, builtin_authentication=False,
            deployment="controlled local/internal service; public deployment not accepted",
            streaming_response_schema_scope="envelopes; sequencing and tool semantics require client validation")})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    schema = chat_schema()
    inventory = json.loads((ROOT / "tools/serve_audit/inventory.json").read_text())
    assert set(schema["properties"]) == set(inventory["fields"]), "uncatalogued field"
    publish(ROOT / "docs/serve-chat-request-v1.schema.json", schema, args.check)
    publish(ROOT / "docs/serve-input-semantics-v1.json", semantics(), args.check)
    spec = openapi()
    assert {method.upper()+" "+path for path, operations in spec["paths"].items()
            for method in operations} == set(inventory["routes"])
    publish(ROOT / "docs/serve-openapi-v1.json", spec, args.check)


if __name__ == "__main__":
    main()
