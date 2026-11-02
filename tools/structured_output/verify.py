"""Bounded real SDK/HTTP checks for constrained output, under one GPU lease."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "application_wire_checks", ROOT / "tools/application_service/verify.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
wire = app.wire
from chat_client import append_tool_results, prepare_tool_calls
from project_records import ProjectRecords, TOOLS, VALIDATORS


def schema(properties):
    return dict(type="object", properties=properties,
                required=list(properties), additionalProperties=False)


ANSWER = schema(dict(answer=dict(type="integer"),
                     label=dict(type="string", enum=["中文"])))
READ_TOOLS = copy.deepcopy(TOOLS[:1])
READ_TOOLS[0]["function"]["strict"] = True
READ_TOOLS[0]["function"]["parameters"]["properties"]["path"]["enum"] = [
    "docs/MODEL.md"]
NOOP = dict(type="function", function=dict(name="noop", strict=True,
                                         parameters=schema({})))
READ_PROMPT = "调用read_project_record读取docs/MODEL.md第1行，line_count=8。"


def request(prompt, *, stream=False, thinking=False, **fields):
    value = dict(model="qwen3.8-flash-next", temperature=0,
        max_completion_tokens=512, stream=stream,
        messages=[dict(role="user", content=prompt)],
        extra_body=dict(chat_template_kwargs=dict(enable_thinking=thinking,
                                                  reasoning_effort="low")))
    if stream:
        value["stream_options"] = dict(include_usage=True)
    value.update(fields)
    return value


def response_schema():
    return dict(type="json_schema", json_schema=dict(
        name="answer", strict=True, schema=copy.deepcopy(ANSWER)))


def error_terminal(status, raw, streaming):
    """A failed constrained response must expose no executable partial payload."""
    if not streaming:
        wire.require(status == 502, "expected structured HTTP 502")
        value = json.loads(raw)
        wire.require(set(value) == {"error"}, "partial success in JSON error")
        events = [value]
    else:
        wire.require(status == 200, "expected committed SSE HTTP status")
        data = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
        wire.require(data and data[-1] == "[DONE]" and
                     data.count("[DONE]") == 1, "invalid SSE error terminal")
        events = [json.loads(item) for item in data[:-1]]
        wire.require(events and "error" in events[-1], "error must end SSE")
    errors = []
    for event in events:
        if "error" in event:
            errors.append(event["error"])
        wire.require(event.get("usage") is None, "usage published on error")
        for choice in event.get("choices", []):
            wire.require(choice.get("finish_reason") is None,
                         "successful finish published on error")
            delta = choice.get("delta", {})
            wire.require(set(delta) <= {"role", "content"} and
                         not delta.get("content"), "partial payload escaped")
    wire.require(len(errors) == 1 and errors[0]["code"] ==
                 "structured_output_incomplete", "wrong structured error")


class Run(app.Run):
    def __init__(self, plan, output, *, installed=False):
        self.plan, self.installed = plan, installed
        self.deadline = plan["started"] + 1800 - 30
        wire.Run.__init__(self, SimpleNamespace(output=Path(output), port=18099,
            model_dir=Path(plan["model_dir"]),
            protocol_sha256=plan["protocol_sha256"]))
        self.out.mkdir(mode=0o700)

    def budget(self):
        wire.require(time.time() < self.deadline, "structured stage deadline")
        wire.require(self.requests <= (2 if self.installed else 16) and
                     self.negative <= (0 if self.installed else 12) and
                     self.starts <= (1 if self.installed else 2) and
                     self.capability_gets <= (1 if self.installed else 2),
                     "structured stage request/start budget")

    def success(self, folder, label, body):
        return self.sdk_chat(self.sdk(), folder, label, body)

    @staticmethod
    def call(result, name="read_project_record", expected=None):
        calls = result.message.get("tool_calls", [])
        wire.require(result.finish_reason == "tool_calls" and len(calls) == 1
                     and calls[0]["function"]["name"] == name,
                     "wrong tool choice/count/finish")
        args = json.loads(calls[0]["function"]["arguments"])
        if expected is not None:
            wire.require(args == expected, "wrong tool argument values")
        return args

    def low_budget(self, folder, stream):
        self.counted()
        label = "truncated-sse" if stream else "truncated-json"
        body = request("计算17乘19，answer为乘积，label为中文。", stream=stream,
                       max_completion_tokens=1, response_format=response_schema())
        body.update(body.pop("extra_body"))
        wire.save(folder / (label + "-request.json"), body)
        start = time.monotonic()
        code, raw = self.request("POST", "/v1/chat/completions", body)
        wire.save(folder / (label + "-response.json"), dict(status=code, raw=raw,
            seconds=time.monotonic() - start))
        error_terminal(code, raw, stream)
        self.wait_slot(label)

    def negatives(self, folder):
        base = request(READ_PROMPT, tools=READ_TOOLS, tool_choice="required")
        cases = {}
        def case(name, **fields):
            item = copy.deepcopy(base)
            item.update(fields)
            cases[name] = item
            return item
        case("unknown-name", tool_choice=dict(type="function",
             function=dict(name="missing")))
        case("required-empty", tools=[])
        case("parallel-type", parallel_tool_calls="false")
        case("raw-parse", extra_body=dict(parse_response=False))
        case("schema-plus-tools", response_format=response_schema())
        case("session", extra_body=dict(session_id="not-created"))
        case("prefix", extra_body=dict(prefix_cache_scope="public"))
        case("media", messages=[dict(role="user", content=[dict(type="image_url",
             image_url=dict(url="http://127.0.0.1:1/do-not-fetch"))])])
        unknown = case("unknown-schema-key")
        unknown["tools"][0]["function"]["parameters"]["properties"]["path"][
            "pattern"] = ".*"
        missing = case("strict-missing-required")
        missing["tools"][0]["function"]["parameters"]["required"] = ["path"]
        case("duplicate-tools", tools=READ_TOOLS + READ_TOOLS)
        invalid = case("schema-root-array", tools=[], tool_choice="none",
                       response_format=response_schema())
        invalid["response_format"]["json_schema"]["schema"] = dict(
            type="array", items=dict(type="string"))
        for label, body in cases.items():
            self.sdk_chat(self.sdk(), folder, label, body, error_status=400)
            self.wait_slot(label)

    def plain(self, folder):
        expected = dict(path="docs/MODEL.md", start_line=1, line_count=8)
        none = self.success(folder, "none", request("只回复数字710003。",
            tools=READ_TOOLS, tool_choice="none"))
        wire.require(not none.message.get("tool_calls") and
                     none.message["content"].strip() == "710003", "none quality")
        self.call(self.success(folder, "required-sse", request(READ_PROMPT,
            stream=True, tools=READ_TOOLS, tool_choice="required")), expected=expected)
        self.call(self.success(folder, "named", request(READ_PROMPT,
            tools=READ_TOOLS + [NOOP], tool_choice=dict(type="function",
            function=dict(name="read_project_record")))), expected=expected)
        self.call(self.success(folder, "auto-single", request(READ_PROMPT +
            "然后再调用noop；如果只能调用一个，先读取。", tools=READ_TOOLS + [NOOP],
            tool_choice="auto", parallel_tool_calls=False)), expected=expected)
        record_schema = schema(dict(flags=dict(type="array", items=dict(type="boolean"),
            minItems=2, maxItems=2), meta=schema(dict(label=dict(type="string",
            enum=["中文"]), score=dict(type="integer"))), note=dict(type="null")))
        record_args = dict(flags=[True, False], meta=dict(label="中文", score=3), note=None)
        record = dict(type="function", function=dict(name="record", strict=True,
                                                    parameters=record_schema))
        self.call(self.success(folder, "nested-strict", request(
            '调用record，参数为' + json.dumps(record_args, ensure_ascii=False),
            tools=[record], tool_choice="required")), "record", record_args)
        obj = self.success(folder, "json-object", request(
            '只输出JSON对象，answer为17乘19的乘积。', response_format=dict(type="json_object")))
        wire.require(json.loads(obj.message["content"]) == {"answer": 323},
                     "json_object quality")
        for label, stream, thinking in [("schema-json", False, False),
                ("schema-sse", True, False), ("schema-thinking", False, True)]:
            result = self.success(folder, label, request(
                "计算17乘19，answer为乘积，label为中文。", stream=stream,
                thinking=thinking, response_format=response_schema()))
            wire.require(json.loads(result.message["content"]) ==
                         {"answer": 323, "label": "中文"}, label + " quality")
            if thinking:
                wire.require(bool(result.message.get("reasoning_content")),
                             "reasoning was not separated")
        self.low_budget(folder, False)
        self.low_budget(folder, True)
        self.project_loop(folder)
        self.negatives(folder)

    def project_loop(self, folder):
        body = request(READ_PROMPT + "根据读取内容告诉我模型架构名称，并逐字引用工具给出的citation。",
                       stream=True, tools=READ_TOOLS, tool_choice="required",
                       parallel_tool_calls=False)
        result = self.success(folder, "project-read", body)
        self.call(result, expected=dict(path="docs/MODEL.md", start_line=1, line_count=8))
        calls = prepare_tool_calls(result, VALIDATORS)
        wire.require(len(calls) == 1, "project tool execution count")
        data = ProjectRecords(ROOT).execute(calls[0])
        wire.require(data.get("ok") is True and "Qwen" in data["source"]["text"],
                     "real project source unavailable")
        wire.save(folder / "project-tool-result.json", data)
        body["messages"] = append_tool_results(body["messages"], result,
            {calls[0].call_id: json.dumps(data, ensure_ascii=False)})
        body["tool_choice"] = "none"
        answer = self.success(folder, "project-answer", body)
        text = answer.message["content"]
        wire.require(answer.finish_reason == "stop" and
                     data["source"]["citation"] in text and "Qwen" in text,
                     "project final answer/citation missing")

    def mtp(self, folder):
        ordinary = self.success(folder, "ordinary-auto", request("只回复数字710003。"))
        wire.require(ordinary.message["content"].strip() == "710003", "MTP quality")
        tool = request(READ_PROMPT, tools=copy.deepcopy(READ_TOOLS), tool_choice="auto")
        tool["tools"][0]["function"].pop("strict")
        self.call(self.success(folder, "legacy-auto-tool", tool), expected=dict(
            path="docs/MODEL.md", start_line=1, line_count=8))
        self.call(self.success(folder, "constrained-in-mtp-server", request(
            READ_PROMPT, stream=True, tools=READ_TOOLS, tool_choice="required")),
            expected=dict(path="docs/MODEL.md", start_line=1, line_count=8))

    def health(self, folder):
        ordinary = self.success(folder, "ordinary", request("只回复数字710003。"))
        wire.require(ordinary.message["content"].strip() == "710003", "installed MTP quality")
        result = self.success(folder, "schema", request(
            "计算17乘19，answer为乘积，label为中文。", stream=True,
            response_format=response_schema()))
        wire.require(json.loads(result.message["content"]) ==
                     {"answer": 323, "label": "中文"}, "installed schema quality")

    def run(self):
        state = dict(status="FAILED")
        try:
            binary = ROOT / "build/q4t" if self.installed else Path(self.plan["candidate"])
            if self.installed:
                self.model("candidate-installed", binary, True, self.health)
            else:
                self.model("candidate-plain", binary, False, self.plain)
                self.model("candidate-mtp", binary, True, self.mtp)
            wire.require(self.requests == (2 if self.installed else 16) and
                         self.negative == (0 if self.installed else 12), "missing cases")
            state["status"] = "PASS_BOUNDED_STRUCTURED_OUTPUT"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(requests=self.requests, negatives=self.negative,
                starts=self.starts, seconds=time.monotonic() - self.started,
                performance_acceptance=False)
            wire.save(self.out / "result.json", state)
