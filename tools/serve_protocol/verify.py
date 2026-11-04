"""Bounded real JS SDK, read-only project tools and explicit stream recovery."""
from dataclasses import asdict
import copy
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time

from tools.serve_protocol import stage
from tools.serve_protocol.js_client import JsSdkClient, JsSdkError
from chat_client import (append_final, append_tool_results, make_request,
                         prepare_tool_calls, require)
from project_query import SYSTEM, validate_answer
from project_records import ProjectRecords, TOOLS, VALIDATORS
from tool_recovery import journal_lock, save_receipt as save, inspect_recovery
from tools.serve_protocol.check_contract import load_validators
from jsonschema import Draft202012Validator

ROOT, MODEL = stage.ROOT, "qwen3.8-flash-next"


def negative_requests():
    base = make_request([dict(role="user", content="q")], [], stream=False, max_tokens=1)
    patches = [("unknown", dict(unknown=True)), ("temperature", dict(temperature=1)),
        ("top-p", dict(top_p=0.5)), ("n", dict(n=2)), ("seed", dict(seed=True)),
        ("repetition", dict(repetition_penalty=1.1)), ("reasoning-field", dict(reasoning_field="other")),
        ("zero-output", dict(max_tokens=0)), ("limits", dict(max_completion_tokens=1)),
        ("stream-options", dict(stream_options=dict(include_usage="yes"))),
        ("user", dict(user=12)), ("null-parse", dict(parse_response=None)),
        ("role", dict(messages=[dict(role="wrong", content="q")])),
        ("message-name", dict(messages=[dict(role="user", content="q", name="name")])),
        ("tools", dict(tools=[dict(type="function", function=dict(name="read", extra=True))])),
        ("stop-constraint", dict(tool_choice="none", stop="END"))]
    return [(name, dict(copy.deepcopy(base), **patch)) for name, patch in patches]


def strict_read_tools(paths):
    tool = copy.deepcopy(TOOLS[0])
    tool["function"]["strict"] = True
    tool["function"]["parameters"]["properties"]["path"] = dict(type="string", enum=paths)
    return [tool]


def metrics(raw):
    result = {}
    for line in raw.splitlines():
        if line and not line.startswith("#"):
            key, number = line.rsplit(" ", 1)
            require(key not in result and math.isfinite(float(number)), "invalid metric")
            result[key] = float(number)
    return result


class CutProxy:
    """One owned loopback TCP connection, cut inside actual answer content.

    No synthetic model bytes. The upstream and forwarded prefixes are both
    retained. Closing the transport is deliberate and never triggers replay.
    """
    def __init__(self, port, folder):
        self.port, self.folder = port, folder
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(10)
        self.address = self.listener.getsockname()
        self.error = None
        self.thread = threading.Thread(target=self.run, name="q4t-owned-cut-proxy", daemon=True)

    def run(self):
        upstream_raw, forwarded, request = bytearray(), b"", bytearray()
        try:
            with self.listener:
                peer, _ = self.listener.accept()
                with peer, socket.create_connection(("127.0.0.1", self.port), timeout=180) as upstream:
                    peer.settimeout(10)
                    while b"\r\n\r\n" not in request:
                        part = peer.recv(4096)
                        require(part and len(request)+len(part) <= 65536, "invalid proxy ingress")
                        request.extend(part)
                    head, _, body = request.partition(b"\r\n\r\n")
                    length = int(re.search(rb"(?im)^content-length: *([0-9]+)\r?$", head)[1])
                    require(length <= 1024*1024, "proxy request budget")
                    while len(body) < length:
                        part = peer.recv(length-len(body))
                        require(part, "truncated ingress")
                        body.extend(part)
                        request.extend(part)
                    require(len(body) == length, "extra proxy request")
                    upstream.sendall(request)
                    while True:
                        part = upstream.recv(65536)
                        require(part, "upstream ended before answer content")
                        upstream_raw.extend(part)
                        require(len(upstream_raw) <= 8*1024*1024, "proxy response budget")
                        match = re.search(rb'"delta":\s*\{\s*"content":\s*"[^"\\]', upstream_raw)
                        if match:
                            forwarded = bytes(upstream_raw[:match.end()])
                            require(forwarded.startswith(b"HTTP/1.1 200 ") and
                                    b"[DONE]" not in forwarded, "not a partial success response")
                            peer.sendall(forwarded)
                            break
        except BaseException as error:
            self.error = error
        finally:
            (self.folder / "proxy-request.bin").write_bytes(request)
            (self.folder / "proxy-upstream.bin").write_bytes(upstream_raw)
            (self.folder / "proxy-forwarded.bin").write_bytes(forwarded)

    def start(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.address[1]}"

    def finish(self):
        self.thread.join(timeout=10)
        require(not self.thread.is_alive(), "proxy did not stop")
        if self.error:
            raise self.error


