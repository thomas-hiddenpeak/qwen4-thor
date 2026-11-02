"""Bounded reasoning transport, hiding, SDK, and real tool history acceptance."""
import copy
import json
from pathlib import Path
import subprocess
import time

import stage
b = stage.module("b2_live_base", stage.ROOT / "tools/serve_stage_b1/verify.py")
s, wire = b.s, b.wire
ROOT = stage.ROOT


def simple(*, field=None, include=True, stream=False):
    body = b.simple(stream=stream, thinking=True)
    body["extra_body"]["include_reasoning"] = include
    if field is not None:
        body["extra_body"]["reasoning_field"] = field
    return body


def compare(visible, hidden, field):
    wire.require(bool(visible.message.get(field)), "visible reasoning missing")
    expected = copy.deepcopy(visible.message)
    del expected[field]
    wire.require(expected == hidden.message and visible.usage == hidden.usage and
                 visible.finish_reason == hidden.finish_reason == "stop",
                 "hiding changed answer, finish, or token accounting")
    wire.require(hidden.message["content"].strip() == "710003", "answer quality")


def negative_cases():
    user = dict(role="user", content="q")
    base = dict(messages=[user])
    rows = [("field-unknown", dict(base, reasoning_field="unknown"), "reasoning_field"),
        ("field-null", dict(base, reasoning_field=None), "reasoning_field"),
        ("include-number", dict(base, include_reasoning=0), "include_reasoning"),
        ("include-null", dict(base, include_reasoning=None), "include_reasoning"),
        ("raw-hide", dict(prompt="raw", include_reasoning=False), "include_reasoning"),
        ("raw-field", dict(prompt="raw", reasoning_field="reasoning"), "reasoning_field"),
        ("unparsed-hide", dict(base, parse_response=False, include_reasoning=False), "include_reasoning"),
        ("unparsed-field", dict(base, parse_response=False, reasoning_field="reasoning"), "reasoning_field")]
    for label, message, param in [
        ("history-conflict", dict(role="assistant", reasoning="one", reasoning_content="two"), "reasoning"),
        ("history-type", dict(role="assistant", reasoning=0), "reasoning"),
        ("legacy-type", dict(role="assistant", reasoning_content=[]), "reasoning_content"),
        ("history-role", dict(role="user", reasoning=None), "reasoning")]:
        rows.append((label, dict(messages=[user, message]), "messages[1]." + param))
    return rows


class Run(b.Run):
    def budget(self):
        wire.require(time.time() < self.deadline, "stage B2a deadline")
        wire.require(self.requests <= (1 if self.installed else 13) and
                     self.negative <= (0 if self.installed else 12) and
                     self.starts <= (1 if self.installed else 2) and
                     self.capability_gets <= (1 if self.installed else 2),
                     "stage B2a request/start budget")

    def success(self, folder, label, body):
        if label == "project-read":
            body["extra_body"]["reasoning_field"] = "reasoning"
            body["extra_body"]["chat_template_kwargs"]["enable_thinking"] = True
            body["max_completion_tokens"] = 512
        elif label == "project-answer":
            wire.require(any(m.get("reasoning") for m in body["messages"]
                         if m["role"] == "assistant"), "real reasoning history lost")
            body["extra_body"]["chat_template_kwargs"]["enable_thinking"] = False
            body["extra_body"]["include_reasoning"] = False
        result = super().success(folder, label, body)
        if label == "project-read":
            wire.require(bool(result.message.get("reasoning")) and
                         "reasoning_content" not in result.message, "tool reasoning field")
        return result

    def negatives(self, folder):
        before, _ = self.snapshot(folder, "before-negative")
        for index, (label, body, param) in enumerate(negative_cases()):
            self.counted(negative=True)
            body.update(stream=bool(index % 2), max_completion_tokens=1)
            raw = json.dumps(body, ensure_ascii=False).encode()
            status, _, value = self.exchange(folder, label,
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: " +
                str(len(raw)).encode() + b"\r\n\r\n" + raw)
            wire.require(status == 400, label + ": expected pre-stream 400")
            b.t.error_contract(value, "invalid_request", param)
        after, _ = self.snapshot(folder, "after-negative")
        for key in ("q4t_prompt_tokens_total", "q4t_generation_tokens_total",
                    "q4t_structured_workers_started_total"):
            wire.require(before[key] == after[key], "invalid request performed work")
        self.resources_empty(folder, "negative-drained")

    def business(self, folder):
        self.negatives(folder)
        for field in (None, "reasoning"):
            for streaming in (False, True):
                label = (field or "default") + ("-sse" if streaming else "-json")
                visible = self.success(folder, label + "-visible",
                                       simple(field=field, stream=streaming))
                hidden = self.success(folder, label + "-hidden",
                    simple(field=field, stream=streaming, include=False))
                compare(visible, hidden, field or "reasoning_content")
        self.project_loop(folder, strict=True, stream=True)
        body = simple(field="reasoning", include=False, stream=True)
        body["messages"][-1]["content"] = '输出JSON对象，answer必须为字符串710003。'
        body["response_format"] = dict(type="json_schema", json_schema=dict(
            name="answer", strict=True, schema=dict(type="object", properties=dict(
                answer=dict(type="string", enum=["710003"])), required=["answer"],
                additionalProperties=False)))
        body["max_completion_tokens"] = 512
        result = self.success(folder, "hidden-schema", body)
        wire.require(result.finish_reason == "stop" and
            json.loads(result.message["content"]) == dict(answer="710003") and
            not (set(result.message) & {"reasoning", "reasoning_content"}), "hidden schema")
        self.resources_empty(folder, "business-drained")

    def plain(self, folder):
        result = self.success(folder, "hidden-plain", simple(include=False, stream=True))
        wire.require(result.finish_reason == "stop" and
            result.message["content"].strip() == "710003" and
            not (set(result.message) & {"reasoning", "reasoning_content"}), "plain hidden quality")
        self.resources_empty(folder, "plain-drained")

    def health(self, folder):
        result = self.success(folder, "reasoning-installed", simple(field="reasoning"))
        wire.require(result.finish_reason == "stop" and
            result.message["content"].strip() == "710003" and
            bool(result.message.get("reasoning")), "installed reasoning quality")
        self.resources_empty(folder, "installed-drained")

    def run(self):
        state = dict(status="FAILED")
        try:
            binary = ROOT / "build/q4t" if self.installed else Path(self.plan["candidate"])
            if self.installed:
                self.model("candidate-installed", binary, True, self.health)
            else:
                self.model("candidate-mtp", binary, True, self.business)
                self.model("candidate-plain", binary, False, self.plain)
            wire.require(self.requests == (1 if self.installed else 12) and
                         self.negative == (0 if self.installed else 12), "missing frozen cases")
            for script in ("tools/serve_stage_a/check_schema.py",
                           "tools/serve_stage_b2a/check_schema.py"):
                subprocess.run(["/usr/bin/python3", "-B", str(ROOT / script), str(self.out)],
                               cwd=ROOT, check=True, timeout=30)
            state["status"] = "PASS_BOUNDED_SERVE_STAGE_B2A"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(request_attempts=self.requests, negative_requests=self.negative,
                starts=self.starts, seconds=time.monotonic() - self.started,
                performance_acceptance=False, thinking_token_budget_accepted=False)
            wire.save(self.out / "result.json", state)
