"""Bounded real-token budget, grammar, MTP, and private-cache acceptance."""
import copy
from dataclasses import asdict
import io
import json
from pathlib import Path
import re
import subprocess
import time
from unittest.mock import patch

import stage
b = stage.module("thinking_live_base", stage.ROOT / "tools/serve_stage_b1/verify.py")
s, wire, ROOT = b.s, b.wire, stage.ROOT
from chat_client import _usage


def simple(limit=0, *, stream=False, include=True, thinking=True):
    body = b.simple(stream=stream, thinking=thinking)
    body["extra_body"].update(thinking_token_budget=limit,
        reasoning_field="reasoning", include_reasoning=include)
    return body


def schema_body(limit=1, *, maximum=512):
    body = simple(limit, stream=True, include=False)
    body["messages"][-1]["content"] = '输出JSON对象，answer必须为字符串710003。'
    body["response_format"] = dict(type="json_schema", json_schema=dict(
        name="answer", strict=True, schema=dict(type="object", properties=dict(
            answer=dict(type="string", enum=["710003"])), required=["answer"],
            additionalProperties=False)))
    body["max_completion_tokens"] = maximum
    return body


def cache_seed():
    body = simple(8)
    body["messages"][-1]["content"] = ("以下是无关占位记录，忽略其内容。\n" +
        "记录仅供占位。\n" * 1400 + "\n现在只回复数字710003。")
    body["extra_body"].update(session_id="c" * 64)
    body["extra_body"]["chat_template_kwargs"]["preserve_thinking"] = True
    return body


def command(binary, model, port, mtp, cache=None):
    args = b.command_original(binary, model, port, mtp)
    if cache is not None:
        args[args.index("--max-len") + 1] = "16384"
        args += ["--seq-capacities", "16384", "--session-cache-dir", str(cache)]
    return args


b.command_original = b.command


def prepare_cache(directory):
    # The acceptance owns a fresh private directory, never an existing pool.
    directory.mkdir(mode=0o700)
    wire.require(directory.stat().st_mode & 0o777 == 0o700, "private cache permissions")


def negative_cases():
    # Preserve lexical fractional values: Python float rounding is not an oracle.
    values = ("-1", "2147483648", "true", '"8"', "1.5",
              "1.0000000000000001", "[]", "{}", "0", "0", "1e-1",
              "-0.000000000000000000000000000001")
    rows = []
    for index, value in enumerate(values):
        body = dict(messages=[dict(role="user", content="q")],
                    stream=bool(index % 2), max_completion_tokens=1)
        if index == 8:
            body = dict(prompt="raw", stream=False, max_completion_tokens=1)
        if index == 9:
            body["parse_response"] = False
        raw = json.dumps(body)[:-1] + ',"thinking_token_budget":' + value + '}'
        rows.append(("negative-" + str(index), raw.encode()))
    return rows


def accounting(usage, limit):
    if limit is None:
        wire.require("q4t_thinking_budget" not in usage and
                     "completion_tokens_details" not in usage, "disabled usage drift")
        return
    _usage(usage)
    budget = usage.get("q4t_thinking_budget", {})
    wire.require(budget.get("limit") == limit and
        usage.get("completion_tokens_details", {}).get("reasoning_tokens") ==
        budget.get("consumed", -1) + budget.get("forced_closure_tokens", -1),
        "budget usage missing or inconsistent")


def quality(result):
    wire.require(result.finish_reason == "stop" and
                 result.message["content"].strip() == "710003", "answer quality")