class Run:
    def __init__(self, plan, output):
        self.plan, self.out = plan, output
        output.mkdir(mode=0o700)
        self.started = time.monotonic()
        self.deadline = plan["started"] + stage.LIMITS["seconds"] - 30
        self.counts = dict(generations=0, negatives=0, starts=0)
        self.port = plan["port"]
        self.base = f"http://127.0.0.1:{self.port}"
        self.client = JsSdkClient(self.base, stage.BASE / "sdk",
                                 protocol_sha256=plan["protocol_sha256"], timeout=180)
        self.server = self.server_fd = None
        self.records = []
        self.schemas = load_validators()
        self.transport = Draft202012Validator(json.loads((ROOT / "docs/serve-transport-v1.schema.json").read_text()))

    def budget(self, kind=None):
        require(time.time() < self.deadline, "original stage deadline")
        if kind:
            require(self.counts[kind] < stage.LIMITS[kind], "stage budget: " + kind)
            self.counts[kind] += 1
            save(self.out / f"attempt-{kind}-{self.counts[kind]:02d}.json",
                 dict(kind=kind, counts=self.counts.copy(), time=time.time()))
        require(all(self.counts[k] <= stage.LIMITS[k] for k in self.counts), "count drift")

    def http(self, method, path, body=None):
        self.budget()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(method, path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(8*1024*1024+1)
            require(len(raw) <= 8*1024*1024, "control response limit")
            return response.status, dict(response.getheaders()), raw.decode()
        finally:
            connection.close()

    def drain(self, label):
        end = time.monotonic()+15
        while True:
            status, _, raw = self.http("GET", "/healthz")
            health = json.loads(raw)
            require(status == 200 and health["gpu_healthy"] and health["seq_slots_total"] > 0,
                    "unhealthy after " + label)
            status, _, raw = self.http("GET", "/metrics")
            require(status == 200, "metrics unavailable")
            rows = metrics(raw)
            if health["seq_slots_free"] == health["seq_slots_total"] and all(rows[k] == 0 for k in (
                "q4t_active_chats", "q4t_structured_active", "q4t_structured_preparing",
                "q4t_structured_vocab_bytes", "q4t_structured_workers_active")):
                save(self.out / (label+"-drained.json"), dict(health=health, metrics=rows,
                    process_status=Path(f"/proc/{self.server.pid}/status").read_text(),
                    meminfo=Path("/proc/meminfo").read_text()))
                return rows
            require(time.monotonic() < end, "resources not released: " + label)
            time.sleep(0.02)

    def check_wire(self, path, *, stream):
        raw = path.read_bytes()
        values = ([json.loads(line[6:]) for line in raw.decode().splitlines()
                   if line.startswith("data: ") and line != "data: [DONE]"]
                  if stream else [json.loads(raw)])
        require(values, "empty validated wire")
        for value in values:
            self.transport.validate(value)
        self.records.append(dict(path=str(path), sha256=stage.h.sha(path), envelopes=len(values)))

    def turn(self, folder, label, request, *, client=None):
        self.schemas["chat"].validate(request)
        self.budget("generations")
        save(folder / (label+"-request.json"), request)
        save(folder / (label+"-submitted.json"), dict(request_id=request["request_id"]))
        begin = time.monotonic()
        try:
            with (folder / (label+"-wire.bin")).open("xb", buffering=0) as wire:
                def capture(part):
                    wire.write(part)
                    os.fsync(wire.fileno())
                result = (client or self.client).complete(request, sink=capture)
            save(folder / (label+"-response.json"), asdict(result))
        except BaseException as error:
            save(folder / "failed.json", dict(error=repr(error), automatic_retry=False,
                completed_tool_calls=len(list(folder.glob("*-tool-*-result.json")))))
            raise
        self.check_wire(folder / (label+"-wire.bin"), stream=request["stream"])
        save(self.out / (folder.name+"-"+label+"-observation.json"), dict(
            seconds=time.monotonic()-begin, response=asdict(result),
            headers=(client or self.client).last_result["result"].get("headers")))
        self.drain(folder.name+"-"+label)
        return result

    def journal(self, name, history, limit=2):
        folder = self.out / name
        folder.mkdir(mode=0o700)
        save(folder / "run-started.json", dict(journal_version=1, base_url=self.base,
            model=MODEL, max_requests=limit, max_tool_calls=8, initial_messages=history,
            stream=True, thinking=False, max_tokens=512))
        save(folder / "capabilities.json", self.caps)
        return folder

    def final(self, folder, label, history, tools, *, stream=True):
        request = make_request(history, tools, stream=stream, max_tokens=512, timeout_ms=180000)
        request["tool_choice"] = "none"
        return self.turn(folder, label, request)

    def commit_final(self, folder, label, history, result, calls, requests):
        require(result.finish_reason == "stop", "missing final answer")
        history = append_final(history, result)
        save(folder / (label+"-history.json"), history)
        save(folder / "completed.json", dict(messages=history, tool_calls=calls, requests=requests))

    def normals(self):
        for stream in (False, True):
            name = "normal-"+("sse" if stream else "json")
            history = [dict(role="developer", content="遵循用户请求。"),
                       dict(role="user", content="只回复数字710003。")]
            folder = self.journal(name, history, 1)
            with journal_lock(folder, create=True):
                body = make_request(history, [], stream=stream, max_tokens=128, timeout_ms=180000)
                result = self.turn(folder, "round-00", body)
                require(result.message["content"].strip() == "710003", "ordinary quality")
                self.commit_final(folder, "round-00", history, result, 0, 1)
            require(inspect_recovery(folder)["state"] == "COMPLETED", "normal journal mismatch")

    def negatives(self):
        folder = self.out / "negatives"
        folder.mkdir()
        rows = negative_requests()
        fixture = stage.h.read(stage.PACKAGE / "cpu/http-negative-contracts.json")
        require([name for name, _ in rows] == fixture["names"], "negative case list drift")
        expected = fixture["native"]
        require(len(expected) == len(rows) and all(not row["accepted"] for row in expected),
                "negative fixture accepted by production probe")
        save(folder / "native-rejections.json", expected)
        before = self.drain("before-negatives")
        for (name, body), value in zip(rows, expected):
            self.budget("negatives")
            save(folder / (name+"-request.json"), body)
            with (folder / (name+"-wire.bin")).open("xb") as wire:
                try:
                    self.client.complete(body, sink=wire.write)
                except JsSdkError as error:
                    save(folder / (name+"-error.json"), error.details)
                    native_error = value["response"]["error"]
                    require(error.details.get("status") == 400 and
                            error.details.get("code") == native_error["code"] and
                            error.details.get("param") == native_error["param"], "HTTP/native rejection mismatch: "+name)
                else:
                    raise AssertionError("negative request succeeded: " + name)
            self.check_wire(folder / (name+"-wire.bin"), stream=False)
        after = self.drain("after-negatives")
        for key in ("q4t_prompt_tokens_total", "q4t_generation_tokens_total", "q4t_structured_workers_started_total"):
            require(before[key] == after[key], "rejected request performed work")

    def tools(self, name, paths, question, *, missing=False):
        tools = strict_read_tools(paths)
        history = [dict(role="system", content=SYSTEM), dict(role="user", content=question)]
        folder = self.journal(name, history)
        with journal_lock(folder, create=True):
            body = make_request(history, tools, stream=True, max_tokens=512, timeout_ms=180000)
            body.update(tool_choice="required", parallel_tool_calls=len(paths) > 1)
            result = self.turn(folder, "round-00", body)
            planned = prepare_tool_calls(result, VALIDATORS)
            require(len(planned) == len(paths) and sorted(p.arguments["path"] for p in planned) == sorted(paths),
                    "missing/duplicate real tool batch")
            require(all(p.arguments["start_line"] == 1 and p.arguments["line_count"] == 24 for p in planned),
                    "unexpected read range")
            save(folder / "round-00-plan.json", [asdict(p) for p in planned])
            results, citations = {}, set()
            for index, invocation in enumerate(planned):
                label = f"round-00-tool-{index:03d}"
                save(folder / (label+"-started.json"), asdict(invocation))
                value = ProjectRecords(ROOT).execute(invocation)
                require(value["ok"] != missing, "tool business outcome differs")
                if missing:
                    require(value["error"]["code"] == "record_not_found", "unexpected business error")
                else:
                    citations.add(value["source"]["citation"])
                value["application_budget"] = dict(remaining_model_requests=1,
                    remaining_tool_calls=0, final_response_required=True)
                content = json.dumps(value, ensure_ascii=False)
                save(folder / (label+"-result.json"), dict(tool_call_id=invocation.call_id, content=content))
                results[invocation.call_id] = content
            # Reverse map insertion order to check ID-based association.
            history = append_tool_results(history, result, dict(reversed(list(results.items()))))
            save(folder / "round-00-history.json", history)
            final = self.final(folder, "round-01", history, tools)
            answer = final.message["content"]
            validate_answer(answer, citations, len(planned))
            if missing:
                require("record_not_found" in answer and any(word in answer for word in ("无法", "不存在", "未找到")),
                        "business error described as success")
            else:
                require(all(citation in answer for citation in citations), "missing source citation")
                if len(paths) == 1:
                    require("Qwen4ExpForConditionalGeneration" in answer, "model architecture fact")
                else:
                    require("qwen4_exp" in answer.lower() and "greedy" in answer.lower(), "multi-source facts")
            self.commit_final(folder, "round-01", history, final, len(planned), 2)
        state = inspect_recovery(folder)
        require(state["state"] == "COMPLETED" and state["completed_tool_calls"] == len(paths), "tool journal mismatch")
        return history, tools, citations, folder

    def recovery(self, checkpoint):
        history, tools, citations, source = checkpoint
        folder = self.journal("interrupted-final", history)
        proxy = CutProxy(self.port, self.out)
        interrupted = JsSdkClient(proxy.start(), stage.BASE / "sdk",
            protocol_sha256=self.plan["protocol_sha256"], timeout=180)
        before = self.drain("before-interruption")
        with journal_lock(folder, create=True):
            request = make_request(history, tools, stream=True, max_tokens=512, timeout_ms=180000)
            request["tool_choice"] = "none"
            try:
                self.turn(folder, "round-00", request, client=interrupted)
            except JsSdkError as error:
                save(self.out / "interruption-error.json", error.details)
            else:
                raise AssertionError("partial stream completed")
            finally:
                proxy.finish()
        state = inspect_recovery(folder)
        require(state["state"] == "REQUEST_OUTCOME_UNKNOWN" and not state["automatic_retry"] and
                state["messages"] == history and state["attempted_tool_calls"] == 0,
                "partial response was committed/replayed")
        raw = (folder / "round-00-wire.bin").read_bytes()
        forwarded = (self.out / "proxy-forwarded.bin").read_bytes().partition(b"\r\n\r\n")[2]
        require(raw and raw == forwarded and b"[DONE]" not in raw, "interruption capture differs")
        after = self.drain("after-interruption")
        require(after["q4t_generation_tokens_total"] > before["q4t_generation_tokens_total"],
                "cut occurred before actual generation")
        save(self.out / "recovery-inspection.json", state)
        # This explicit scenario has no tool execution after the saved complete
        # checkpoint. Server owners/slots are drained; regenerate only its final
        # answer with fresh credentials. Never claim old 404 or cancellation as
        # proof of nonexecution, and never replay a started external tool.
        self.caps = self.client.capabilities()
        resumed = self.journal("explicit-recovery", state["messages"], 1)
        with journal_lock(resumed, create=True):
            result = self.final(resumed, "round-00", state["messages"], tools)
            fresh = json.loads((resumed / "round-00-request.json").read_text())
            require(fresh["request_id"] != request["request_id"] and
                    fresh["cancel_token"] != request["cancel_token"], "reused recovery credentials")
            validate_answer(result.message["content"], citations, 1)
            require("Qwen4ExpForConditionalGeneration" in result.message["content"], "recovered fact")
            self.commit_final(resumed, "round-00", history, result, 0, 1)
        require(inspect_recovery(resumed)["state"] == "COMPLETED", "recovery journal mismatch")
        save(self.out / "explicit-recovery-decision.json", dict(automatic_retry=False,
            source_checkpoint=stage.h.identity(source / "round-00-history.json"),
            failed_request=stage.h.identity(folder / "round-00-request.json"),
            fresh_request=stage.h.identity(resumed / "round-00-request.json"),
            new_tools_executed=0, original_owners_and_slots_drained=True,
            scope="Explicit new final answer from known complete tool results; no unknown tool replay"))

    def model(self):
        self.budget("starts")
        argv = [self.plan["binary"]["path"], "serve", "--model-dir", self.plan["model_dir"],
                "--host", "127.0.0.1", "--port", str(self.port), "--max-seq", "1",
                "--max-prefill", "2048", "--max-len", "8192"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("Q4T_") and
               k not in ("LD_PRELOAD", "NODE_OPTIONS", "NODE_PATH")}
        save(self.out / "command.json", dict(argv=argv, binary=self.plan["binary"]))
        with (self.out / "server.log").open("x") as log:
            self.server = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            self.server_fd = os.pidfd_open(self.server.pid)
            end = time.monotonic()+240
            while "serving on port" not in (self.out / "server.log").read_text():
                self.budget()
                require(self.server.poll() is None and time.monotonic() < end, "startup failed")
                time.sleep(0.5)
        self.caps = self.client.capabilities()
        save(self.out / "capabilities.json", self.caps)
        require(self.caps["runtime"]["mtp_enabled"], "default MTP expected")
        models = self.client.models()
        save(self.out / "models.json", models)
        spec = json.loads((ROOT / "docs/serve-openapi-v1.json").read_text())
        for path in ("/v1/config", "/healthz", "/livez", "/readyz"):
            status, headers, raw = self.http("GET", path)
            save(self.out / (path.replace("/", "_")+".json"), dict(status=status, headers=headers, raw=raw))
            require(status == 200, "discovery failed: " + path)
            schema = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
            self.schemas["chat"].evolve(schema=schema).validate(json.loads(raw))
        self.schemas["chat"].evolve(schema=spec["paths"]["/v1/models"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]).validate(models)

    def run(self):
        state = dict(status="FAILED")
        try:
            stage.sdk_identity()  # Fail before starting a model if SDK is absent.
            self.model()
            self.negatives()
            self.normals()
            checkpoint = self.tools("read-success", ["docs/MODEL.md"],
                "先调用一次read_project_record读取docs/MODEL.md的第1到24行，start_line=1、line_count=24。"
                "再回答architectures字段的准确值，并附工具给出的引用。")
            self.tools("read-business-error", ["docs/SRV08-intentionally-absent.md"],
                "先调用一次read_project_record读取docs/SRV08-intentionally-absent.md，第1到24行，"
                "start_line=1、line_count=24。然后根据实际结果说明能否核实，失败必须给出准确错误码。", missing=True)
            self.tools("read-two-sources", ["docs/MODEL.md", "docs/ARCHITECTURE.md"],
                "请在同一个响应中发出恰好两个read_project_record调用，分别读取docs/MODEL.md和"
                "docs/ARCHITECTURE.md，各自start_line=1、line_count=24。两个文件都必须读取，不能只选一个。"
                "收到两个结果后，简短回答模型model_type与当前生成是否greedy，并分别附准确引用。")
            self.recovery(checkpoint)
            self.drain("final")
            require(self.counts == dict(generations=10, negatives=16, starts=1), "missing frozen case")
            save(self.out / "wire-schema-result.json", dict(passed=True, records=self.records,
                intentional_partial="interrupted-final/round-00-wire.bin; rejected by client, no success receipt"))
            state["status"] = "PASS_BOUNDED_PROTOCOL_CLIENT"
        except BaseException as error:
            state["error"] = repr(error)
            raise
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
                clean = self.server.returncode == 0 and "shutdown complete (0 in-flight remaining)" in (self.out / "server.log").read_text()
                save(self.out / "shutdown.json", dict(clean=clean, returncode=self.server.returncode))
                if not clean:
                    state.update(status="FAILED", shutdown_error="unclean shutdown")
            state.update(counts=self.counts, seconds=time.monotonic()-self.started,
                         default_replaced=False, performance_acceptance=False)
            save(self.out / "result.json", state)
            require(state["status"] == "PASS_BOUNDED_PROTOCOL_CLIENT", "protocol stage failed")
