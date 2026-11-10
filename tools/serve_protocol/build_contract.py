"""Publish reviewed request shapes; production semantics remain authoritative."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
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
    typed = lambda kind: present("type", dict(anyOf=[dict(const=kind),
        dict(type="array", contains=dict(const=kind))]))
    bound = dict(type="integer", minimum=0, maximum=2147483647)
    node = obj(dict(type=dict(anyOf=[dict(enum=types), dict(type="array",
        minItems=1, maxItems=7, uniqueItems=True, items=dict(enum=types))]),
        title=dict(type="string"), description=dict(type="string"),
        properties=dict(type="object", maxProperties=5000,
            additionalProperties=ref("schemaNode")),
        required=dict(type="array", uniqueItems=True, items=dict(type="string")),
        additionalProperties=dict(const=False), items=ref("schemaNode"),
        minItems=bound, maxItems=bound, minLength=bound, maxLength=bound,
        minimum=dict(type="number"), maximum=dict(type="number"),
        exclusiveMinimum=dict(type="number"), exclusiveMaximum=dict(type="number"),
        multipleOf=dict(type="number", exclusiveMinimum=0),
        pattern=dict(type="string"), format=dict(enum=["date-time", "time", "date",
            "duration", "email", "hostname", "ipv4", "ipv6", "uuid"]),
        enum=dict(type="array", minItems=1, maxItems=1000, uniqueItems=True),
        const={}, default={}, examples=dict(type="array"),
        anyOf=dict(type="array", minItems=1, items=ref("schemaNode")),
        **{"$ref": dict(type="string", pattern="^#"),
           "$defs": dict(type="object", additionalProperties=ref("schemaNode")),
           "$comment": dict(type="string"), "$schema": dict(type="string")}))
    reference = any_present(["$ref", "anyOf"])
    node["allOf"] = [
        any_present(["type", "$ref", "anyOf", "enum", "const"]),
        when(dict(anyOf=[present("properties"),
            dict(allOf=[typed("object"), {"not": reference}])]),
            dict(required=["additionalProperties"])),
        when(dict(allOf=[typed("array"), {"not": reference}]),
            dict(required=["items"]))]
    for kind, fields in [("object", ["properties", "required", "additionalProperties"]),
                         ("array", ["items", "minItems", "maxItems"]),
                         ("string", ["minLength", "maxLength", "pattern", "format"])]:
        node["allOf"].append(when(dict(allOf=[present("type"), {"not": typed(kind)}]),
                                  forbid(fields)))
    node["allOf"].append(when(dict(allOf=[present("type"),
        {"not": dict(anyOf=[typed("number"), typed("integer")])}]),
        forbid(["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"])))
    return node


def chat_schema():
    safe = dict(type="string", minLength=1, maxLength=64, pattern="^[A-Za-z0-9_-]+$")
    hex64 = dict(type="string", pattern="^[0-9a-f]{64}$")
    identifier = dict(type="string", minLength=1, maxLength=128,
        pattern="^[A-Za-z0-9_-]+$", **{"not": dict(pattern="^(chatcmpl|cmpl)-auto-")})
    text_part = obj(dict(type=dict(const="text"), text=dict(type="string")), ("text",))
    refusal_part = obj(dict(type=dict(const="refusal"),
                            refusal=dict(type="string")), ("type", "refusal"))
    content = dict(anyOf=[dict(type=["string", "null"]),
        dict(type="array", minItems=1,
             items=dict(oneOf=[ref("textPart"), ref("refusalPart")]))])
    arguments = dict(anyOf=[dict(type="object"),
        dict(type="string", **{"x-q4t-json-object-text": True})])
    call = dict(oneOf=[obj(dict(id=dict(type="string", minLength=1, maxLength=256,
        **{"x-q4t-max-utf8-bytes": 256}), type=dict(const="function"),
        function=obj(dict(name=text_name(), arguments=arguments), ("name", "arguments"))),
        ("function",)), obj(dict(type=dict(const="function"), name=text_name(),
                                arguments=arguments), ("name", "arguments"))])
    call["oneOf"].append(obj(dict(id=dict(type="string", minLength=1, maxLength=256,
        **{"x-q4t-max-utf8-bytes": 256}), type=dict(const="custom"),
        custom=obj(dict(name=safe, input=dict(type="string")), ("name", "input"))),
        ("id", "type", "custom")))
    legacy_call = obj(dict(name=safe, arguments=dict(type="string",
        **{"x-q4t-json-object-text": True})), ("name", "arguments"))
    message = obj(dict(role=dict(enum=["system", "developer", "user", "assistant", "tool", "function"]),
        content=content, name=dict(type=["string", "null"],
            **{"x-q4t-max-utf8-bytes": 4096}),
        refusal=dict(type=["string", "null"]),
        audio=dict(type="null"), annotations=dict(type="null"), function_call=nullable(legacy_call),
        reasoning=dict(type=["string", "null"]), reasoning_content=dict(type=["string", "null"]),
        tool_calls=nullable(dict(type="array", maxItems=128, items=ref("historyCall"))),
        tool_call_id=dict(type="string", minLength=1, maxLength=256,
                          **{"x-q4t-max-utf8-bytes": 256})), ("role",))
    message["allOf"] = [
        when(present("role", {"not": dict(const="assistant")}),
             forbid(["reasoning", "reasoning_content", "tool_calls", "refusal", "function_call"])),
        when(present("role", {"not": dict(const="tool")}), forbid(["tool_call_id"])),
        when(dict(allOf=[{"not": present("role", dict(const="function"))},
          {"not": dict(allOf=[present("role", dict(const="assistant")),
            dict(anyOf=[present("tool_calls", dict(type="array", minItems=1)),
                        present("refusal", dict(type="string")),
                        present("function_call", dict(type="object"))])])}]),
            dict(required=["content"],
                 properties=dict(content=dict(type=["string", "array"])))),
        when(present("function_call", dict(type="object")),
            dict(properties=dict(tool_calls=dict(type="null")))),
        when(present("role", dict(const="function")),
            dict(required=["name", "content"], properties=dict(
                name=safe, content=dict(type=["string", "null"])),
                **forbid(["audio", "annotations", "tool_call_id"]))),
        when(present("role", {"not": dict(const="assistant")}),
            dict(properties=dict(content=dict(anyOf=[dict(type=["string", "null"]),
                dict(type="array", items={"not": present("type", dict(const="refusal"))})])))),
        when(present("content", dict(type="array", contains=ref("refusalPart"))),
            dict(properties=dict(content=dict(maxItems=1, items=ref("refusalPart")))))]
    fn = obj(dict(name=text_name(), description=dict(type="string"),
        strict=dict(type=["boolean", "null"]), parameters=dict(type="object",
            properties=dict(properties=dict(type="object")))), ("name",))
    tool = obj(dict(type=dict(const="function"), function=fn), ("type", "function"))
    custom_format = dict(oneOf=[obj(dict(type=dict(const="text")), ("type",)),
        obj(dict(type=dict(const="grammar"), grammar=obj(dict(
            syntax=dict(enum=["regex", "lark"]), definition=dict(type="string", minLength=1,
                **{"x-q4t-max-utf8-bytes": 16384})), ("syntax", "definition"))), ("type", "grammar"))])
    tool = dict(oneOf=[tool, obj(dict(type=dict(const="custom"), custom=obj(dict(
        name=safe, description=dict(type="string"), format=custom_format), ("name",))),
        ("type", "custom"))])
    tool_reference = dict(oneOf=[obj(dict(type=dict(const=kind), **{
        kind: obj(dict(name=safe), ("name",))}), ("type", kind))
        for kind in ("function", "custom")])
    legacy_fn = obj(dict(name=safe, description=dict(type="string"),
        parameters=fn["properties"]["parameters"]), ("name",))
    legacy_active = dict(anyOf=[present(key, {"not": dict(type="null")})
                               for key in ("functions", "function_call")])
    node = schema_node()
    root_schema = dict(allOf=[ref("schemaNode"), {"not": present("anyOf")},
        dict(anyOf=[present("$ref"), present("type", dict(const="object"))])])
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
    forced = present("tool_choice", dict(anyOf=[dict(const="required"),
        dict(type="object", **present("type", dict(enum=["function", "custom"]))),
        dict(type="object", allOf=[present("type", dict(const="allowed_tools")),
                    present("allowed_tools", present("mode", dict(const="required")))])]))
    strict_tools = present("tools", dict(type="array", contains=present("function", present("strict", dict(const=True)))))
    constrained = dict(anyOf=[json_output, forced, strict_tools,
        present("tools", dict(type="array", contains=present("type", dict(const="custom")))),
        present("functions", nonempty_tools),
        present("function_call", dict(anyOf=[dict(const="none"), dict(type="object")])),
        present("tool_choice", dict(type="object", **present("type", dict(const="allowed_tools")))),
        present("tool_choice", dict(const="none")), dict(allOf=[present("parallel_tool_calls", dict(const=False)),
            present("tools", nonempty_tools)])])
    stops = nullable(dict(anyOf=[dict(type="string", minLength=1, maxLength=256,
        **{"x-q4t-max-utf8-bytes": 256}), dict(type="array", maxItems=4,
            items=dict(type="string", minLength=1, maxLength=256,
                       **{"x-q4t-max-utf8-bytes": 256}))]))
    properties = dict(
        model=dict(type="string", **{"x-q4t-registered-model-or-alias": True}),
        prompt=dict(type="string", minLength=1),
        messages=dict(type="array", minItems=1, items=ref("message")),
        max_tokens=nullable(dict(type="integer", minimum=1, maximum=2147483647)),
        max_completion_tokens=nullable(dict(type="integer", minimum=1, maximum=2147483647)),
        stream=dict(type=["boolean", "null"], default=False),
        store=dict(type=["boolean", "null"], default=False),
        metadata=nullable(dict(type="object", maxProperties=16,
            propertyNames=dict(maxLength=64),
            additionalProperties=dict(type="string", maxLength=512))),
        stream_options=nullable(obj(dict(include_usage=dict(type="boolean", default=False)))),
        request_id=identifier, cancel_token=hex64,
        request_timeout_ms=dict(type="integer", minimum=1, maximum=10800000),
        tools=nullable(dict(type="array", maxItems=128, items=ref("tool"))),
        functions=nullable(dict(type="array", minItems=1, maxItems=128, items=legacy_fn)),
        function_call=nullable(dict(oneOf=[dict(enum=["auto", "none"]),
            obj(dict(name=safe), ("name",))])),
        chat_template_kwargs=template, **template["properties"],
        reasoning_field=dict(enum=["reasoning_content", "reasoning"], default="reasoning_content"),
        include_reasoning=dict(type="boolean", default=True),
        thinking_token_budget=nullable(dict(type="integer", minimum=0, maximum=2147483647)),
        temperature=nullable(dict(type="number", minimum=0, maximum=2, default=1)),
        top_p=nullable(dict(type="number", minimum=0, maximum=1, default=1)),
        n=nullable(dict(type="integer", minimum=1, maximum=128, default=1)),
        frequency_penalty=nullable(dict(type="number", minimum=-2, maximum=2, default=0)),
        presence_penalty=nullable(dict(type="number", minimum=-2, maximum=2, default=0)),
        repetition_penalty=dict(const=1), seed=nullable(dict(type="integer",
            minimum=-9223372036854775808, maximum=9223372036854775807)), stop=stops,
        logprobs=nullable(dict(type="boolean", default=False)),
        top_logprobs=nullable(dict(type="integer", minimum=0, maximum=20)),
        logit_bias=nullable(dict(type="object", propertyNames=dict(pattern="^[0-9]+$"),
            additionalProperties=dict(type="number", minimum=-100, maximum=100))),
        prediction=nullable(obj(dict(type=dict(const="content"), content=dict(anyOf=[
            dict(type="string"), dict(type="array", minItems=1, items=ref("textPart"))])),
            ("type", "content"))),
        response_format=response_format, user=dict(type=["string", "null"]),
        session_id=hex64, prefix_cache_scope=hex64,
        prompt_cache_key=nullable(dict(type="string", maxLength=512)),
        prompt_cache_retention=nullable(dict(enum=["in_memory", "24h"])),
        prefix_cache_write_tokens=dict(anyOf=[dict(const=0), dict(type="integer", minimum=1024, maximum=131072)]),
        parse_response=dict(type="boolean"),
        tool_choice=nullable(dict(oneOf=[dict(enum=["auto", "none", "required"]),
            tool_reference,
            obj(dict(type=dict(const="allowed_tools"), allowed_tools=obj(dict(
                mode=dict(enum=["auto", "required"]), tools=dict(type="array", items=tool_reference)), ("mode", "tools"))), ("type", "allowed_tools"))])),
        parallel_tool_calls=dict(type="boolean", default=True))
    result = obj(properties)
    result.update({"$schema": DRAFT, "$id": "urn:q4t:serve-chat-request:v1",
        "title": "Q4T complete text Chat request structure v1",
        "description": "All supported text request fields. Required semantic and runtime checks are enumerated by x-q4t; schema validation alone is not admission or execution acceptance.",
        "$defs": dict(textPart=text_part, refusalPart=refusal_part,
                       message=message, historyCall=call,
                       tool=tool, schemaNode=node),
        "oneOf": [present("messages"), present("prompt")],
        "dependentRequired": dict(request_id=["cancel_token"], cancel_token=["request_id"]),
        "allOf": [
            when(present("store", dict(const=True)),
                 dict(properties=dict(messages=dict(maxItems=4096)))),
            when(present("tool_choice", dict(type="null")), legacy_active),
            when(legacy_active, dict(allOf=[parsed, dict(properties=dict(
                tools=dict(type="null"), tool_choice=dict(type="null"),
                parallel_tool_calls=dict(const=False)))])),
            when(present("function_call", dict(type="object")),
                present("functions", nonempty_tools)),
            when(present("prediction", dict(type="object")), dict(properties=dict(
                n=dict(enum=[None, 1]), logprobs=dict(enum=[None, False]),
                presence_penalty=dict(type=["number", "null"], maximum=0),
                frequency_penalty=dict(type=["number", "null"], maximum=0),
                tools=dict(enum=[None, []]), functions=dict(type="null"),
                max_completion_tokens=dict(type="null")))),
            when(present("top_logprobs", {"not": dict(type="null")}), present("logprobs", dict(const=True))),
            {"not": dict(required=["max_tokens", "max_completion_tokens"],
                properties={key: {"not": dict(type="null")} for key in ("max_tokens", "max_completion_tokens")})},
            when(present("prompt"), forbid(["tools", "chat_template_kwargs", *template["properties"]])),
            when(present("parse_response", dict(const=True)), dict(required=["messages"])),
            when(any_present(["reasoning_field", "include_reasoning", "tool_choice", "parallel_tool_calls"]), parsed),
            when(present("thinking_token_budget", {"not": dict(type="null")}), parsed),
            when(present("prefix_cache_write_tokens", dict(type="integer", minimum=1)), dict(required=["prefix_cache_scope"])),
            when(forced, present("tools", nonempty_tools)),
            when(present("tool_choice", dict(type="object", allOf=[
                present("type", dict(const="allowed_tools")),
                present("allowed_tools", present("mode", dict(const="required")))])),
                present("tool_choice", present("allowed_tools", present("tools", dict(minItems=1))))),
            when(constrained, dict(allOf=[parsed,
                dict(properties=dict(stop=dict(enum=[None, []])))])),
            when(any_present(["session_id", "prefix_cache_scope"]),
                 dict(properties=dict(stop=dict(enum=[None, []])))),
            when(dict(anyOf=[present(key, {"not": dict(type="null")})
                for key in ("prompt_cache_key", "prompt_cache_retention")]),
                 {"not": dict(required=["prefix_cache_scope"])})],
        "x-q4t": dict(profile="qwen3.8-openai-v3", scope="text; experimental media excluded",
            semantic_contract="serve-input-semantics-v1.json", required_semantic_validation=True,
            runtime_preflight="POST /v1/chat/preflight", defaults_are_annotations=True,
            raw_prompt_always_unparsed=True, parsed_messages_default=True,
            template_option_precedence="chat_template_kwargs overrides top level after both are validated",
            model_omitted="effective served model", output_omitted_or_null="effective configured output default",
            deadline_omitted="effective configured request deadline",
            exact_decimal_data=True, finite_numeric_controls=True,
            duplicate_keys="reject at every JSON object, including historical argument text")})
    return result



def completion_schema():
    chat = chat_schema()["properties"]
    token_ids = dict(type="array", items=dict(type="integer", minimum=0,
        maximum=2147483647), **{"x-q4t-vocabulary-check": True})
    prompt = dict(anyOf=[dict(type=["string", "null"]), token_ids,
        dict(type="array", minItems=1, maxItems=128, items=dict(type="string")),
        dict(type="array", minItems=1, maxItems=128, items=token_ids)])
    fields = {k: chat[k] for k in ("temperature", "top_p", "seed", "logit_bias",
        "presence_penalty", "frequency_penalty", "stop", "user",
        "request_id", "cancel_token", "request_timeout_ms")}
    fields.update(model=dict(type="string", minLength=1, maxLength=256,
        pattern="^[^\\x00-\\x20\\x7f]+$", **{"x-q4t-max-utf8-bytes": 256},
        **{"x-q4t-registered-model-or-alias": True}), prompt=prompt,
        n=nullable(dict(type="integer", minimum=1, maximum=128, default=1)),
        best_of=nullable(dict(type="integer", minimum=1, maximum=128)),
        echo=nullable(dict(type="boolean", default=False)),
        stream=nullable(dict(type="boolean", default=False)),
        logprobs=nullable(dict(type="integer", minimum=0, maximum=5)),
        max_tokens=nullable(dict(type="integer", minimum=0, maximum=2147483647,
                                 default=16)), suffix=nullable(dict(type="string")),
        stream_options=nullable(obj(dict(include_usage=dict(type="boolean"),
            include_obfuscation=dict(type="boolean", default=True)))))
    result = obj(fields, ("model",))
    result.update({"$schema": DRAFT, "$id": "urn:q4t:serve:completion-request:v1",
        "title": "Native OpenAI Completions request",
        "dependentRequired": dict(request_id=["cancel_token"], cancel_token=["request_id"]),
        "allOf": [when(present("stream_options", dict(type="object")),
                       present("stream", dict(const=True)))],
        "x-q4t": dict(profile="q4t-completions-v1", endpoint="/v1/completions",
            required_semantic_validation=True, max_batch_candidates=128,
            best_of_default="n", best_of_selection_streaming=False,
            max_tokens_default=16, prompt_omitted_or_null="empty document",
            empty_document_seed="<|endoftext|>; counted as one actual input token",
            prompt_array_empty="one empty token prompt",
            model_lookup="registry including explicit aliases; unknown is 404",
            token_input="execute exact IDs without decode/re-encode",
            echo="original string or UTF-8 repaired decoded IDs",
            suffix="native FIM prefix/suffix/middle tokens; no Chat template",
            limits="all prompts checked before execution; total candidate output and input quota",
            semantic_contract="serve-input-semantics-v1.json")})
    return result


def completion_transport():
    number = dict(type="number", maximum=0)
    probabilities = obj(dict(tokens=dict(type="array", items=dict(type="string")),
        token_logprobs=dict(type="array", items=nullable(number)),
        top_logprobs=dict(type="array", items=nullable(dict(type="object",
            additionalProperties=number))),
        text_offset=dict(type="array", items=dict(type="integer", minimum=0))),
        ("tokens", "token_logprobs", "top_logprobs", "text_offset"))
    usage = obj({key: dict(type="integer", minimum=0)
                 for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                ("prompt_tokens", "completion_tokens", "total_tokens"))
    choice = obj(dict(text=dict(type="string"), index=dict(type="integer", minimum=0),
        logprobs=nullable(ref("logprobs")),
        finish_reason=dict(enum=["stop", "length", "content_filter"])),
        ("text", "index", "logprobs", "finish_reason"))
    envelope = dict(id=dict(type="string"), object=dict(const="text_completion"),
        created=dict(type="integer", minimum=0), model=dict(type="string"),
        choices=dict(type="array", items=choice), usage=ref("usage"))
    completion = obj(envelope, ("id", "object", "created", "model", "choices", "usage"))
    chunk_choice = obj(dict(choice["properties"],
        finish_reason=dict(enum=[None, "stop", "length", "content_filter"])),
        choice["required"])
    chunk = obj(dict(envelope, choices=dict(type="array", items=chunk_choice),
        usage=nullable(ref("usage")), obfuscation=dict(type="string")),
        ("id", "object", "created", "model", "choices"))
    return {"$schema": DRAFT, "$id": "urn:q4t:serve:completion-transport:v1",
        "$defs": dict(completion=completion, chunk=chunk, logprobs=probabilities, usage=usage),
        "x-q4t": dict(equal_probability_array_lengths=True,
            offsets="Unicode scalar positions in returned text, original NFC source alignment",
            selected_logprobs="full raw target distribution; no Chat top20 sentinel",
            best_of="mean generated token probability, includes actual EOS; ties keep candidate order",
            usage="prompt once per input; all generated candidates including discarded ones",
            stream="indexed text deltas, per-choice finish, optional aggregate usage, DONE and EOF")}


def vision_schema():
    result = chat_schema()
    uri = dict(type="string", minLength=1,
        description="Canonical padded standard base64 or exact PNG/JPEG data URL; no network URLs.")
    url = dict(oneOf=[uri, obj(dict(url=uri, detail=dict(const="auto")), ("url",))])
    image_uri = dict(type="string", minLength=1,
        description="PNG/JPEG/static GIF/WebP bytes in canonical base64 or a data URI, or credential-free HTTP(S) under deployment fetch policy.")
    image_url = dict(oneOf=[image_uri, obj(dict(url=image_uri,
        detail=dict(enum=["auto", "low", "high", "original"])), ("url",))])
    result["$defs"]["imagePart"] = obj(dict(type=dict(const="image_url"), image_url=image_url), ("image_url",))
    file = obj(dict(file_id=dict(type="string", minLength=1, maxLength=128,
        pattern=r"^[A-Za-z0-9_-]+$"), file_data=dict(type="string"),
        filename=dict(type="string", minLength=1, maxLength=1024,
            pattern=r"^[^\u0000-\u001f\u007f]+$",
            **{"x-q4t-max-utf8-bytes": 1024})))
    file["oneOf"] = [present("file_id"), present("file_data")]
    result["$defs"]["filePart"] = obj(dict(type=dict(const="file"), file=file),
        ("type", "file"))
    result["$defs"]["videoPart"] = obj(dict(type=dict(const="video_frames"),
        video_frames=dict(type="array", minItems=2, maxItems=32, items=url),
        video_timestamps=dict(type="array", minItems=2, maxItems=32,
            items=dict(type="number", minimum=0, maximum=86400),
            description="One strictly increasing seconds value per source frame. Omission means 24fps from zero.")), ("video_frames",))
    message = result["$defs"]["message"]
    message["properties"]["content"]["anyOf"][1]["items"] = dict(oneOf=[
        ref("textPart"), ref("refusalPart"), ref("imagePart"), ref("videoPart"),
        ref("filePart")])
    message["allOf"].append(when(present("role", {"not":dict(const="user")}),
        dict(properties=dict(content=dict(anyOf=[dict(type=["string", "null"]),
            dict(type="array", items=dict(oneOf=[ref("textPart"), ref("refusalPart")]))])))))
    result["x-q4t"].update(profile="q4t-vision-v1", scope="explicit media service profile",
        server_flags=["--allow-media", "--no-mtp", "--max-seq", "1"],
        required_semantic_validation=True, acceptance="separate release attestation",
        temporal_patch_size=2, max_media_items=8,
        file_inputs="Chat file parts require actual PDF; original text and pages use common preparation, preflight and stored snapshot ownership",
        unsupported=["video_container_on_server", "audio", "MTP",
                     "state_cache", "media_chunked_prefill", "multiple_execution_slots"])
    result["allOf"].append(forbid(["session_id", "prefix_cache_scope"]))
    return result


def message_schema():
    full = chat_schema()
    return {"$schema": DRAFT, "$id": "urn:q4t:serve:message-profile:v3",
        "title": "qwen3.8-openai-v3 text message profile",
        "description": "Text message shapes only. Tool association, full request options and runtime admission still require native validation; media uses the vision request schema.",
        "type": "object", "required": ["messages"],
        "properties": dict(messages=full["properties"]["messages"]),
        "$defs": {k: full["$defs"][k] for k in
                  ("message", "textPart", "refusalPart", "historyCall")},
        "x-q4t": dict(profile="qwen3.8-openai-v3",
            developer_mapping="ordered system ChatML messages",
            name="JSON quoted participant; angle brackets escaped",
            name_max_utf8_bytes=4096, user_message_required=False,
            full_request_validation=False)}


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
        ("chat-file-inputs", ["src/server/chat_file_inputs.cpp",
            "src/server/file_expansion.cpp", "src/server/chat_storage.cpp",
            "src/server/chat_preflight.cpp", "src/server/chat_generation.cpp"],
         "Chat and preflight validate the complete request before acquiring files. Standard user file parts accept PDF file_id or file_data with optional filename. PDF text and all admitted page images enter the actual template; image URL inputs use common bounded PNG/JPEG/static GIF/WebP acquisition and conversion with auto/low/high/original detail. Generation owns cancellation during preparation. Stored Chat retains original content_parts and private immutable snapshot references bound to completion/message identity; an unknown parent publication keeps absolute holds and is never regenerated. Preflight owns prepared bytes only for that call, reports actual rasterized page/source counts and performs no model execution. Combined media, token, decoder and storage budgets remain enforced; this implementation still requires integrated business/release acceptance."),
        ("responses-request-fields", ["src/server/responses_plan.cpp",
            "src/server/responses_execution.cpp", "src/server/responses_tokens.cpp"],
         "Responses create rejects explicit null for access_programs/input/model/prompt_cache_options/text/tool_choice/tools/user and reasoning.mode; nullable fields keep their own defaults. Input type and original envelope are checked before prompt state lookup, then expanded requests are checked against effective client limits. Count and compact normalize only their separately nullable SDK fields. All seven SDK service-tier values are accepted; execution reports the actual default scheduler tier without claiming hosted pricing, dedicated capacity or latency guarantees."),
        ("responses-input-tokens", ["src/server/responses_tokens.cpp",
            "src/server/chat_input_tokens.cpp", "src/server/responses_plan.cpp",
            "src/server/chat_plan.cpp", "src/server/media_admission.cpp"],
         "POST /v1/responses/input_tokens uses the actual native template, tokenizer and expanded media count. Omitted/null body, null model and empty input use defaults without a dummy message. Strict count-only fields; previous/conversation/encrypted history resolves in the authenticated client scope without beginning or mutating a response. Input/client/context and data concurrency/deadline limits remain. Text, PDF/image and frozen summaries use shared actual file preparation without final-answer generation. New spreadsheet summaries execute real bounded native model steps under the parent admission/cancellation and one RPM/cumulative actual TPM lease. The standard input_tokens value describes the final initial prompt, excluding preparation model usage; X-Q4T-Preparation-Input-Tokens/Output-Tokens/Model-Steps report that actual auxiliary work even on ordinary error responses. No response is begun or mutated, no tools/final answer are run, and no cache write occurs; private file read leases remain possible. Truncation auto protects the current turn and tool dependencies; count has zero final-answer output budget. Existing immutable summaries are reused exactly; independent fresh inputs remain independent requests. Friendly/pragmatic model-owned personality presets enter actual system guidance, shared with native Responses create; no claim of hosted-model prompt or tokenizer equivalence. File counting uses the shared prepared-input path; remaining endpoint executors/business/release remain linked A09/A10/A11 tasks."),
        ("stored-chat", ["src/server/chat_storage.cpp", "src/server/chat_storage_options.cpp",
            "src/server/chat_generation.cpp", "src/server/object_worker.cpp",
            "include/q4t/server/application_routes.h"],
         "store defaults false; null omits it. Metadata is nullable or16 string pairs with64/512 Unicode scalar limits, never an auth identity. store=true retains actual completed JSON or SSE choices/logprobs/usage and original input messages after a durable receipt, before final success; receipt loss is queried by original completion ID without replay. Model/filter metadata/list/messaging CRUD are client scoped; asc default, after cursor, limit1..100. Updates preserve original absolute retention, normally30days. No store means no object I/O. At most4096 stored input messages and32MiB complete response; object size, namespace/global quota and deadline may reject independently. Management has bounded observation admission, separate from generation. Original media/tool fields are preserved; query is closed and metadata uses nested URL keys."),
        ("custom-tools", ["src/server/custom_tools.cpp", "src/server/custom_grammar.cpp",
             "src/server/custom_regex.cpp", "src/server/regex_syntax/lib.rs",
             "src/server/chat_response_parser.cpp"],
         "Custom tools retain type=custom and custom.name/input, with raw text payloads. Omitted format is text; grammar format accepts regex/Lark with definition 1..16384 UTF-8 bytes. Named and allowed references match declared type and name; names are unique across function/custom. Each custom request uses the existing bounded grammar worker. Model-only definitions and histories use native function tags with one input parameter containing a JSON string literal; declared names determine the outward custom/function type. Rust regex syntax and Lark lexer expressions are mapped to the JSON-string model payload without weakening Unicode, escapes, anchors or word boundaries. Expansion512KiB, assertion graph4096 nodes; existing worker time/memory limits remain. Incremental UTF-8/escape decoding is independently compared to the final parse. Success terminal and EOF gate client execution; custom validators are registered separately. Identified custom/function history may share one batch with exactly one result per ID."),
        ("legacy-functions", ["src/server/legacy_tools.cpp", "src/server/chat_plan.cpp",
             "src/server/chat_response_parser.cpp"],
         "Non-null functions/function_call choose legacy output: one function_call, matching finish_reason and incremental name/arguments deltas without tool IDs. Normalized controls share production grammar and final validation; no mixing non-null modern controls or parallel=true. Legacy assistant.function_call must have a contiguous role=function result matching its name; null result is empty text. Internal association IDs never escape onto the legacy wire. Errors refer to original legacy input paths. Null controls are omission."),
        ("prediction", ["src/server/prediction.cpp", "src/server/chat_plan.cpp", "src/server/chat_generation.cpp"],
         "Static string/text parts become target-verified tokens. Prediction never forces the target distribution. Accepted tokens are consumed once; remaining provided tokens count as rejected completion tokens. Effective output/context and client quota include them. n>1/logprobs/positive penalties/tools/max_completion_tokens are incompatible only with a non-null prediction."),
        ("json-lexical", ["src/io/json.cpp", "include/q4t/server/request_json.h", "include/q4t/server/request_limits.h"],
         "UTF-8 JSON; no duplicate keys, NaN/Infinity literals or trailing data; depth64/65536 values. File-bearing Chat/preflight/Responses/Conversations bodies allow96MiB total decoded string bytes; Completions and nested tool-argument/schema JSON parsers retain16MiB. Chat/preflight/schema/history data retain exact decimal lexemes up to16384 characters, including binary64 overflow/underflow. Generation numeric controls separately require finite/range-valid values. Integer fields use exact source lexemes, not rounded binary64 values. Model/config/cancel readers retain their previous limits."),
        ("transport-body", ["src/server/http_request.cpp", "src/server/chat_http.cpp",
             "include/q4t/server/http_request.h", "include/q4t/server/request_limits.h", "include/q4t/server/http_framing.h",
             "include/q4t/server/http_observation.h", "include/q4t/server/http_connection.h"],
         "HTTP/1.1 Content-Length or chunked decoded bodies; CL/TE mixing and ambiguous framing reject before dispatch. Authenticated/admitted 100-continue only; trailers cannot override routing/framing/credentials. Chunk metadata64KiB, line8KiB, trailers128. Sequential keep-alive at most100 requests/5s idle for the core; every request has fresh identity/admission and its own absolute header+body deadline. Finite JSON uses Content-Length; HTTP/1.1 SSE is chunked with a zero terminal only after successful completion. Truncated streams close and never retry or resume onto a new model generation. HTTP/1.0 and explicit close retain close-delimited stream fallback. Native ingress v4 separately configures bounded HTTP limits, path prefix and explicit proxy trust. Files POST is multipart/form-data with incremental parsing, 512MiB file and 512MiB+65536 wire ceilings; private client v4 max_file_body_bytes and native policy v5 file_body_bytes are explicit separate caps. Older policy versions retain their ordinary body limits. Files and Upload parts have fixed working reservations and separately charged persistent storage quotas, not a full-body buffer. Upload JSON create/complete/cancel and streamed64MiB parts use native client-scoped persistence; public expiry3600s, receipt-only grace86400s, final8GiB/8192parts/16384chunks bounds. Completion verifies actual bytes and optional MD5 before atomic File publication; cancellation has independent32MiB working reservation to remain admissible when data reservations fill. Ordinary aggregate is192MiB. GET file content streams original bytes with Content-Length; interrupted download closes without appending JSON/SSE or retrying. 96MiB ordinary application body ceiling with192MiB aggregate reservation; explicit client and ingress caps may be lower, existing configured caps are unchanged; capacity failure is HTTP503/body_capacity_exhausted/param=body before body read. This is not a RAM measurement. Exact GET metrics/config/capabilities/models share four observation connections, independent of four health/cancel controls and business capacity; observation overflow is HTTP503/observation_capacity_exhausted. Primary unclassified headers share its summed handler limit. Explicit operations-port adds a loopback-only independent eight-handler listener (four observation/four control); it rejects generation, preflight and unknown method/path before body, preserving client authentication and cancel authority. Operations headers share only its own eight slots; no local flood isolation. Operations total overflow is HTTP503/operations_capacity_exhausted. Both listeners close on shutdown or setup failure. Cancel body4096. JSON-generation POST uses application/json UTF-8 with no query strings; management GET supports its declared pagination/filter query and no body; methods exact; framing and header limits apply before parsing. Fixed first receive-failure counters exclude business validation."),
        ("model-and-credentials", ["src/server/input_contract.cpp", "src/server/chat_contract.cpp"],
         "If present model equals served identifier. request_id and fresh lowercase64hex cancel_token together; reserved auto prefix forbidden. Active ID registry/cancel authority are runtime checks, not idempotency. Optional immutable gateway policy scopes IDs, cancellation and cache keys by authenticated client. Wire IDs remain unchanged."),
        ("utf8-byte-length", ["src/server/input_contract.cpp", "src/server/text_stop.cpp"],
         "x-q4t-max-utf8-bytes counts encoded bytes, not codepoints/UTF-16. Historical names/parameter keys forbid C0, DEL and angle brackets; stop valid UTF-8 1..256 bytes, up to4."),
        ("message-profile", ["src/server/chat_template.cpp", "src/server/chat_prompt.cpp"],
         "System/developer instruction messages retain order as native system ChatML; no required user role. Participant names are JSON quoted with escaped angle brackets and limited to4096 UTF-8 bytes, never authorization identity. Assistant refusal strings/one refusal content part are rendered as text. Content is required unless assistant has nonempty tool_calls or a refusal string. Other media and content shapes remain role-checked."),
        ("reasoning-aliases", ["src/server/reasoning_options.cpp"],
         "Only assistant may carry reasoning aliases; two string values must agree byte-for-byte. Null alias may defer to the other. Hidden output preserves computation/usage."),
        ("historical-arguments", ["src/server/chat_tool_history.cpp"],
         "Arguments are object or JSON text of an object using production parser budgets. Argument keys are JSON data, not function identifiers. Exact decimals, arbitrary keys and null/string identity survive rendering, with JSON wrapper and escaped markers when native XML is ambiguous. No silent parameter dropping."),
        ("historical-batches", ["src/server/chat_tool_history.cpp"],
         "Any ID in a batch requires all nested calls to have valid unique IDs and type=function. Exactly one contiguous result for every ID; no orphan/duplicate/missing/mixed IDs. Text results may reorder. Unlabelled native legacy has distinct preserved rules; IDs are unique per batch, not globally."),
        ("tool-definitions", ["src/server/input_contract.cpp", "src/server/output_constraints.cpp"],
         "Function names unique. allowed_tools references declared unique names, filters template and grammar, and is checked again on actual output; required needs a nonempty subset, auto may be empty. Incremental tool fragments are provisional until successful whole-response terminal and EOF. Auto/raw parameter schemas advisory, but parameters/properties must be objects. A named choice must exist when compiling tool arguments; none mode removes tools and intentionally does not compile their argument schemas."),
        ("constraint-combinations", ["src/server/output_constraints.cpp", "src/server/text_stop.cpp"],
         "JSON output/none/required/named/strict tools/nonparallel nonempty tools activate constraints. Parsed messages required; text state caches use completed target-only snapshots with layout checking, request-local grammar/sampler and successful-response publication. Cache startup configuration must be enabled; effective stop remains forbidden. JSON response_format and modern/legacy tools choose a JSON-content or tool-only branch; required/named force tools. Tool declarations max128 with safe names in constrained mode. Compiled description <=2MiB; resource rejection is distinct from invalid syntax. Incremental content/tool/reasoning deltas remain provisional until successful terminal and EOF."),
        ("schema-v2", ["src/server/output_schema.cpp", "src/server/schema_graph.cpp",
             "src/server/schema_grammar.cpp", "src/server/schema_token_guard.cpp",
             "src/server/schema_receipt.cpp", "src/server/schema_string.cpp"],
         "Messages expose the response schema and annotations as exact delimiter-safe system format data; shared preflight and cache prefix include those tokens. Raw prompt stays caller-owned. Official structured-output graph: local refs/defs/recursion/anyOf; root resolves to object and excludes root anyOf; closed objects and all properties required when strict. 5000 total properties/10 nesting levels/1000 enum values/120000 constrained characters, with15000 for string enums over250 members. Exact decimal enum/const, numeric bounds and multipleOf; Unicode lengths, arrays, patterns and9 formats. Additional bounded graph/grammar/worker resources fail explicitly. Native-format checks run only in the isolated worker; parent binds schema/tool/value receipts before successful terminal. Simple unambiguous trained XML is retained; rich tool arguments are one JSON object parameter with refs relocated only in model definitions. Published structural schema requires native semantic validation for refs, aggregate limits, exact relationships and strict requirements."),
        ("response-parser-prefix", ["src/server/chat_response_parser.cpp", "src/server/thinking_budget.cpp"],
         "Production parser and thinking budget initialization must accept native generation prefix. Real thinking closure token preparation remains execution-time validation."),
        ("sampling-and-choices", ["src/server/sampling.cpp", "src/server/chat_generation.cpp",
             "src/server/token_logprobs.cpp", "src/server/chat_choices.cpp"],
         "Default/null temperature=1; temperature0 is explicit greedy. Per-choice RNG from exact signed64 seed, top_p nucleus, generated-history penalties and token bias are applied to actual target logits after candidate masks. Non-greedy/logprob requests bypass greedy MTP/packed argmax. Token bias IDs must be distinct numerically and in the served vocabulary. Logprobs describe raw finite vocabulary distribution before masks/penalties; selected outside raw top20 is -9999. Hidden-only tokens excluded, boundary-crossing tokens retain actual bytes/probability once. Choices share one request/cancel/admission and prompt charge; output quota/usage sum across choices. JSON response<=32MiB; pending/collected probability storage each<=16MiB."),
        ("configured-capabilities", ["src/server/chat_plan.cpp"],
         "Structured backend must actually be built; state caches require their effective enabled configuration. Probe flags describe hypothetical config; they do not prove a running backend or cache."),
        ("token-and-slot-budget", ["src/server/chat_plan.cpp", "include/q4t/server/prefix_cache_policy.h"],
         "Production tokenizer and max_len determine input/output allowance. Uniform clips output, explicit capacity fits full capped budget; prefix seed must match configured checkpoint and fit prompt. Optional preflight preview has conservative encoded8MiB bound."),
        ("startup-memory-admission", ["include/q4t/server/startup_admission.h",
             "src/server/server_options.cpp", "src/server/chat_server.cpp",
             "src/server/chat_plan.cpp"],
         "Fresh global MemAvailable checks before metadata, before either model allocation path, and before readiness. Default2048MiB headroom; preallocation also requires the existing allocation peak estimate. Missing/inconsistent observation, missing estimate or overflow stops startup. Zero or --no-budget explicitly bypasses this guard; request memory admission remains independent. Effective configuration exposes all three snapshots. No reservation, silent capacity retry or physical RAM hard-limit guarantee."),
        ("cuda-allocation-quota", ["include/q4t/runtime/allocation_quota.h",
             "include/q4t/runtime/cuda_allocation_quota.h",
             "src/runtime/cuda_allocation_quota.cpp",
             "src/runtime/cuda_quota_backend.h",
             "include/q4t/server/cuda_quota_policy.h",
             "include/q4t/server/cuda_quota_metrics.h",
             "src/runtime/startup_reservation.cpp",
             "cmake/CudaCleanupBoundary.cmake"],
         "Production-linked CUDA requested-byte quota covers device, managed, both pinned APIs and async allocations, including startup physical reservations and process caches. Default auto resolves mem_fraction times MemTotal; zero explicitly disables, independently of no-budget. Fixed tables reject before backend. Take does not double charge; async free remains pending until a successful matching-generation stream or device synchronization. Event and stream destruction are not completion credit. Direct recoverable serve buffers report cuda_allocation_quota_exhausted; partial model execution failures keep existing stop policy. Configure once per process. External DSOs, host heap, file cache, driver and retained CUDA pools are excluded: not a physical RAM limit."),
        ("device-memory-failure", ["include/q4t/server/device_memory.h",
             "src/server/cuda_memory_backend.h", "src/server/chat_device_memory.cpp",
             "src/server/chat_server.cpp", "src/server/chat_generation.cpp"],
         "Serve-owned text and scheduler buffers distinguish proven capacity exhaustion from other device failures. Only a known OOM indicator may be consumed: optional buffers fall back, required request buffers return device_memory_exhausted (JSON503 or SSE error). Other device or any release failure closes readiness and stops serve with nonzero exit. Release ownership is consumed once and checked before successful terminal/cache publication. Untyped MTP preparation failure drains and stops. Media and model-internal allocations/releases are not fully covered. This is not physical RAM reservation or a hard limit."),
        ("backend-cleanup", ["include/q4t/runtime/cleanup_policy.h",
             "src/runtime/startup_reservation.cpp", "cmake/CudaCleanupBoundary.cmake",
             "src/model/model.cu", "src/model/model_owner.cpp", "src/mtp/mtp.cu",
             "src/server/chat_server.cpp"],
         "Production q4t links checked direct CUDA free/freeAsync/freeHost/eventDestroy/streamDestroy/streamSynchronize/deviceSynchronize/eventSynchronize calls. Unknown cleanup/completion fails closed with immediate exit1, no destructor retry and no guaranteed HTTP error. Only the explicit serving cudaFree owner may receive errors to stop/drain and publish its existing error protocol. External shared-library internals and other executables are outside this link boundary. Successful async free/destroy does not prove completion or physical reclaim. Requested MTP load failure stops either startup path; --no-mtp remains explicit opt-out. Model load completion is checked before source release; failed MTP load drains before loader destruction. No model arithmetic change or complete media allocation guarantee."),
        ("media-admission-v2", ["include/q4t/vision/media_plan.h",
             "src/vision/media_plan.cpp", "include/q4t/vision/processor.h",
             "src/vision/processor.cpp", "src/vision/vision.cu",
             "include/q4t/server/media_admission.h", "src/server/media_admission.cpp",
             "src/server/chat_contract.cpp", "src/server/chat_prompt.cpp",
             "src/server/chat_input.cpp", "src/server/chat_generation.cpp",
             "include/q4t/model/vision_positions.h", "include/q4t/vision/configuration.h"],
         "Explicit media profile (acceptance separate): user only, closed image_url/video_frames/video_timestamps parts, auto detail only, canonical standard padded base64 or exact PNG/JPEG data URLs with matching headers. All item/frame source and geometry limits precede decoding. Video prompt has averaged pair timestamps and one t=1 block per temporal group. Nearest-even geometry matches the fixed reference; odd frames repeat the last frame. Shared host plan expands tokens before preflight/slot/output/client/TPM budgeting; processor reuses the geometry and verifies decoded size. Decoder owned C allocations including prefixes have a 256MiB quota, nonthrowing callbacks and cleanup before quota400 or actual bad_alloc503. Cancellation checks item/frame boundaries and before GPU, not preemption of a C decode/kernel. Attention limit is queried from current device default minus static shared and function dynamic maximum; unknown fails requested media startup. Policy values are exposed in configuration/discovery. These logical limits do not bound allocator internal realloc peaks or reserve whole-server physical RAM. GPU/HTTP acceptance is separate."),
        ("runtime-memory-admission", ["src/runtime/system_memory.cpp",
             "src/server/memory_admission.cpp", "src/server/chat_memory.cpp",
             "src/server/chat_http.cpp", "src/server/chat_admission.cpp"],
         "Configurable Linux-wide MemAvailable gate, independent of startup allocation estimates; default2048MiB floor, 512MiB recovery margin, 250ms cached observation with fresh preparation and slot handoff. Missing/invalid samples reject new chat/preflight work, including queued requests; readiness closes, liveness/control remain available and active generations continue. No byte reservation, CUDA allocation or whole-server/cgroup hard-limit guarantee."),
        ("runtime-admission", ["src/server/chat_generation.cpp", "src/server/chat_preflight.cpp",
             "src/server/chat_admission.cpp", "include/q4t/server/request_admission.h",
             "include/q4t/server/sequence_admission.h", "src/server/server_options.cpp"],
         "Availability/registry/cache lease/GPU health/deadline/cancellation/grammar compilation are not established by schema or offline probe. Chat/preflight share a configured inflight bound through output cleanup; only generation enters the separately bounded capacity-FIFO slot queue after preparation. Queue fullness can reject independently of whole-server RAM and connection/body limits. Preflight does not reserve slots or guarantee execution or answer quality."),
        ("permanent-model-health", ["include/q4t/runtime/model_health.h",
             "include/q4t/server/service_health.h", "src/ple/page_reader.cpp",
             "src/ple/ple_embedding.cpp", "src/server/chat_health.cpp",
             "src/server/chat_http.cpp", "src/server/chat_metrics.cpp",
             "src/server/chat_generation.cpp", "src/server/chat_preflight.cpp",
             "src/server/chat_admission.cpp", "src/server/chat_scheduler.cpp"],
         "Permanent PLE read and existing upload failures latch on the dependency instance. Healthy CUDA alone cannot keep readiness open. Ready/health endpoints return503 and q4t_ready=0 while liveness remains independent; pure reader faults do not change gpu_healthy. New Chat/preflight, slot handoff and execution check the same state. Model faults use model_unhealthy; an already-started stream uses the existing single error terminal when writable. Successful short-read retries, memory recovery and NVML observations cannot clear a permanent fault. No automatic dependency reload or request replay. Existing fatal CUDA completion/cleanup can exit1 before HTTP reporting; that boundary is unchanged.")]
    rules.append(("native-optional-cache-isolation", [
        "include/q4t/server/cache_io.h", "include/q4t/server/cache_health.h",
        "src/server/cache_io.cpp", "src/server/cache_protocol.h",
        "src/server/cache_worker.cpp", "src/server/grammar_process.cpp",
        "src/server/session_cache.cpp", "src/server/chat_preflight.cpp",
        "src/server/chat_server.cpp"],
        "Private and prefix snapshots use independent exec-only filesystem workers, not an external gateway. The model owns tensor copies and byte checksums; workers inherit no model/GPU lease descriptors. Each file IPC operation waits at most two seconds and checks cancellation; normal multiblock transfers still obey the request deadline. A missing receipt invalidates that cache generation; no write, request or model work is replayed. One controller and worker per domain retain capacity through unconfirmed cleanup; replacement requires actual reap and at most three starts per minute. Optional cache unavailability does not close core readiness or restart a healthy model. Session leases remain held, partial restores require model-state reset, and only successful completion publishes snapshots. Disk charges include actual allocated blocks. Config cache_io reports availability, recovery and observed RSS without directory/session data; three bounded 8MiB payload buffers plus at most4096 initialization bytes are unified RAM, not a whole-process cap or CPU offload."))
    rules.append(("native-completions-and-models", [
        "include/q4t/server/model_catalog.h", "src/server/model_catalog.cpp",
        "include/q4t/server/completion_plan.h", "src/server/completion_plan.cpp",
        "include/q4t/server/completion_response.h", "src/server/completion_response.cpp",
        "src/server/chat_generation.cpp", "src/server/token_logprobs.cpp",
        "src/server/sampling.cpp", "include/q4t/text/tokenizer.h", "src/text/tokenizer.cpp",
        "src/server/chat_http.cpp", "src/server/chat_metrics.cpp",
        "src/server/client_request.cpp", "src/service/ingress_main.cpp"],
        "Raw Completions execute strings, homogeneous prompt batches, exact token IDs and token batches. Empty/null document uses one actual endoftext seed. Default/null max_tokens16, zero allowed. n/best_of independent candidates share one request/cancel/quota; at most128 batch candidates. best_of>=n and selection cannot stream; rank by full actual mean generated-token logprob including EOS, stable candidate-order ties; all work charged. No Chat template or reasoning/tool stripping. Echo preserves original string with NFC source offsets or repaired decoded IDs; prompt probabilities are teacher-forced from real target logits in bounded16-row readbacks, first unconditional token null. Sampled probability is full raw distribution, independent of Chat's top20 sentinel. Suffix uses actual model FIM tokens, preserving raw prefix IDs. JSON/SSE text_completion carries parallel probability arrays and Unicode character offsets; successful final usage/DONE only after all choices and cleanup. Optional stream obfuscation defaults on. Models register stable process-lifetime created and explicit aliases; no per-query timestamp. GET detail percent-decodes once; unknown404; loaded base model DELETE403 and never modifies read-only assets. Alias lookup shared by Chat/preflight/Completions; served name/aliases configurable, no external gateway required."))
    rules.append(("native-responses-conversations", [
        "src/server/responses_plan.cpp", "src/server/responses_execution.cpp",
        "src/server/responses_api.cpp", "src/server/responses_state.cpp",
        "src/server/responses_jobs.cpp", "src/server/responses_owner.cpp",
        "src/server/responses_output.cpp", "src/server/conversation_api.cpp",
        "src/server/conversation_state.cpp", "src/server/object_store.cpp",
        "src/server/application_query.cpp", "include/q4t/server/application_routes.h",
        "src/server/chat_responses.cpp"],
        "Responses share native model execution and preserve item identities, actual token usage/probabilities and original sequenced events; no Chat DONE. Store defaults true, previous instructions do not carry forward, client-scoped history and conversation commits use isolated bounded object I/O and atomic revision-checked transactions. Background owns its admission permit and original deadline independently of subscribers. Serialized retained input is charged once per live job; default512MiB aggregate, configured by --background-max-input-mib from1 to2048MiB, separate from HTTP and persistent storage quotas; this is not a process RSS bound; cancel is idempotent empty POST returning200, with independent bounded cancellation capacity. Temporary background records cover execution then600 seconds. Recovery proves old execution owner death using boot/PID birth/namespace identity: queued may resume original ID, started fails with saved partial output and is never replayed. Storage/monitor failure cannot restart the model or close healthy core readiness. Conversations support CRUD, item append/list/retrieve/delete and include projection. Application queries use closed per-route fields with exact integers, true/false booleans and include arrays; other routes keep prior query rejection. State APIs, shape validation and capability declarations do not complete pending compaction/summary/encryption/file/tool executors."))
    rules.append(("native-application-retention", [
        "include/q4t/server/object_retention.h", "src/server/object_retention.cpp",
        "src/server/object_worker.cpp", "src/server/object_store.cpp",
        "src/server/state_key_admin.cpp", "src/server/state_retention_cli.cpp"],
        "Expiry collection preserves queued/in_progress tasks until recovery has durably recorded a terminal state, and follows typed same-owner references from retained roots. Expired unrooted cycles can be collected; unknown lifecycle/shape preserves that owner's records and is reported. No references are extracted from user text or tool arguments. Permanent conversations/prompts retain explicit-delete semantics; permanent orphan items require an explicit creation-time cutoff, not an inferred orphan age. Native same-UID state-retention inspect/collect uses the existing bounded isolated store/admin channel. Inspect does not apply proposed cleanup; collect rechecks the exact SHA256 plan and returns409 on state/eligibility change. At most128 selected objects from one owner per atomic journal transaction; inspect again for further batches. Planner bounds100000 nodes,64MiB metadata estimate and2M references; resource failure retains data and does not deny otherwise usable CRUD. Lost mutation receipt is commit_unknown with no replay; reinspection/recovery uses the existing journal. Offline access requires exclusive store ownership. No business payload/key bytes in receipts; maintenance errors cannot restart a model or close healthy core readiness."))
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
    chat = dict(anyOf=[external("serve-chat-request-v1.schema.json"),
                       external("serve-vision-request-v1.schema.json")],
        **{"x-q4t-media-profile": "Explicit --allow-media --no-mtp --max-seq 1; check runtime capabilities. video_frames/video_timestamps are Q4T extensions. Structural validity does not establish model task accuracy."})
    cancel = obj(dict(request_id=chat_schema()["properties"]["request_id"],
        cancel_token=dict(type="string", pattern="^[0-9a-f]{64}$")), ("request_id", "cancel_token"))
    preflight = obj(dict(request=chat, include_prompt=dict(type="boolean", default=False),
                         include_token_ids=dict(type="boolean", default=False)), ("request",))
    paths = {}
    model = obj(dict(id=dict(type="string"), object=dict(const="model"), created=dict(type="integer"),
                    owned_by=dict(const="q4t")), ("id", "object", "created", "owned_by"))
    models = obj(dict(object=dict(const="list"), data=dict(type="array", minItems=1,
        maxItems=33, items=model)), ("object", "data"))
    health = obj(dict(status=dict(enum=["ok", "unhealthy"]), gpu_healthy=dict(type="boolean"),
        requests_running=dict(type="integer", minimum=0), seq_slots_free=dict(type="integer", minimum=0),
        seq_slots_total=dict(type="integer", minimum=1)),
        ("status", "gpu_healthy", "requests_running", "seq_slots_free", "seq_slots_total"))
    endpoints = [
        ("/v1/completions", "post", external("serve-completion-request-v1.schema.json"),
         external("serve-completion-transport-v1.schema.json", "#/$defs/completion")),
        ("/v1/chat/completions", "post", chat, external("serve-transport-v1.schema.json", "#/$defs/completion")),
        ("/v1/chat/preflight", "post", preflight, external("serve-preflight-v1.schema.json")),
        ("/v1/requests/cancel", "post", cancel,
         obj(dict(cancel_requested=dict(const=True)), ("cancel_requested",))),
        ("/v1/models", "get", None, models),
        ("/v1/models/{model}", "get", None, model),
        ("/v1/models/{model}", "delete", None, None),
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
            if path == "/v1/completions":
                success["content"]["text/event-stream"].update({
                    "x-q4t-event-schema": external("serve-completion-transport-v1.schema.json", "#/$defs/chunk"),
                    "x-q4t-stream-contract": "Indexed text/probability deltas; per-choice finish; optional aggregate usage; DONE then EOF. Error terminal never publishes successful aggregate usage."})
        if path == "/v1/models/{model}":
            operation["operationId"] = "v1_models_" + method
            operation["parameters"].append(dict(name="model", **{"in": "path"}, required=True,
                schema=dict(type="string"), description="Exact registered identifier or alias; percent-encoded once"))
            operation["responses"]["404"] = response(error, "Model not registered")
            if method == "delete":
                operation["responses"].pop("200")
                operation["responses"]["403"] = response(error, "Loaded base models are read-only; this registry contains no user-owned fine-tuned models")
        if path in ("/healthz", "/livez", "/readyz"):
            operation["responses"]["503"] = response(schema, "Unhealthy, not ready, or draining")
        if path == "/healthz":
            operation["description"] = (
                "Local backend health. Permanent PLE dependency faults also return 503; "
                "gpu_healthy reports CUDA health independently. "
                "Liveness and memory admission use /livez and /readyz.")
        paths.setdefault(path, {})[method] = operation
    from tools.serve_responses.build_contract import add_openapi
    add_openapi(paths, response, error)
    return dict(openapi="3.1.0", info=dict(title="Q4T serving contract",
        version="q4t-serve-v1", description="Static supported surface, not full OpenAI API compatibility. See required native semantics and version-bound acceptance matrix."),
        servers=[dict(url="http://127.0.0.1:8080", description="Example only; use effective configured address")],
        paths=paths, **{"x-q4t": dict(semantic_contract="serve-input-semantics-v1.json",
            query_parameters="route-specific closed fields; unlisted fields return400",
            get_request_body="unsupported;400",
            wrong_method="405 with Allow", post_content_type="application/json; optional charset=utf-8",
            unsupported_media_type=415, builtin_authentication=False,
            native_ingress=dict(optional_external_gateway=True,
                bearer_api_keys=True, tls=True, mtls=True, cors=True,
                backend_identity='native ingress authenticates and replaces internal identity headers'),
            gateway_client_policy=dict(profile="q4t-gateway-clients-v1",
                enabled_by="--client-policy-file", transport="loopback text or explicit media profile",
                external_authentication="TLS gateway independent API keys",
                internal_headers=["X-Q4T-Client-Id", "X-Q4T-Gateway-Token"],
                identity="gateway overwrites internal headers; runner validates configured client and independent shared credential before body admission",
                scoped_resources=["request_id", "cancellation", "session_id", "prefix_cache_scope",
                                  "responses", "conversations", "application_items", "stored_chat", "files", "uploads"],
                limits=["data/control inflight", "body bytes", "explicit file body bytes", "expanded prompt tokens including vision", "total output tokens",
                        "optional v2 fixed-window requests/tokens per minute"],
                refusals=dict(authentication=401, local_admin_with_identity=403,
                    body_limit=413, concurrency_limit=429, token_limit=400,
                    minute_quota=429),
                governance=dict(profile="q4t-client-governance-v1", policy_versions=[1,2,3,4],
                    enabled_by="v2 rates; v3 revisioned administration; v4 explicit file capacity; zero disables rates",
                    window_seconds=60, window="fixed_monotonic", restart="reset",
                    reservation="full prompt plus available output, retained across windows",
                    settlement="slot-admitted full prompt plus logical output, including failure/cancellation",
                    fair_queue="prepared-slot client round robin, per-client bounded queue share",
                    preflight_charged=False, automatic_retry=False),
                defaults="omitted/null output budget is min(server default, client output limit)",
                local_admin="exact GET health/liveness/readiness/config/metrics remain trusted same-host routes; gateway denies them",
                no_guarantee=["per-client physical RAM", "GPU-time or preparation fairness", "distributed quotas", "automatic in-flight key revocation"]),
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
    publish(ROOT / "docs/serve-completion-request-v1.schema.json", completion_schema(), args.check)
    publish(ROOT / "docs/serve-completion-transport-v1.schema.json", completion_transport(), args.check)
    publish(ROOT / "docs/serve-vision-request-v1.schema.json", vision_schema(), args.check)
    publish(ROOT / "docs/serve-message-profile-v3.schema.json", message_schema(), args.check)
    publish(ROOT / "docs/serve-input-semantics-v1.json", semantics(), args.check)
    spec = openapi()
    assert {method.upper()+" "+path for path, operations in spec["paths"].items()
            for method in operations} == set(inventory["routes"])
    publish(ROOT / "docs/serve-openapi-v1.json", spec, args.check)
    from tools.serve_responses.build_contract import standalone_openapi
    publish(ROOT / "docs/serve-openapi-bundle-v1.json", standalone_openapi(spec), args.check)


if __name__ == "__main__":
    main()
