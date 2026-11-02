"""Frozen text-stop wire, SDK, MTP, and real project query acceptance."""
import copy
import io
import json
from pathlib import Path
import re
import subprocess
import time
from unittest.mock import patch

import stage

transport = stage.module("stop_budget_transport", stage.ROOT / "tools/serve_thinking_budget/verify.py")
b, s, wire, ROOT = transport.b, transport.s, transport.wire, stage.ROOT
TEXT = "ALPHA0123456789ENDOMEGA"
STOP = "456789END"


def copying(text=TEXT, stop=STOP, *, stream=False, maximum=128):
    body = s.request("严格逐字复制下一行，不加解释或引号：\n" + text,
                     stream=stream, max_completion_tokens=maximum)
    if stop is not None:
        body["stop"] = stop
    return body


def negative_cases():
    rows = []
    for value, param in (("", "stop"), (["a", ""], "stop[1]"), (True, "stop"),
        (1, "stop"), ({}, "stop"), (["a", None], "stop[1]"), (["a"] * 5, "stop"),
        ("x" * 257, "stop"), ("中" * 86, "stop")):
        rows.append((dict(stop=value), param))
    tools = copy.deepcopy(s.READ_TOOLS)
    tools[0]["function"]["strict"] = None
    rows += [(dict(stop="x", tools=s.READ_TOOLS), "stop"),
        (dict(stop="x", tools=tools, tool_choice="required"), "stop"),
        (dict(stop="x", tools=tools, parallel_tool_calls=False), "stop"),
        (dict(stop="x", response_format=dict(type="json_object")), "stop"),
        (dict(stop="x", session_id="a" * 64), "stop"),
        (dict(stop="x", prefix_cache_scope="a" * 64), "stop"),
        (dict(stop="x", tools=tools, tool_choice=dict(type="function",
            function=dict(name="read_project_record"))), "stop")]
    return rows


def check_text(result, wanted, reason="stop"):
    wire.require(result.message["content"].strip() == wanted and
                 not result.message.get("tool_calls") and result.finish_reason == reason,
                 "text/finish mismatch: " + repr(result.message))


def truncated_tool(status, raw, streaming):
    if streaming:
        wire.require(status == 200, "SSE headers not committed")
        values = b.t.events(raw)
        wire.require("error" in values[-1], "SSE error not last")
    else:
        wire.require(status == 502, "tool truncation must fail HTTP")
        values = [json.loads(raw)]
    errors = [v["error"] for v in values if "error" in v]
    wire.require(len(errors) == 1 and errors[0]["code"] == "response_parse_failed",
                 "wrong tool truncation code")
    for value in values:
        wire.require(value.get("usage") is None, "usage escaped on error")
        for choice in value.get("choices", []):
            wire.require(choice.get("finish_reason") is None and
                         not choice.get("delta", {}).get("tool_calls"),
                         "executable partial tool escaped")


def stop_traces(log):
    labels = {}
    for match in re.finditer(r"\[q4t\]\[http\] trace_id=trace-(\S+) completion_id=(\S+)", log):
        wire.require(match[2] not in labels, "duplicate completion")
        labels[match[2]] = match[1]
    rows = {}
    for match in re.finditer(r"\[text-stop\] request=(\S+) matched_index=(-?\d+) tokens=(\d+) "
            r"mtp_steps=(\d+) mtp_tokens=(\d+) mtp_discarded=(\d+)", log):
        wire.require(match[1] in labels and labels[match[1]] not in rows, "trace identity")
        rows[labels[match[1]]] = dict(zip(
            ("matched_index", "tokens", "mtp_steps", "mtp_tokens", "mtp_discarded"),
            map(int, match.groups()[1:])))
    return rows


