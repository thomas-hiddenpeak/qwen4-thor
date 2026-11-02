"""Bounded real admission, capacity FIFO, cancellation and project-tool checks.

The validation pause holds one already-generated response for at most 10 s.
It does not change logits, inject response bytes, or become a deployed binary.
"""
from dataclasses import asdict
import copy
import http.client
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

from tools.serve_admission import stage
from tools.serve_backpressure.verify import memory_snapshot
from tools.serve_protocol.verify import metrics, strict_read_tools
from tools.serve_protocol.js_client import JsSdkClient
from tools.serve_protocol.check_contract import load_validators
from chat_client import (ChatClient, RemoteError, append_final, append_tool_results,
                         make_request, prepare_tool_calls, require)
from project_query import SYSTEM, validate_answer
from project_records import ProjectRecords, VALIDATORS
from jsonschema import Draft202012Validator

h, ROOT = stage.h, stage.ROOT
EMPTY = ("q4t_active_chats", "q4t_queued_requests", "q4t_request_body_bytes",
         "q4t_structured_active", "q4t_structured_preparing",
         "q4t_structured_vocab_bytes", "q4t_structured_workers_active")


def ordinary(*, large=False, stream=False):
    return make_request([dict(role="user", content="只回复数字710003。")], [],
        stream=stream, max_tokens=4096 if large else 128, timeout_ms=180000)


def constrained(*, hold=False, large=False, stream=False):
    schema = (dict(type="object", properties=dict(data=dict(type="array", minItems=64,
                   maxItems=64, items=dict(type="boolean", enum=[True]))),
                   required=["data"], additionalProperties=False) if hold else
              dict(type="object", properties=dict(ok=dict(type="boolean", enum=[True])),
                   required=["ok"], additionalProperties=False))
    body = make_request([dict(role="user", content="仅输出符合schema的JSON。")], [],
        stream=stream, max_tokens=4096 if large else 1024 if hold else 128,
        timeout_ms=180000)
    body.update(tool_choice="none", response_format=dict(type="json_schema",
        json_schema=dict(name="admission", strict=True, schema=schema)))
    return body


def wire_values(raw, stream):
    return ([json.loads(line[6:]) for line in raw.decode().splitlines()
             if line.startswith("data: ") and line != "data: [DONE]"]
            if stream else [json.loads(raw)])


def error_contract(response, status, code, *, stream=False):
    require(response["status"] == status, "incorrect HTTP rejection status")
    raw = response["raw"].encode()
    values = wire_values(raw, stream)
    require(values and set(values[-1]) == {"error"}, "error must be the terminal envelope")
    require(values[-1]["error"]["code"] == code, "incorrect logical error code")
    require(len(values) <= (2 if stream else 1), "duplicate error or unexpected payload")
    for prefix in values[:-1]:
        choices = prefix.get("choices", [])
        require(prefix.get("object") == "chat.completion.chunk" and
                prefix.get("usage") is None and len(choices) == 1 and
                choices[0].get("index") == 0 and choices[0].get("finish_reason") is None and
                choices[0].get("delta") in ({"role": "assistant"}, {"role": "assistant", "content": ""}),
                "only the initial role announcement may precede an admission error")
    if stream:
        require(response["headers"].get("Content-Type", "").startswith("text/event-stream"),
                "incorrect SSE content type")
        require(raw.count(b"data: [DONE]") == 1 and raw.rstrip().endswith(b"data: [DONE]"),
                "missing/duplicate/error terminal order")
    return values