class Run(b.Run):
    def __init__(self, plan, output, *, installed=False):
        super().__init__(plan, output, installed=installed)
        self.deadline = plan.get("original_started", plan["started"]) + \
            plan["limits"]["seconds"] - 30

    def budget(self):
        wire.require(time.time() < self.deadline, "thinking budget deadline")
        plan = getattr(self, "plan", {})
        previous = plan.get("prior_counts", {})
        limits = plan.get("limits", dict(generations=22, negatives=12, starts=4))
        generations = limits["generations"] - 1 - previous.get("generations", 0)
        negatives = limits["negatives"] - previous.get("negatives", 0)
        starts = limits["starts"] - 1 - previous.get("starts", 0)
        wire.require(self.requests <= (1 if self.installed else generations) and
                     self.negative <= (0 if self.installed else negatives) and
                     self.starts <= (1 if self.installed else starts) and
                     self.capability_gets <= (1 if self.installed else starts),
                     "thinking request/start budget")

    def sdk_chat(self, client, folder, label, request, **unused):
        self.counted()
        request = dict(request, extra_headers={"X-Request-Id": "trace-" + label})
        wire.save(folder / (label + "-request.json"), request)
        begin, raw, headers, sdk = time.monotonic(), bytearray(), {}, None
        streaming = request.get("stream", False)
        try:
            if streaming:
                with client.chat.completions.create(**request) as stream:
                    headers = dict(stream.response.headers)
                    original = stream.response.iter_bytes
                    def capture(*args, **kwargs):
                        for part in original(*args, **kwargs):
                            raw.extend(part)
                            yield part
                    with patch.object(stream.response, "iter_bytes", capture):
                        sdk = [chunk.model_dump(mode="json") for chunk in stream]
                values = b.t.events(raw)
                result = wire.read_sse(io.BytesIO(raw))
            else:
                response = client.chat.completions.with_raw_response.create(**request)
                headers = dict(response.http_response.headers)
                raw.extend(response.http_response.content)
                parsed = response.parse()
                sdk = parsed.model_dump(mode="json")
                wire.require(parsed._request_id == "trace-" + label, "SDK trace ID missing")
                values = [json.loads(raw)]
                result = wire.read_json(io.BytesIO(raw))
            wire.save(folder / (label + "-result.json"), dict(**asdict(result),
                      seconds=time.monotonic() - begin))
            wire.require(headers["x-request-id"] == "trace-" + label and
                         result.id != headers["x-request-id"], "trace/cancel IDs conflated")
            b.t.nullable_contract(values, streaming)
            self.wait_slot(label)
            return result
        except BaseException as error:
            response = getattr(error, "response", None)
            if not raw and response is not None:
                raw.extend(response.content)
                headers = dict(response.headers)
            wire.save(folder / (label + "-failure.json"), dict(error=repr(error),
                      seconds=time.monotonic() - begin))
            raise
        finally:
            (folder / (label + "-wire.bin")).write_bytes(raw)
            wire.save(folder / (label + "-headers.json"), headers)
            if sdk is not None:
                wire.save(folder / (label + "-sdk.json"), sdk)

    def success(self, folder, label, body):
        if label == "project-read":
            body["extra_body"].update(thinking_token_budget=8, reasoning_field="reasoning")
            body["extra_body"]["chat_template_kwargs"]["enable_thinking"] = True
        elif label == "project-answer":
            wire.require(any(m.get("reasoning") for m in body["messages"]
                             if m["role"] == "assistant"), "real reasoning history lost")
            body["extra_body"].update(thinking_token_budget=8, include_reasoning=False)
            body["extra_body"]["chat_template_kwargs"]["enable_thinking"] = False
        result = super().success(folder, label, body)
        limit = body.get("extra_body", {}).get("thinking_token_budget")
        accounting(result.usage, limit)
        if label == "project-read":
            wire.require(bool(result.message.get("reasoning")), "tool reasoning missing")
        return result

    def negatives(self, folder):
        before, _ = self.snapshot(folder, "before-negative")
        for label, raw in negative_cases():
            self.counted(negative=True)
            status, _, value = self.exchange(folder, label,
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: " +
                str(len(raw)).encode() + b"\r\n\r\n" + raw)
            wire.require(status == 400, "budget input expected pre-stream 400")
            b.t.error_contract(value, "invalid_request", "thinking_token_budget")
        after, _ = self.snapshot(folder, "after-negative")
        for key in ("q4t_prompt_tokens_total", "q4t_generation_tokens_total",
                    "q4t_structured_workers_started_total"):
            wire.require(before[key] == after[key], "invalid budget performed work")
        self.resources_empty(folder, "negative-drained")

    def strict_length(self, folder):
        self.counted()
        body = schema_body(0, maximum=1)
        body.update(body.pop("extra_body"))
        raw = json.dumps(body, ensure_ascii=False).encode()
        wire.save(folder / "strict-length-request.json", body)
        status, _, response = self.exchange(folder, "strict-length",
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
            b"X-Request-Id: trace-strict-length\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(raw)).encode() + b"\r\n\r\n" + raw)
        s.error_terminal(status, response.decode(), True)
        self.wait_slot("strict-length")

    def cache(self, folder):
        body = cache_seed()
        first = self.success(folder, "cache-seed", body)
        quality(first)
        wire.require(first.usage["prompt_tokens"] >= 8192, "cache threshold not exercised")
        body["messages"] += [copy.deepcopy(first.message),
                              dict(role="user", content="仍然只回复数字710003。")]
        cached = self.success(folder, "cache-restored", body)
        quality(cached)
        wire.require(cached.usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                     >= 8192, "private snapshot was not restored")
        del body["extra_body"]["session_id"]
        fresh = self.success(folder, "cache-fresh", body)
        quality(fresh)
        wire.require(cached.message["content"] == fresh.message["content"] and
                     cached.finish_reason == fresh.finish_reason and
                     cached.usage["prompt_tokens"] == fresh.usage["prompt_tokens"],
                     "restored and fresh business answer differs")
        wire.save(folder / "cache-comparison.json", dict(passed=True,
            exact_message=cached.message == fresh.message,
            completion_tokens=[cached.usage["completion_tokens"], fresh.usage["completion_tokens"]],
            numerical_equivalence_claim=False))

    def business(self, folder):
        self.negatives(folder)
        control = self.success(folder, "unbudgeted", b.simple(thinking=True))
        quality(control)
        prior = json.loads((ROOT / ".q4t-work/serve-stage-b2a-20261103/execution/business/"
            "candidate-mtp/default-json-visible-result.json").read_text())
        wire.require(control.message == prior["message"] and control.usage == prior["usage"],
                     "unbudgeted baseline response changed")
        null_body = b.simple(thinking=True)
        null_body["extra_body"]["thinking_token_budget"] = None
        null = self.success(folder, "null-budget", null_body)
        b.compare(control, null)
        for label, limit, stream, include in (("zero", 0, False, True),
            ("one-hidden", 1, True, False), ("sixteen", 16, False, True),
            ("sixteen-hidden", 16, True, False), ("natural", 128, True, True)):
            result = self.success(folder, label, simple(limit, stream=stream, include=include))
            quality(result)
            if label == "sixteen":
                visible = result
            elif label == "sixteen-hidden":
                expected = copy.deepcopy(visible.message)
                expected.pop("reasoning", None)
                wire.require(result.message == expected and result.usage == visible.usage,
                             "hiding changed budget behavior")
        closed = self.success(folder, "closed-prefix", simple(0, stream=True, thinking=False))
        quality(closed)
        wire.require(closed.usage["completion_tokens_details"]["reasoning_tokens"] == 0,
                     "closed prompt charged reasoning")
        self.project_loop(folder, strict=True, stream=True)
        structured = self.success(folder, "schema", schema_body())
        wire.require(structured.finish_reason == "stop" and
                     json.loads(structured.message["content"]) == dict(answer="710003"),
                     "schema budget quality")
        body = simple(0)
        body["max_completion_tokens"] = 1
        limited = self.success(folder, "ordinary-length", body)
        wire.require(limited.finish_reason == "length" and
                     limited.usage["completion_tokens"] == 1 and
                     not limited.message.get("content"), "total limit lost")
        self.strict_length(folder)
        self.cache(folder)
        self.resources_empty(folder, "business-drained")
        self.traces(folder, mtp=True)

    def plain(self, folder):
        for limit in (0, 16):
            quality(self.success(folder, "plain-" + str(limit), simple(limit, stream=bool(limit))))
        self.resources_empty(folder, "plain-drained")
        self.traces(folder, mtp=False)

    def health(self, folder):
        quality(self.success(folder, "installed-zero", simple(0)))
        self.resources_empty(folder, "installed-drained")
        self.traces(folder, mtp=False)

    def traces(self, folder, *, mtp):
        caps = json.loads(json.loads((folder / "capabilities.json").read_text())["raw"])
        wire.require(caps["reasoning"]["budget_profile"] == "q4t-thinking-v1" and
                     caps["reasoning"]["thinking_token_budget"], "budget capability drift")
        pattern = (r"\[thinking-budget\] request=trace-(\S+) limit=(\d+) consumed=(\d+) "
                   r"forced=(\d+) reasoning_tokens=(\d+) mtp_steps=(\d+) mtp_tokens=(\d+) fallback=(\d+)")
        keys = ("limit", "consumed", "forced", "reasoning_tokens", "mtp_steps", "mtp_tokens", "fallback")
        rows = {}
        for match in re.finditer(pattern, (folder / "server.log").read_text()):
            wire.require(match[1] not in rows, "duplicate budget trace")
            rows[match[1]] = dict(zip(keys, map(int, match.groups()[1:])))
        expected = []
        for path in folder.glob("*-result.json"):
            value = json.loads(path.read_text())
            if "usage" not in value or "q4t_thinking_budget" not in value["usage"]:
                continue
            label = path.name.removesuffix("-result.json")
            expected.append(label)
            row, budget = rows[label], value["usage"]["q4t_thinking_budget"]
            wire.require(row["limit"] == budget["limit"] and
                         row["consumed"] == budget["consumed"] and
                         row["forced"] == budget["forced_closure_tokens"] and
                         row["reasoning_tokens"] == value["usage"]["completion_tokens_details"]["reasoning_tokens"],
                         "trace/usage mismatch")
        if mtp:
            expected.append("strict-length")
            for label in ("sixteen", "sixteen-hidden", "cache-seed"):
                wire.require(rows[label]["mtp_steps"] > 0 and rows[label]["fallback"] == 1,
                             label + ": MTP fallback not exercised")
            wire.require(rows["natural"]["mtp_steps"] > 0 and rows["natural"]["forced"] == 0,
                         "natural closing case not exercised")
            wire.require(rows["closed-prefix"]["consumed"] == rows["closed-prefix"]["forced"] == 0,
                         "closed prefix charged")
            for label in ("zero", "one-hidden", "project-read", "schema", "strict-length"):
                wire.require(rows[label]["mtp_steps"] == 0, "unexpected speculative execution")
        else:
            wire.require(all(row["mtp_steps"] == 0 for row in rows.values()), "plain MTP work")
        for label in ("zero", "one-hidden", "sixteen", "sixteen-hidden", "project-read",
                      "schema", "ordinary-length", "strict-length", "cache-seed", "plain-0",
                      "plain-16", "installed-zero"):
            if label in rows:
                wire.require(rows[label]["consumed"] == rows[label]["limit"] and
                             0 < rows[label]["forced"] <= 11, label + ": closure not exercised")
        wire.require(set(rows) == set(expected), "missing/unexpected budget trace")
        metrics, _ = self.snapshot(folder, "budget-metrics")
        for metric, value in (
            ("q4t_reasoning_budget_tokens_total", sum(r["consumed"] for r in rows.values())),
            ("q4t_reasoning_forced_tokens_total", sum(r["forced"] for r in rows.values())),
            ("q4t_reasoning_forced_requests_total", sum(r["forced"] > 0 for r in rows.values())),
            ("q4t_reasoning_mtp_fallbacks_total", sum(r["fallback"] for r in rows.values()))):
            wire.require(metrics[metric] == value, "budget metric mismatch: " + metric)
        wire.save(folder / "budget-trace-result.json", dict(passed=True, requests=rows))

    def model(self, name, binary, mtp, action):
        cache = self.out / name / "session-cache" if name == "candidate-mtp" else None
        def launch_command(binary, model, port, mtp):
            if cache is not None:
                prepare_cache(cache)
            argv = command(binary, model, port, mtp, cache)
            preflight = self.plan["startup_preflight"]
            wire.require(wire.digest(Path(preflight["path"])) == preflight["sha256"],
                         "startup preflight identity changed")
            result = subprocess.run([preflight["path"], *argv[2:]], cwd=ROOT,
                                    capture_output=True, text=True, timeout=20)
            wire.save(self.out / name / "startup-preflight.json", dict(argv=argv,
                returncode=result.returncode, stdout=result.stdout, stderr=result.stderr))
            wire.require(result.returncode == 0, "CPU startup preflight failed")
            return argv
        with patch.object(b, "command", launch_command):
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
            wire.require(self.requests == (1 if self.installed else 18) and
                         self.negative == (0 if self.installed else 12), "missing frozen cases")
            for script in ("tools/serve_stage_a/check_schema.py", "tools/serve_stage_b2a/check_schema.py",
                           "tools/serve_thinking_budget/check_schema.py"):
                subprocess.run(["/usr/bin/python3", "-B", str(ROOT / script), str(self.out)],
                               cwd=ROOT, check=True, timeout=30)
            state["status"] = "PASS_BOUNDED_THINKING_BUDGET"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(request_attempts=self.requests, negative_requests=self.negative,
                starts=self.starts, seconds=time.monotonic() - self.started,
                performance_acceptance=False)
            wire.save(self.out / "result.json", state)
