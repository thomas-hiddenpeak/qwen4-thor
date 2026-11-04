"""Bounded actual preflight/config, SDK generation and project-tool acceptance."""
import copy
import json
from pathlib import Path
import re
import subprocess
import time
from unittest.mock import patch

import stage

transport = stage.module("preflight_budget_transport",
                         stage.ROOT / "tools/serve_thinking_budget/verify.py")
b, s, wire, ROOT = transport.b, transport.s, transport.wire, stage.ROOT
original_command = b.command


def command(binary, model, port, mtp, explicit=False):
    args = original_command(binary, model, port, mtp)
    if explicit:
        args[args.index("--max-seq") + 1] = "2"
        args += ["--seq-capacities", "2048,8192"]
    return args


def flattened(body):
    value = copy.deepcopy(body)
    value.update(value.pop("extra_body", {}))
    value.pop("extra_headers", None)
    return value


def negative_cases():
    return [(dict(unknown=True), "unknown"), (dict(model="other"), "model"),
        (dict(tool_choice="none", stop="END"), "stop"),
        (dict(session_id="a" * 64), "session_id"),
        (dict(prefix_cache_scope="a" * 64), "prefix_cache_scope"),
        (dict(messages=[dict(role="wrong", content="q")]), "messages[0].role"),
        (dict(max_completion_tokens="1"), "max_completion_tokens"),
        (dict(max_tokens=1), "max_completion_tokens")]