class Pending:
    def __init__(self, base, folder, label, body, client=None):
        self.body, self.folder, self.label = body, folder, label
        self.value = self.error = None
        self.started = time.monotonic()
        self.client = client or ChatClient(base)
        self.thread = threading.Thread(target=self.run, name=label, daemon=True)
        self.thread.start()

    def run(self):
        try:
            with (self.folder / (self.label+"-wire.bin")).open("xb", buffering=0) as wire:
                def capture(part):
                    wire.write(part)
                    os.fsync(wire.fileno())
                try:
                    self.value = self.client.complete(self.body, sink=capture)
                except RemoteError as error:
                    # Non-200 urllib responses are consumed before the sink.
                    # Preserve the actual error body, not a fabricated envelope.
                    if wire.tell() == 0 and isinstance(error.body, (str, bytes)):
                        capture(error.body.encode() if isinstance(error.body, str) else error.body)
                    raise
            h.save(self.folder / (self.label+"-result.json"), asdict(self.value))
        except BaseException as error:
            self.error = error
            h.save(self.folder / (self.label+"-error.json"), dict(error=repr(error),
                code=getattr(error, "code", None), status=getattr(error, "status", None),
                automatic_retry=False, complete_assistant_turn=False))
        finally:
            h.save(self.folder / (self.label+"-elapsed.json"),
                   dict(seconds=time.monotonic()-self.started))

    def finish(self, *, cancelled=False, timeout=180):
        self.thread.join(timeout=timeout)
        require(not self.thread.is_alive(), "owned client timed out: " + self.label)
        if cancelled:
            require(self.value is None and isinstance(self.error, RemoteError) and
                    self.error.code == "explicit_cancel", "cancelled request reported success/other failure")
            return
        if self.error:
            raise self.error
        require(self.value is not None, "missing response")
        return self.value


