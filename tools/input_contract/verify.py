"""Bounded real SDK input contracts and two project-record tool roundtrips."""
import copy
import http.client
import importlib.util
import io
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "input_structured_wire", ROOT / "tools/structured_output/verify.py")
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
wire = s.wire
NULLS = dict(temperature=None, top_p=None, n=None, frequency_penalty=None,
             presence_penalty=None, stream=None, stream_options=None, user=None)


def negative_cases():
    tools = copy.deepcopy(s.READ_TOOLS)
    tools[0]["function"]["strict"] = None
    base = s.request(s.READ_PROMPT, tools=tools)
    base.update(base.pop("extra_body"))
    cases = []

    def add(label, patch=None):
        body = copy.deepcopy(base)
        if patch:
            body.update(patch)
        cases.append((label, body))
        return body

    add("auto-outer-strict")["tools"][0]["strict"] = True
    add("raw-function-typo", dict(parse_response=False))["tools"][0][
        "function"]["paramaters"] = {}
    add("none-unknown-field", dict(tool_choice="none"))["tools"][0]["extra"] = 1
    add("required-function-typo", dict(tool_choice="required"))["tools"][0][
        "function"]["extra"] = 1
    add("parameters-null")["tools"][0]["function"]["parameters"] = None
    add("strict-wrong-type")["tools"][0]["function"]["strict"] = "true"
    history = [dict(role="user", content="q"), dict(role="assistant", content=None,
        tool_calls=[dict(id="a", type="function", function=dict(
            name="read_project_record", arguments="{}"))]),
        dict(role="tool", tool_call_id="a", content="result")]
    for label in ("history-missing-arguments", "history-typo-arguments",
                  "history-missing-result", "legacy-custom", "legacy-extra"):
        messages = copy.deepcopy(history)
        call = messages[1]["tool_calls"][0]
        if label == "history-missing-arguments":
            del call["function"]["arguments"]
        elif label == "history-typo-arguments":
            call["function"]["argumnts"] = call["function"].pop("arguments")
        elif label == "history-missing-result":
            del messages[2]["content"]
        else:
            del call["id"]
            del messages[2]["tool_call_id"]
            if label == "legacy-custom":
                call["type"] = "custom"
            else:
                call["extra"] = 1
        add(label, dict(messages=messages))
    add("nonneutral-temperature", dict(temperature=0.5))
    result = [(name, json.dumps(body, ensure_ascii=False)) for name, body in cases]
    for name, field, number in [
            ("fractional-budget", "max_completion_tokens", "1.0000000000000001"),
            ("fractional-seed", "seed", "9007199254740991.1"),
            ("fractional-timeout", "request_timeout_ms", "1.0000000000000001")]:
        body = copy.deepcopy(base)
        body[field] = "EXACT_NUMBER"
        result.append((name, json.dumps(body).replace('"EXACT_NUMBER"', number)))
    body = s.request("q", response_format=s.response_schema())
    body.update(body.pop("extra_body"))
    body["response_format"]["json_schema"]["schema"]["properties"]["answer"] = dict(
        type="array", items=dict(type="integer"), maxItems="EXACT_NUMBER")
    result.append(("fractional-array-bound", json.dumps(body).replace(
        '"EXACT_NUMBER"', "1.0000000000000001")))
    return result