class Run(b.Run):
    sdk_chat = transport.Run.sdk_chat

    def __init__(self, plan, output, *, installed=False):
        self.preflights = 0
        super().__init__(plan, output, installed=installed)
        self.deadline = plan.get("original_started", plan["started"]) + 1800 - 30
        self.checked = []

    def budget(self):
        wire.require(time.time() < self.deadline, "preflight stage deadline")
        prior = getattr(self, "plan", {}).get("prior_counts", {})
        if getattr(self, "plan", {}).get("combined_plain"):
            wire.require(not self.installed and
                self.requests <= 12 - prior["generations"] and
                self.starts <= 3 - prior["starts"] and
                self.capability_gets <= 3 - prior["starts"] and
                self.negative <= 20 - prior["negatives"] and
                self.preflights <= 32 - prior["preflights"], "frozen stage budget")
            return
        wire.require(self.requests <= (2 if self.installed else 10 - prior.get("generations", 0)) and
            self.starts <= (1 if self.installed else 2 - prior.get("starts", 0)) and
            self.capability_gets <= (1 if self.installed else 2 - prior.get("starts", 0)) and
            self.negative <= (0 if self.installed else 20 - prior.get("negatives", 0)) and
            self.preflights <= (2 if self.installed else 30 - prior.get("preflights", 0)),
            "frozen stage budget")

    def wait_slot(self, name):
        released_by = time.monotonic() + 3
        while True:
            code, raw = self.request("GET", "/healthz")
            health = json.loads(raw)
            wire.require(code == 200 and health["gpu_healthy"] and
                health["seq_slots_total"] > 0, name + ": unhealthy")
            if health["seq_slots_free"] == health["seq_slots_total"]:
                break
            wire.require(time.monotonic() < released_by, name + ": slot not released")
            time.sleep(0.01)
        self.observe(lambda rows: rows["q4t_active_chats"] == 0 and
            rows["q4t_structured_active"] == 0 and
            rows["q4t_structured_workers_active"] == 0)

    def http(self, folder, label, method, path, body=None, content_type="application/json"):
        payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
        return self.exchange(folder, label,
            f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nX-Request-Id: trace-{label}\r\n".encode() +
            f"Content-Type: {content_type}\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload)

    def preflight(self, folder, label, request=None, *, envelope=None, previews=True,
                  status=200, code=None, param=None):
        self.preflights += 1
        self.budget()
        if envelope is None:
            envelope = dict(request=flattened(request), include_prompt=previews,
                            include_token_ids=previews)
        before, _ = self.snapshot(folder, label + "-before-preflight")
        actual, headers, raw = self.http(folder, label + "-preflight", "POST",
                                        "/v1/chat/preflight", envelope)
        self.wait_slot(label)
        after, _ = self.snapshot(folder, label + "-after-preflight")
        for key in ("q4t_requests_total", "q4t_prompt_tokens_total",
                    "q4t_generation_tokens_total", "q4t_execution_steps_total",
                    "q4t_structured_workers_started_total"):
            wire.require(before[key] == after[key], "preflight performed work: " + key)
        wire.require(actual == status and headers["content-type"] == "application/json",
                     label + ": preflight status/type")
        if status != 200:
            b.t.error_contract(raw, code, param)
            return json.loads(raw)
        response = json.loads(raw)
        wire.require(response["profile"] == "q4t-preflight-v1" and
            response["protocol_source_sha256"] == self.plan["protocol_sha256"] and
            response["execution_guaranteed"] is False and
            response["admission"]["slot_reserved"] is False, "preflight identity/contract")
        if previews:
            wire.require(len(response["token_ids"]) == response["prompt_tokens"] and
                len(response["prompt"].encode()) == response["prompt_bytes"], "preview count")
        else:
            wire.require("prompt" not in response and "token_ids" not in response,
                         "preview leaked without opt in")
        wire.save(folder / (label + "-preflight.json"), response)
        self.resources_empty(folder, label + "-preflight-drained")
        return response

    def success(self, folder, label, body):
        if body["messages"][0]["role"] not in ("system", "developer"):
            body["messages"].insert(0, dict(role="developer", content="遵循用户请求。"))
        body.setdefault("extra_body", {})["request_timeout_ms"] = 180000
        preflight = self.preflight(folder, label, body)
        if label == "ordinary-json":
            self.counted(negative=True)
            status, _, raw = self.http(folder, "preflight-not-registered", "POST",
                "/v1/requests/cancel", dict(request_id="preflight-identity", cancel_token="c" * 64))
            wire.require(status == 404, "preflight registered cancellation identity")
            b.t.error_contract(raw, "request_not_found")
        with self.sdk() as client:
            result = self.sdk_chat(client, folder, label, body)
        wire.require(result.usage["prompt_tokens"] == preflight["prompt_tokens"],
                     label + ": preflight/actual prompt usage differs")
        wire.require(result.usage["completion_tokens"] <= preflight["output"]["available_tokens"],
                     label + ": output exceeded planned budget")
        self.checked.append(dict(label=label, prompt_tokens=preflight["prompt_tokens"],
                                 usage=result.usage, admission=preflight["admission"]))
        return result

    def config(self, folder, *, explicit=False, mtp=True):
        status, _, raw = self.http(folder, "effective-config", "GET", "/v1/config")
        wire.require(status == 200, "configuration unavailable")
        config = json.loads(raw)
        wire.save(folder / "effective-config.json", config)
        wire.require(config["protocol_source_sha256"] == self.plan["protocol_sha256"] and
            config["max_context_tokens"] == dict(requested=8192, effective=8192,
                source="cli", resolution="explicit_slots" if explicit else "startup_memory_budget") and
            config["max_sequences"]["effective"] == (2 if explicit else 1) and
            config["slots"]["capacities"] == ([2048, 8192] if explicit else [8192]) and
            config["runtime"]["mtp_enabled"] == mtp and
            config["default_output_tokens"]["source"] == "builtin_default" and
            config["default_output_tokens"]["effective"] == 256 and
            config["prefill_chunk_tokens"]["effective"] == 2048,
            "effective configuration differs from actual launch")
        wire.require(str(self.args.model_dir) not in raw.decode() and
                     str(ROOT) not in raw.decode(), "private path leaked")

    def rejection_pair(self, folder, label, body, code, param):
        preflight = self.preflight(folder, label, body, status=400, code=code, param=param)
        self.counted(negative=True)
        status, _, raw = self.http(folder, label + "-chat", "POST", "/v1/chat/completions", body)
        wire.require(status == 400, "Chat rejection differs from preflight")
        b.t.error_contract(raw, code, param)
        wire.require(json.loads(raw) == preflight, "shared planning errors differ")
        self.wait_slot(label)

    def negatives(self, folder):
        for index, (fields, param) in enumerate(negative_cases()):
            body = dict(messages=[dict(role="user", content="q")],
                        stream=False, max_completion_tokens=1)
            body.update(fields)
            self.rejection_pair(folder, "negative-" + str(index), body,
                                "invalid_request", param)
        for index, (body, param) in enumerate([
            (dict(request={}, include_prompt=None), "include_prompt"),
            (dict(request={}, unknown=True), "unknown"), ({}, "request")]):
            self.preflight(folder, "envelope-" + str(index), envelope=body,
                           status=400, code="invalid_request", param=param)
        self.preflights += 1
        payload = b'{"request":{"prompt":"q"},"request":{"prompt":"q"}}'
        status, _, raw = self.exchange(folder, "duplicate-envelope",
            b"POST /v1/chat/preflight HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Type: application/json\r\nContent-Length: " +
            str(len(payload)).encode() + b"\r\n\r\n" + payload)
        wire.require(status == 400, "duplicate envelope accepted")
        b.t.error_contract(raw, "invalid_json")
        cases = [("GET", "/v1/chat/preflight", None, "application/json", 405,
                  "method_not_allowed", "method"),
                 ("POST", "/v1/config", {}, "application/json", 405,
                  "method_not_allowed", "method"),
                 ("POST", "/v1/chat/preflight", {}, "text/plain", 415,
                  "unsupported_media_type", "Content-Type"),
                 ("GET", "/v1/config", {}, "application/json", 400,
                  "unexpected_body", "body"),
                 ("GET", "/v1/config?x=1", None, "application/json", 400,
                  "unsupported_query", "path")]
        for index, (method, path, body, mime, status, code, param) in enumerate(cases):
            self.counted(negative=True)
            actual, headers, raw = self.http(folder, "route-" + str(index),
                                             method, path, body, mime)
            wire.require(actual == status, "wrong route status")
            b.t.error_contract(raw, code, param)
            if status == 405:
                wire.require(headers["allow"] == ("post" if method == "GET" else "get"),
                             "missing Allow")
        self.rejection_pair(folder, "context-overflow", dict(
            prompt="<|im_end|>" * 8192, max_completion_tokens=1),
            "context_length_exceeded", "prompt")
        clipped = self.preflight(folder, "context-clip", dict(
            prompt="<|im_end|>" * 8191, max_completion_tokens=2147483647))
        wire.require(clipped["prompt_tokens"] == 8191 and
                     clipped["output"]["available_tokens"] == 1 and
                     clipped["output"]["capped_tokens"] == 8192, "uniform clipping")
        default = self.preflight(folder, "defaults", dict(prompt="e\u0301 中文 🚀"),
                                 previews=False)
        wire.require(default["output"]["source"] == "server_default" and
                     default["output"]["requested_tokens"] == 256, "output default")

    def business(self, folder):
        self.config(folder)
        self.negatives(folder)
        body = b.simple()
        body["extra_body"].update(request_id="preflight-identity", cancel_token="c" * 64)
        plain = self.success(folder, "ordinary-json", body)
        streamed = self.success(folder, "ordinary-sse", b.simple(stream=True))
        b.compare(plain, streamed)
        wire.require(plain.message["content"].strip() == "710003", "ordinary quality")
        stop = s.request("严格逐字复制下一行，不加解释或引号：\nALPHA0123456789ENDOMEGA",
                         stream=True, max_completion_tokens=128)
        stop["stop"] = "456789END"
        stopped = self.success(folder, "text-stop", stop)
        wire.require(stopped.message["content"].strip() == "ALPHA0123", "stop quality")
        body = transport.simple(0, stream=True)
        body["stop"] = "\n</think>\n\n"
        closed = self.success(folder, "thinking-close", body)
        wire.require(not closed.message["content"] and closed.finish_reason == "stop" and
                     closed.usage["completion_tokens"] == 3, "thinking closure contract")
        strict = self.success(folder, "strict-schema", transport.schema_body(1))
        wire.require(json.loads(strict.message["content"]) == dict(answer="710003"),
                     "schema quality")
        self.project_loop(folder, strict=True, stream=True)
        rows = stage.h.read(stage.PACKAGE / "cpu/template-diff/cases.json")
        long_request = next(r["request"] for r in rows if r["id"] == "long-tool-history")
        long_body = dict(model="qwen3.8-flash-next", messages=long_request["messages"],
            tools=long_request["tools"], stream=False, max_completion_tokens=128,
            extra_body={k: v for k, v in long_request.items() if k not in ("messages", "tools")})
        long_result = self.success(folder, "long-history", long_body)
        wire.require((long_result.message["content"] or "").strip() == "710003" and
                     long_result.usage["prompt_tokens"] > 2048, "long history quality")
        self.resources_empty(folder, "business-drained")

    def explicit(self, folder):
        self.config(folder, explicit=True)
        base = self.preflight(folder, "slot-count", b.simple())
        count = base["prompt_tokens"]
        for extra in (0, 1):
            body = b.simple()
            body["max_completion_tokens"] = 2048 - count + extra
            label = "slot-" + str(extra)
            result = self.success(folder, label, body)
            wire.require(result.message["content"].strip() == "710003", "slot quality")
            planned = self.checked[-1]["admission"]
            wire.require(planned["eligible_slots"] == ([0, 1] if extra == 0 else [1]),
                         "explicit eligibility differs at boundary")
        self.rejection_pair(folder, "slot-overflow", dict(prompt="q",
            max_completion_tokens=2147483647), "context_length_exceeded", "max_completion_tokens")
        log = (folder / "server.log").read_text()
        for slot, required in ((0, 2048), (1, 2049)):
            wire.require(re.search(rf"\[q4t\]\[slot-admission\].* slot={slot} capacity="
                rf"{2048 if slot == 0 else 8192} required={required}\n", log),
                "actual slot selection differs from planning")
        self.resources_empty(folder, "explicit-drained")

    def corrected_explicit(self, folder, *, mtp=True):
        self.config(folder, explicit=True, mtp=mtp)
        base = self.preflight(folder, "slot-count", b.simple())
        count = base["prompt_tokens"]
        body = b.simple()
        body["max_completion_tokens"] = 2048 - count
        small = self.success(folder, "slot-short", body)
        wire.require(small.message["content"].strip() == "710003" and
            self.checked[-1]["admission"]["eligible_slots"] == [0, 1], "short slot")
        body["max_completion_tokens"] += 1
        edge = self.preflight(folder, "slot-edge-readonly", body)
        wire.require(edge["admission"]["eligible_slots"] == [1] and
            edge["admission"]["required_capacity"] == 2049, "exact eligibility edge")
        rows = stage.h.read(stage.PACKAGE / "cpu/template-diff/cases.json")
        request = next(r["request"] for r in rows if r["id"] == "long-tool-history")
        body = dict(model="qwen3.8-flash-next", messages=request["messages"],
            tools=request["tools"], stream=False, max_completion_tokens=128,
            tool_choice="none", extra_body={k: v for k, v in request.items()
                                           if k not in ("messages", "tools")})
        result = self.success(folder, "long-history-final-answer", body)
        wire.require((result.message["content"] or "").strip() == "710003" and
            result.finish_reason == "stop" and not result.message.get("tool_calls"),
            "explicit final-answer long history quality")
        long = self.checked[-1]["admission"]
        wire.require(long["eligible_slots"] == [1], "long history slot eligibility")
        self.rejection_pair(folder, "slot-overflow", dict(prompt="q",
            max_completion_tokens=2147483647), "context_length_exceeded", "max_completion_tokens")
        log = (folder / "server.log").read_text()
        for slot, required in ((0, 2048), (1, long["required_capacity"])):
            wire.require(re.search(rf"\[q4t\]\[slot-admission\].* slot={slot} capacity="
                rf"{2048 if slot == 0 else 8192} required={required}\n", log),
                "actual slot assignment differs")
        self.resources_empty(folder, "explicit-drained")

    def combined_plain(self, folder):
        self.corrected_explicit(folder, mtp=False)
        left = self.success(folder, "plain-json", b.simple())
        right = self.success(folder, "plain-sse", b.simple(stream=True))
        b.compare(left, right)
        wire.require((left.message["content"] or "").strip() == "710003",
                     "plain quality")
        self.resources_empty(folder, "plain-drained")

    def health(self, folder):
        self.config(folder, mtp=False)
        left = self.success(folder, "installed-json", b.simple())
        right = self.success(folder, "installed-sse", b.simple(stream=True))
        b.compare(left, right)
        wire.require(left.message["content"].strip() == "710003", "installed plain quality")
        self.resources_empty(folder, "installed-drained")

    def model(self, name, binary, mtp, action):
        argv = command(binary, self.args.model_dir, self.args.port, mtp,
                       explicit=name in ("candidate-explicit", "candidate-plain-explicit"))
        probe = self.plan["startup_preflight"]
        wire.require(wire.digest(Path(probe["path"])) == probe["sha256"], "startup probe drift")
        checked = subprocess.run([probe["path"], *argv[2:]], cwd=ROOT,
                                 capture_output=True, text=True, timeout=20)
        wire.save(self.out / (name + "-startup.json"), dict(argv=argv,
            returncode=checked.returncode, stdout=checked.stdout, stderr=checked.stderr))
        wire.require(checked.returncode == 0, "production CLI preflight failed")
        with patch.object(b, "command", lambda *unused: argv):
            super().model(name, binary, mtp, action)

    def run(self):
        state = dict(status="FAILED")
        try:
            with self.sdk():
                pass
            binary = ROOT / "build/q4t" if self.installed else Path(self.plan["candidate"])
            if self.installed:
                self.model("candidate-installed", binary, False, self.health)
            elif self.plan.get("reuse_http"):
                reuse = self.plan["reuse_http"]
                folder = self.out / "candidate-mtp"
                folder.mkdir()
                for name, identity in reuse["files"].items():
                    stage.h.copy_checked(identity, folder / name)
                self.checked.extend(reuse["successful_comparisons"])
                wire.save(self.out / "reused-evidence.json", reuse)
                if self.plan.get("combined_plain"):
                    self.model("candidate-plain-explicit", binary, False, self.combined_plain)
                else:
                    self.model("candidate-explicit", binary, True, self.corrected_explicit)
            else:
                probe = self.plan["lifecycle_probe"]
                wire.require(wire.digest(Path(probe["path"])) == probe["sha256"], "lifecycle drift")
                checked = subprocess.run([probe["path"]], cwd=ROOT,
                    capture_output=True, text=True, timeout=30)
                (self.out / "http-lifecycle.log").write_text(checked.stdout + checked.stderr)
                wire.require(checked.returncode == 0, "HTTP lifecycle contracts failed")
                self.model("candidate-mtp", binary, True, self.business)
                self.model("candidate-explicit", binary, True, self.explicit)
            corrected = bool(self.plan.get("reuse_http"))
            combined = bool(self.plan.get("combined_plain"))
            wire.require(self.requests == (4 if combined else 2 if self.installed or corrected else 10) and
                self.preflights == (7 if combined else 2 if self.installed else 5 if corrected else 27) and
                self.negative == (0 if self.installed else 1 if corrected else 16),
                "missing frozen cases")
            for script in ("tools/serve_preflight/check_schema.py",
                           "tools/serve_stage_b2a/check_schema.py",
                           "tools/serve_thinking_budget/check_schema.py"):
                subprocess.run(["/usr/bin/python3", "-B", str(ROOT / script), str(self.out)],
                               cwd=ROOT, check=True, timeout=30)
            state["status"] = "PASS_BOUNDED_PREFLIGHT"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(request_attempts=self.requests, negative_requests=self.negative,
                preflight_requests=self.preflights, starts=self.starts,
                comparisons=self.checked, seconds=time.monotonic() - self.started,
                performance_acceptance=False)
            prior = self.plan.get("prior_counts", {})
            state["cumulative"] = {key: prior.get(key, 0) + value for key, value in
                dict(generations=self.requests, negatives=self.negative,
                     preflights=self.preflights, starts=self.starts).items()}
            wire.save(self.out / "result.json", state)