class Run:
    def __init__(self, plan, output):
        self.plan, self.out = plan, output
        output.mkdir(mode=0o700)
        self.started = time.monotonic()
        self.deadline = plan["started"] + stage.LIMITS["seconds"] - 60
        self.counts = dict(generations=0, negatives=0, starts=0)
        self.port = plan["port"]
        self.base = f"http://127.0.0.1:{self.port}"
        self.server = self.server_fd = None
        self.calls, self.wires = [], []
        self.schemas = load_validators()
        self.transport = Draft202012Validator(h.read(ROOT / "docs/serve-transport-v1.schema.json"))
        self.config_schema = Draft202012Validator(h.read(ROOT / "docs/serve-config-v1.schema.json"))

    def budget(self, kind=None):
        require(time.time() < self.deadline, "original stage deadline")
        if kind:
            require(self.counts[kind] < stage.LIMITS[kind], "stage budget: " + kind)
            self.counts[kind] += 1
            h.save(self.out / f"attempt-{kind}-{self.counts[kind]:02d}.json",
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def http(self, method, path, body=None, *, timeout=5):
        self.budget()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, None if body is None else json.dumps(body),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read(8*1024*1024+1)
            require(len(raw) <= 8*1024*1024, "HTTP observation too large")
            return dict(status=response.status, headers=dict(response.getheaders()), raw=raw.decode())
        finally:
            conn.close()

    def rows(self):
        value = self.http("GET", "/metrics")
        require(value["status"] == 200, "metrics unavailable")
        return metrics(value["raw"])

    def wait(self, label, predicate, *, timeout=10):
        end, samples = time.monotonic()+timeout, []
        try:
            while True:
                rows = self.rows()
                samples.append(dict(monotonic=time.monotonic(), metrics=rows))
                if predicate(rows):
                    return rows
                require(self.server.poll() is None and time.monotonic() < end,
                        "observation timed out: " + label)
                time.sleep(0.01)
        finally:
            h.save(self.folder / (label+"-samples.json"), samples)

    def snapshot(self, label):
        rows = self.rows()
        h.save(self.folder / (label+"-resources.json"),
               dict(metrics=rows, memory=memory_snapshot(self.server.pid)))
        return rows

    def drain(self, label):
        rows = self.wait(label, lambda m: all(m[key] == 0 for key in EMPTY) and
            m["q4t_seq_slots_free"] == m["q4t_seq_slots_total"], timeout=20)
        require(rows["q4t_structured_workers_started_total"] ==
                rows["q4t_structured_workers_reaped_total"], "worker not reaped")
        self.snapshot(label)
        return rows

    def health(self, label):
        for path in ("/healthz", "/readyz", "/livez"):
            value = self.http("GET", path)
            h.save(self.folder / (label+path.replace("/", "-")+".json"), value)
            require(value["status"] == 200, "control unavailable: " + path)

    def submit(self, label, body, *, client=None):
        self.schemas["chat"].validate(body)
        self.budget("generations")
        h.save(self.folder / (label+"-request.json"), body)
        pending = Pending(self.base, self.folder, label, body, client)
        self.calls.append(pending)
        return pending

    def check_wire(self, pending, *, success=True):
        path = self.folder / (pending.label+"-wire.bin")
        raw = path.read_bytes()
        values = wire_values(raw, pending.body["stream"])
        require(values, "missing wire")
        for value in values:
            self.transport.validate(value)
        if success:
            require(not any("error" in value for value in values), "success contains error")
        self.wires.append(dict(file=h.identity(path), envelopes=len(values), success=success))

    def normal(self, pending):
        value = pending.finish(timeout=8)
        require(value.finish_reason == "stop" and value.message["content"].strip() == "710003",
                "ordinary task quality failed: " + pending.label)
        self.check_wire(pending)
        return value

    def reject(self, label, path, body, status, code, *, stream=False):
        self.schemas["preflight" if path.endswith("preflight") else "chat"].validate(body)
        self.budget("negatives")
        h.save(self.folder / (label+"-request.json"), body)
        result = self.http("POST", path, body)
        h.save(self.folder / (label+"-http.json"), result)
        for value in error_contract(result, status, code, stream=stream):
            self.transport.validate(value)
        return result

    def cancel(self, pending, label):
        value = self.http("POST", "/v1/requests/cancel", {
            key: pending.body[key] for key in ("request_id", "cancel_token")})
        h.save(self.folder / (label+"-cancel.json"), value)
        require(value["status"] == 202, "cancel not accepted")
        pending.finish(cancelled=True, timeout=5)
        self.check_wire(pending, success=False)

    def production(self):
        before = self.drain("before")
        holding = self.submit("inflight-holder", constrained(hold=True, stream=True))
        self.wait("inflight-held", lambda m: m["q4t_active_chats"] == 1 and
                  m["q4t_seq_slots_free"] == 0 and m["q4t_structured_workers_active"] == 1)
        self.snapshot("inflight-held")
        self.reject("inflight-chat-reject", "/v1/chat/completions", ordinary(),
                    503, "capacity_exhausted")
        self.reject("inflight-preflight-reject", "/v1/chat/preflight",
                    dict(request=ordinary()), 503, "capacity_exhausted")
        self.health("inflight-held")
        self.cancel(holding, "inflight-holder")
        after = self.drain("after-cancel")
        require(after["q4t_text_admission_rejected_total"] - before[
                    "q4t_text_admission_rejected_total"] == 2 and
                after["q4t_queue_rejected_total"] == before["q4t_queue_rejected_total"],
                "inflight rejection accounting")
        self.normal(self.submit("ordinary-recovery", ordinary()))
        self.drain("after-ordinary")
        self.project_tool()

    def project_tool(self):
        sdk = JsSdkClient(self.base, Path(self.plan["sdk_directory"]),
                         protocol_sha256=self.plan["protocol_sha256"], timeout=180)
        h.save(self.folder / "sdk-capabilities.json", sdk.capabilities())
        tools = strict_read_tools(["docs/MODEL.md"])
        history = [dict(role="system", content=SYSTEM), dict(role="user", content=
            "先调用一次read_project_record读取docs/MODEL.md的第1到24行，start_line=1、line_count=24。"
            "再回答architectures字段的准确值，并附工具给出的引用。")]
        body = make_request(history, tools, stream=True, max_tokens=512, timeout_ms=180000)
        body.update(tool_choice="required", parallel_tool_calls=False)
        call = self.submit("project-read", body, client=sdk)
        result = call.finish()
        self.check_wire(call)
        planned = prepare_tool_calls(result, VALIDATORS)
        require(len(planned) == 1 and planned[0].arguments ==
            dict(path="docs/MODEL.md", start_line=1, line_count=24), "unexpected project read")
        invocation = planned[0]
        h.save(self.folder / "project-tool-started.json", asdict(invocation))
        value = ProjectRecords(ROOT).execute(invocation)
        require(value["ok"], "real read failed")
        value["application_budget"] = dict(remaining_model_requests=1,
            remaining_tool_calls=0, final_response_required=True)
        content = json.dumps(value, ensure_ascii=False)
        h.save(self.folder / "project-tool-result.json", dict(tool_call_id=invocation.call_id,
                                                              content=content))
        history = append_tool_results(history, result, {invocation.call_id: content})
        h.save(self.folder / "project-tool-history.json", history)
        body = make_request(history, tools, stream=True, max_tokens=512, timeout_ms=180000)
        body["tool_choice"] = "none"
        call = self.submit("project-answer", body, client=sdk)
        answer = call.finish()
        self.check_wire(call)
        citations = {value["source"]["citation"]}
        validate_answer(answer.message["content"], citations, 1)
        require("Qwen4ExpForConditionalGeneration" in answer.message["content"], "incorrect fact")
        h.save(self.folder / "project-completed.json", dict(
            history=append_final(history, answer), tool_calls=1, requests=2))
        self.drain("after-project")

    def queued(self, count, label):
        return self.wait(label, lambda m: m["q4t_queued_requests"] == count and
                         m["q4t_seq_slots_free"] == 1, timeout=2)

    def capacity(self):
        before = self.drain("before")
        body = ordinary(large=True)
        body["request_id"] = "srv02b-response-holder"
        holder = self.submit("holder", body)
        entered, release = self.folder / "response.entered", self.folder / "response.release"
        end = time.monotonic()+15
        while not entered.exists():
            self.budget()
            require(holder.thread.is_alive() and self.server.poll() is None and
                    time.monotonic() < end, "holder did not reach response gate")
            time.sleep(0.01)
        gated = time.monotonic()
        try:
            first = self.submit("first-cancelled", ordinary(large=True))
            self.queued(1, "first-queued")
            second = self.submit("second", ordinary(large=True))
            self.queued(2, "queue-full")
            before_reject = self.snapshot("queue-full")
            self.reject("queue-json-reject", "/v1/chat/completions", ordinary(large=True),
                        503, "queue_capacity_exhausted")
            self.reject("queue-sse-reject", "/v1/chat/completions",
                        constrained(large=True, stream=True), 200,
                        "queue_capacity_exhausted", stream=True)
            after_reject = self.snapshot("after-reject")
            require(after_reject["q4t_queue_rejected_total"] - before[
                    "q4t_queue_rejected_total"] == 2, "queue rejection counter")
            for key in ("q4t_prompt_tokens_total", "q4t_generation_tokens_total"):
                require(after_reject[key] == before_reject[key], "rejection executed model")
            bypass = self.submit("small-bypass", ordinary())
            self.normal(bypass)
            self.queued(2, "after-bypass")
            require(first.thread.is_alive() and second.thread.is_alive(), "large waiter escaped gate")
            self.health("queue-full")
            self.cancel(first, "first")
            self.queued(1, "after-head-cancel")
            last = self.submit("last", ordinary(large=True))
            self.queued(2, "queue-refilled")
            require(time.monotonic()-gated < 8, "gate time reserve exceeded")
        finally:
            release.touch(exist_ok=False)
            h.save(self.folder / "gate-timing.json", dict(seconds=time.monotonic()-gated,
                max_server_pause_seconds=10, no_generated_response_bytes_injected=True))
        for pending in (holder, second, last):
            self.normal(pending)
        self.drain("after-fifo")
        log = (self.folder / "server.log").read_text()
        admissions = [(r[1], int(r[2]), int(r[3])) for r in re.finditer(
            r"\[q4t\]\[slot-admission\] request=(\S+) slot=(\d+) capacity=(\d+)", log)]
        expected = [(holder.body["request_id"], 0, 8192), (bypass.body["request_id"], 1, 2048),
                    (second.body["request_id"], 0, 8192), (last.body["request_id"], 0, 8192)]
        h.save(self.folder / "admission-order.json", dict(actual=admissions, expected=expected))
        require(admissions == expected, "capacity FIFO order/slot ownership mismatch")
        recovery = self.submit("strict-recovery", constrained())
        value = recovery.finish()
        self.check_wire(recovery)
        require(value.finish_reason == "stop" and json.loads(value.message["content"]) == {"ok": True},
                "strict recovery quality")
        after = self.drain("after-strict")
        require(after["q4t_text_admission_rejected_total"] == before[
                    "q4t_text_admission_rejected_total"], "queue/inflight metrics conflated")

    def model(self, fault):
        self.budget("starts")
        self.folder = self.out / ("capacity-fault" if fault else "production-mtp")
        self.folder.mkdir()
        binary = self.plan["fault_binary"] if fault else self.plan["binary"]
        argv = [binary["path"], "serve", "--model-dir", self.plan["model_dir"], "--host",
            "127.0.0.1", "--port", str(self.port), "--max-prefill", "2048", "--max-len", "8192",
            "--max-seq", "2" if fault else "1", "--max-inflight-requests", "8" if fault else "1",
            "--max-queued-requests", "2" if fault else "0"]
        if fault:
            argv += ["--no-mtp", "--seq-capacities", "8192,2048"]
        env = {k:v for k,v in os.environ.items() if not k.startswith("Q4T_") and k != "LD_PRELOAD"}
        extra = dict(Q4T_SERVE_PAUSE="handler.before_response",
            Q4T_SERVE_REQUEST="srv02b-response-holder", Q4T_SERVE_GATE=str(self.folder / "response")) if fault else {}
        env.update(extra)
        h.save(self.folder / "command.json", dict(argv=argv, binary=binary, validation_env=extra))
        self.calls = []
        self.server = self.server_fd = None
        try:
            with (self.folder / "server.log").open("x") as log:
                self.server = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                self.server_fd = os.pidfd_open(self.server.pid)
            end = time.monotonic()+240
            while "serving on port" not in (self.folder / "server.log").read_text():
                self.budget()
                require(self.server.poll() is None and time.monotonic() < end, "model startup failed")
                time.sleep(0.25)
            self.health("startup")
            value = self.http("GET", "/v1/config")
            h.save(self.folder / "configuration.json", value)
            config = json.loads(value["raw"])
            self.config_schema.validate(config)
            require(value["status"] == 200 and config["protocol_source_sha256"] ==
                    self.plan["protocol_sha256"], "configuration identity")
            require([config["limits"][k] for k in ("text_requests", "queued_requests")] ==
                    ([8, 2] if fault else [1, 0]), "effective limit drift")
            require(config["runtime"]["mtp_enabled"] is (not fault), "decode mode drift")
            value = self.http("GET", "/v1/capabilities")
            h.save(self.folder / "capabilities.json", value)
            (self.capacity if fault else self.production)()
            self.health("final")
        finally:
            if self.server is not None:
                if self.server.poll() is None:
                    if self.server_fd is not None:
                        signal.pidfd_send_signal(self.server_fd, signal.SIGTERM)
                    else:
                        self.server.terminate()
                    try:
                        self.server.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        if self.server_fd is not None:
                            signal.pidfd_send_signal(self.server_fd, signal.SIGKILL)
                        else:
                            self.server.kill()
                        self.server.wait(timeout=10)
                if self.server_fd is not None:
                    os.close(self.server_fd)
                for call in self.calls:
                    call.thread.join(timeout=10)
                clean = self.server.returncode == 0 and "shutdown complete (0 in-flight remaining)" in (
                    self.folder / "server.log").read_text()
                h.save(self.folder / "shutdown.json", dict(clean=clean, returncode=self.server.returncode))
                require(clean and all(not call.thread.is_alive() for call in self.calls), "owned shutdown failed")
            stage.a.require_idle_gpu(self.folder / "gpu-after.json")

    def run(self):
        state = dict(status="FAILED")
        try:
            self.model(False)
            self.model(True)
            require(self.counts == dict(generations=10, negatives=4, starts=2), "frozen case missing")
            h.save(self.out / "wire-schema-result.json", dict(passed=True, records=self.wires))
            state["status"] = "PASS_BOUNDED_ADMISSION"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(counts=self.counts, seconds=time.monotonic()-self.started,
                         performance_acceptance=False, automatic_retry=False)
            h.save(self.out / "result.json", state)