class Run(s.Run):
    def budget(self):
        wire.require(time.time() < self.deadline, "input contract deadline")
        wire.require(self.requests <= (2 if self.installed else 6) and
                     self.negative <= (0 if self.installed else 19) and
                     self.starts <= (1 if self.installed else 2) and
                     self.capability_gets <= (1 if self.installed else 2),
                     "input contract request/start budget")

    def success(self, folder, label, body):
        with self.sdk() as client:
            return self.sdk_chat(client, folder, label, body)

    def raw(self, folder, label, text, *, path="/v1/chat/completions",
            status=400, code="invalid_request"):
        self.counted(negative=status != 200)
        (folder / (label + "-request.bin")).write_bytes(text.encode())
        conn = http.client.HTTPConnection("127.0.0.1", self.args.port, timeout=120)
        start = time.monotonic()
        try:
            conn.request("POST", path, text.encode(),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read()
            (folder / (label + "-wire.bin")).write_bytes(raw)
            wire.save(folder / (label + "-response.json"), dict(
                status=response.status, seconds=time.monotonic() - start))
            wire.require(response.status == status, label + ": wrong HTTP status")
            if status != 200:
                error = json.loads(raw)
                wire.require(set(error) == {"error"} and
                             set(error["error"]) == {"message", "type", "code", "param"}
                             and error["error"]["code"] == code, label + ": error contract")
            else:
                return wire.read_json(io.BytesIO(raw))
        finally:
            conn.close()
            self.wait_slot(label)

    def negatives(self, folder):
        before = self.request("GET", "/metrics")
        for label, body in negative_cases():
            self.raw(folder, label, body)
        token = "a" * 64
        for label, body, status, code in [
            ("cancel-extra", dict(request_id="unknown", cancel_token=token,
                                  dry_run=True), 400, "invalid_cancellation"),
            ("cancel-missing-id", dict(cancel_token=token), 400, "invalid_cancellation"),
            ("cancel-unknown", dict(request_id="unknown", cancel_token=token),
             404, "request_not_found")]:
            self.raw(folder, label, json.dumps(body), path="/v1/requests/cancel",
                     status=status, code=code)
        after = self.request("GET", "/metrics")
        wire.save(folder / "negative-metrics.json", dict(before=before, after=after))
        def tokens(snapshot):
            return [line for line in snapshot[1].splitlines() if line.startswith(
                ("q4t_prompt_tokens_total ", "q4t_generation_tokens_total "))]
        wire.require(before[0] == after[0] == 200 and len(tokens(before)) == 2 and
                     tokens(before) == tokens(after), "negative request generated tokens")

    def project_loop(self, folder, *, strict, stream):
        tools = copy.deepcopy(s.READ_TOOLS)
        tools[0]["function"]["strict"] = True if strict else None
        body = s.request(s.READ_PROMPT +
            "根据读取内容告诉我模型架构名称，并逐字引用工具给出的citation。",
            stream=stream, tools=tools, tool_choice="required" if strict else "auto")
        if strict:
            body["parallel_tool_calls"] = False
        else:
            body.update(NULLS)
        result = self.success(folder, "project-read", body)
        self.call(result, expected=dict(path="docs/MODEL.md", start_line=1, line_count=8))
        calls = s.prepare_tool_calls(result, s.VALIDATORS)
        wire.require(len(calls) == 1, "tool execution count")
        data = s.ProjectRecords(ROOT).execute(calls[0])
        wire.require(data.get("ok") is True and "Qwen3.8" in data["source"]["text"],
                     "real project source unavailable")
        wire.save(folder / "project-tool-result.json", data)
        body["messages"] = s.append_tool_results(body["messages"], result,
            {calls[0].call_id: json.dumps(data, ensure_ascii=False)})
        if strict:
            body["tool_choice"] = "none"
        answer = self.success(folder, "project-answer", body)
        wire.require(answer.finish_reason == "stop" and
                     not answer.message.get("tool_calls") and
                     "Qwen3.8" in answer.message["content"] and
                     data["source"]["citation"] in answer.message["content"],
                     "project answer/citation missing")

    def plain(self, folder):
        self.project_loop(folder, strict=True, stream=True)
        body = s.request("只回复数字710003。", **NULLS)
        result = self.success(folder, "sdk-null-defaults", body)
        wire.require(result.message["content"].strip() == "710003", "null semantics")
        body = s.request("只回复数字710003。")
        body.update(body.pop("extra_body"))
        body["max_completion_tokens"] = "EXACT_NUMBER"
        result = self.raw(folder, "exact-exponent-integer", json.dumps(body).replace(
            '"EXACT_NUMBER"', "640e-1"), status=200)
        wire.require(result.finish_reason == "stop" and
                     result.message["content"].strip() == "710003", "exact integer semantics")
        self.negatives(folder)

    def mtp(self, folder):
        self.project_loop(folder, strict=False, stream=False)

    def run(self):
        state = dict(status="FAILED")
        try:
            binary = ROOT / "build/q4t" if self.installed else Path(self.plan["candidate"])
            if self.installed:
                self.model("candidate-installed", binary, True, self.health)
            else:
                self.model("candidate-plain", binary, False, self.plain)
                self.model("candidate-mtp", binary, True, self.mtp)
            wire.require(self.requests == (2 if self.installed else 6) and
                         self.negative == (0 if self.installed else 19), "missing cases")
            state["status"] = "PASS_BOUNDED_INPUT_CONTRACT"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(requests=self.requests, negatives=self.negative,
                starts=self.starts, seconds=time.monotonic() - self.started,
                performance_acceptance=False)
            wire.save(self.out / "result.json", state)