class Run(b.Run):
    sdk_chat = transport.Run.sdk_chat  # Captures raw wire even on SDK failure.

    def __init__(self, plan, output, *, installed=False):
        super().__init__(plan, output, installed=installed)
        self.deadline = plan.get("original_started", plan["started"]) + 1800 - 30

    def budget(self):
        wire.require(time.time() < self.deadline, "text stop deadline")
        wire.require(self.requests <= (1 if self.installed else 21) and
                     self.negative <= (0 if self.installed else 16) and
                     self.starts <= (1 if self.installed else 3) and
                     self.capability_gets <= (1 if self.installed else 3), "text stop budget")

    def success(self, folder, label, body):
        if label in ("project-read", "project-answer"):
            body["stop"] = "NEVER_STOP_24c3"
            body.update(stream=True, stream_options=dict(include_usage=True))
        return super().success(folder, label, body)

    def raw(self, folder, label, body):
        self.counted()
        body = copy.deepcopy(body)
        body.update(body.pop("extra_body", {}))
        data = json.dumps(body, ensure_ascii=False).encode()
        wire.save(folder / (label + "-request.json"), body)
        result = self.exchange(folder, label,
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
            b"X-Request-Id: trace-" + label.encode() +
            b"\r\nContent-Type: application/json\r\nContent-Length: " +
            str(len(data)).encode() + b"\r\n\r\n" + data)
        self.wait_slot(label)
        return result

    def negatives(self, folder):
        before, _ = self.snapshot(folder, "before-negative")
        for i, (fields, param) in enumerate(negative_cases()):
            self.counted(negative=True)
            body = dict(messages=[dict(role="user", content="q")],
                        stream=bool(i % 2), max_completion_tokens=1, **fields)
            data = json.dumps(body, ensure_ascii=False).encode()
            status, _, raw = self.exchange(folder, "negative-" + str(i),
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: " +
                str(len(data)).encode() + b"\r\n\r\n" + data)
            wire.require(status == 400, "negative must precede stream")
            b.t.error_contract(raw, "invalid_request", param)
        after, _ = self.snapshot(folder, "after-negative")
        for key in ("q4t_prompt_tokens_total", "q4t_generation_tokens_total",
                    "q4t_structured_workers_started_total"):
            wire.require(before[key] == after[key], "invalid stop performed work")
        self.resources_empty(folder, "negative-drained")

    def business(self, folder):
        self.negatives(folder)
        baseline = self.success(folder, "baseline", copying(stop=None))
        check_text(baseline, TEXT)
        at_eos = self.success(folder, "eos-at-limit", copying(stop=None,
            maximum=baseline.usage["completion_tokens"]))
        b.compare(baseline, at_eos)
        plain = self.success(folder, "ascii-json", copying())
        streamed = self.success(folder, "ascii-sse", copying(stream=True))
        check_text(plain, "ALPHA0123")
        b.compare(plain, streamed)
        wire.require(plain.usage["completion_tokens"] < baseline.usage["completion_tokens"],
                     "stop did not shorten logical generation")
        check_text(self.success(folder, "chinese", copying("开始中文停止之后", "中文停止",
            stream=True)), "开始")
        check_text(self.success(folder, "overlap", copying("ALPHAababaOMEGA",
            ["ababa", "bab"], stream=True)), "ALPHAa")
        check_text(self.success(folder, "unmatched-prefix", copying("ALPHAEN", "END")), "ALPHAEN")
        for label, stop, expected, finish in (("length-match", "710", "", "stop"),
                ("length-miss", "710X", "710", "length")):
            body = b.simple(stream=True)
            body.update(stop=stop, max_completion_tokens=1)
            result = self.success(folder, label, body)
            check_text(result, expected, finish)
            wire.require(result.usage["completion_tokens"] == 1, "length token count")
        prompt = "<|im_start|>user\n严格逐字复制下一行，不加解释或引号：\n" + TEXT + \
                 "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        status, _, raw = self.raw(folder, "raw-prompt", dict(prompt=prompt,
            stop=STOP, max_completion_tokens=128))
        wire.require(status == 200, "raw prompt status")
        check_text(wire.read_json(io.BytesIO(raw)), "ALPHA0123")
        results = []
        for label, visible in (("reasoning", True), ("reasoning-hidden", False)):
            body = b.simple(stream=not visible, thinking=True)
            body["stop"] = "</think>"
            body["extra_body"]["include_reasoning"] = visible
            result = self.success(folder, label, body)
            check_text(result, "")
            wire.require(bool(result.message.get("reasoning_content")) == visible,
                         "stopped reasoning visibility")
            results.append(result)
        wire.require(results[0].usage == results[1].usage, "hidden stop changed usage")
        body = transport.simple(0, stream=True)
        body["stop"] = "\n</think>\n\n"
        result = self.success(folder, "budget-stop", body)
        check_text(result, "")
        transport.accounting(result.usage, 0)
        wire.require(result.usage["q4t_thinking_budget"]["forced_closure_tokens"] == 3,
                     "native boundary token count")
        tools = copy.deepcopy(s.READ_TOOLS)
        tools[0]["function"]["strict"] = None
        for streaming in (False, True):
            label = "tool-truncated-sse" if streaming else "tool-truncated-json"
            body = s.request(s.READ_PROMPT, tools=tools, tool_choice="auto",
                             stop="</parameter>", stream=streaming)
            status, _, raw = self.raw(folder, label, body)
            truncated_tool(status, raw, streaming)
        self.project_loop(folder, strict=False, stream=True)
        body = transport.schema_body(1)
        body["stop"] = []
        result = self.success(folder, "strict-disabled-stop", body)
        wire.require(json.loads(result.message["content"]) == dict(answer="710003"),
                     "disabled stop changed strict output")
        self.resources_empty(folder, "business-drained")
        self.traces(folder, mtp=True)

    def plain(self, folder):
        first = self.success(folder, "plain-json", copying())
        second = self.success(folder, "plain-sse", copying(stream=True))
        check_text(first, "ALPHA0123")
        b.compare(first, second)
        prior = json.loads((self.out / "candidate-mtp/ascii-json-result.json").read_text())
        wire.require(first.message == prior["message"] and first.usage == prior["usage"],
                     "MTP/plain stopped response differs")
        self.resources_empty(folder, "plain-drained")
        self.traces(folder, mtp=False)

    def health(self, folder):
        check_text(self.success(folder, "installed-stop", copying(stream=True)), "ALPHA0123")
        self.resources_empty(folder, "installed-drained")
        self.traces(folder, mtp=True)

    def traces(self, folder, *, mtp):
        rows = stop_traces((folder / "server.log").read_text())
        wire.require(rows, "missing stop trace")
        for label, row in rows.items():
            path = folder / (label + "-result.json")
            if path.exists():
                result = json.loads(path.read_text())
                wire.require(row["tokens"] == result["usage"]["completion_tokens"],
                             "stop usage/trace mismatch")
        if mtp:
            wire.require(any(row["mtp_steps"] > 0 for row in rows.values()), "no MTP step")
            if not self.installed:
                wire.require(any(row["mtp_discarded"] > 0 for row in rows.values()),
                             "no actual MTP suffix discard exercised")
                wire.require(len(rows) == 15 and rows["overlap"]["matched_index"] == 1,
                             "incomplete stop cases or overlap order")
        else:
            wire.require(len(rows) == 2 and all(row["mtp_steps"] == 0 for row in rows.values()),
                         "plain ran MTP")
        wire.save(folder / "stop-trace-result.json", dict(passed=True, requests=rows))

    def model(self, name, binary, mtp, action):
        def command(binary, model, port, mtp):
            argv = b.command_original(binary, model, port, mtp)
            preflight = self.plan["startup_preflight"]
            wire.require(wire.digest(Path(preflight["path"])) == preflight["sha256"],
                         "preflight identity changed")
            check = subprocess.run([preflight["path"], *argv[2:]], cwd=ROOT,
                                   capture_output=True, text=True, timeout=20)
            wire.save(self.out / name / "startup-preflight.json", dict(argv=argv,
                returncode=check.returncode, stdout=check.stdout, stderr=check.stderr))
            wire.require(check.returncode == 0, "startup preflight failed")
            return argv
        with patch.object(b, "command", command):
            super().model(name, binary, mtp, action)

    def run(self):
        state = dict(status="FAILED")
        try:
            binary = ROOT / "build/q4t" if self.installed else Path(self.plan["candidate"])
            if self.installed:
                self.model("candidate-installed", binary, True, self.health)
            else:
                self.model("candidate-mtp", binary, True, self.business)
                self.model("candidate-plain", binary, False, self.plain)
            wire.require(self.requests == (1 if self.installed else 20) and
                         self.negative == (0 if self.installed else 16), "missing frozen cases")
            for script in ("tools/serve_stage_a/check_schema.py",
                           "tools/serve_stage_b2a/check_schema.py",
                           "tools/serve_thinking_budget/check_schema.py",
                           "tools/serve_text_stop/check_schema.py"):
                subprocess.run(["/usr/bin/python3", "-B", str(ROOT / script), str(self.out)],
                               cwd=ROOT, check=True, timeout=30)
            state["status"] = "PASS_BOUNDED_TEXT_STOP"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(request_attempts=self.requests, negative_requests=self.negative,
                starts=self.starts, seconds=time.monotonic() - self.started,
                performance_acceptance=False)
            wire.save(self.out / "result.json", state)
