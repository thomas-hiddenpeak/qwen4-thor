"""Actual TCP backpressure after strict validation; no synthetic model output."""
from dataclasses import asdict
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
import sys
import threading
import time

from tools.serve_backpressure import stage

sys.path.insert(0, str(stage.ROOT / "tools/examples"))
from chat_client import (ChatClient, ProtocolError, make_request, read_json,
                         read_sse, require)
from tools.serve_protocol.check_contract import load_validators

ROOT, h = stage.ROOT, stage.h
EMPTY = ("q4t_active_chats", "q4t_structured_active", "q4t_structured_preparing",
         "q4t_structured_vocab_bytes", "q4t_structured_workers_active",
         "q4t_request_body_bytes")


def metrics(raw):
    result = {}
    for line in raw.splitlines():
        if line and not line.startswith("#"):
            key, value = line.rsplit(" ", 1)
            require(key not in result and math.isfinite(float(value)), "invalid metric")
            result[key] = float(value)
    return result


def short_request(strict=False):
    body = make_request([dict(role="user", content=("只回复数字710003。" if not strict
                         else "仅输出符合schema的JSON。"))], [],
                        stream=False, max_tokens=128, timeout_ms=180000)
    if strict:
        body["response_format"] = dict(type="json_schema", json_schema=dict(
            name="recovery", strict=True, schema=dict(type="object",
            properties=dict(ok=dict(type="boolean", enum=[True])), required=["ok"],
            additionalProperties=False)))
    return body


def slow_socket(port, body):
    sock = socket.socket()
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        sock.settimeout(15)
        sock.connect(("127.0.0.1", port))
        payload = json.dumps(body, ensure_ascii=False).encode()
        sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                     b"Content-Type: application/json\r\nContent-Length: " +
                     str(len(payload)).encode() + b"\r\n\r\n" + payload)
        return sock
    except BaseException:
        sock.close()
        raise


def partial_contract(raw, stream):
    require(raw.startswith(b"HTTP/1.1 200 ") and raw.count(b"HTTP/1.1 ") == 1,
            "unexpected status or second HTTP response")
    head, separator, body = raw.partition(b"\r\n\r\n")
    require(separator and body, "missing partial response body")
    require(b"[DONE]" not in body and b'"usage":{' not in body and
            not re.search(rb'"finish_reason"\s*:\s*"', body),
            "partial response contains success terminal")
    # A partial content prefix proves the observed stall is after Finish's
    # whole-response validation, not an SSE heartbeat/header write.
    require(b'"content"' in body and b"====" in body, "no strict content prefix")
    if not stream:
        match = re.search(rb"(?im)^content-length: *([0-9]+)\r?$", head)
        require(match and len(body) < int(match[1]), "JSON is not truncated")
    try:
        (read_sse if stream else read_json)(io.BytesIO(body))
    except (ProtocolError, ValueError) as error:
        return dict(bytes=len(raw), body_bytes=len(body), client_rejected=repr(error))
    raise AssertionError("partial response was accepted as a complete turn")


class Run:
    def __init__(self, plan, output):
        self.plan, self.out = plan, output
        output.mkdir(mode=0o700)
        self.started = time.monotonic()
        self.deadline = plan["started"] + stage.LIMITS["seconds"] - 30
        self.counts = dict(generations=0, negatives=0, starts=0)
        self.port = plan["port"]
        self.server = self.server_fd = None
        self.thread = None
        self.schemas = load_validators()
        self.cases = []

    def budget(self, kind=None):
        require(time.time() < self.deadline, "original stage deadline")
        if kind:
            require(self.counts[kind] < stage.LIMITS[kind], "stage budget: " + kind)
            self.counts[kind] += 1
            h.save(self.out / f"attempt-{kind}-{self.counts[kind]:02d}.json",
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def http(self, method, path, body=None):
        self.budget()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method, path, None if body is None else json.dumps(body),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read(1024*1024+1)
            require(len(raw) <= 1024*1024, "control response limit")
            return dict(status=response.status, headers=dict(response.getheaders()),
                        raw=raw.decode())
        finally:
            conn.close()

    def health(self, folder, label):
        for path in ("/healthz", "/readyz"):
            value = self.http("GET", path)
            h.save(folder / (label+path.replace("/", "-")+".json"), value)
            require(value["status"] == 200, "health/control unavailable")
        return json.loads(value["raw"])

    def memory(self):
        # Separate raw RSS values; shared pages are not added as unique RAM.
        rows, children = {}, set()
        for path in Path(f"/proc/{self.server.pid}/task").glob("*/children"):
            try:
                children.update(int(pid) for pid in path.read_text().split())
            except FileNotFoundError:
                pass
        for pid in [self.server.pid, *sorted(children)]:
            try:
                rows[str(pid)] = Path(f"/proc/{pid}/status").read_text()
            except FileNotFoundError:
                rows[str(pid)] = "process exited during sample"
        return dict(monotonic=time.monotonic(), processes=rows,
                    meminfo=Path("/proc/meminfo").read_text())

    def snapshot(self, folder, label):
        value = self.http("GET", "/metrics")
        require(value["status"] == 200, "metrics unavailable")
        rows = metrics(value["raw"])
        h.save(folder / (label+"-resources.json"), dict(http=value, metrics=rows,
                                                     memory=self.memory()))
        return rows

    def drain(self, folder, label, timeout=45):
        end = time.monotonic()+timeout
        samples = []
        while True:
            value = self.http("GET", "/metrics")
            require(value["status"] == 200, "metrics unavailable")
            rows = metrics(value["raw"])
            samples.append(dict(monotonic=time.monotonic(), metrics=rows))
            if all(rows[key] == 0 for key in EMPTY) and rows["q4t_seq_slots_free"] == 1:
                break
            require(self.server.poll() is None and time.monotonic() < end,
                    "resources not released")
            time.sleep(0.2)
        h.save(folder / (label+"-drain-samples.json"), samples)
        require(rows["q4t_structured_workers_started_total"] ==
                rows["q4t_structured_workers_reaped_total"], "worker not reaped")
        return self.snapshot(folder, label)

    def complete(self, folder, label, body, holder):
        start = time.monotonic()
        try:
            client = ChatClient(f"http://127.0.0.1:{self.port}")
            with (folder / (label+"-wire.bin")).open("xb", buffering=0) as wire:
                def capture(part):
                    wire.write(part)
                    os.fsync(wire.fileno())
                result = client.complete(body, sink=capture)
            holder["result"] = result
            h.save(folder / (label+"-result.json"), dict(result=asdict(result),
                seconds=time.monotonic()-start, completed=time.monotonic()))
        except BaseException as error:
            holder["error"] = error
            h.save(folder / (label+"-failed.json"), dict(error=repr(error)))

    def request(self, folder, label, body):
        self.schemas["chat"].validate(body)
        self.budget("generations")
        h.save(folder / (label+"-request.json"), body)

    def reject(self, folder, label, path, body, status, code):
        self.budget("negatives")
        h.save(folder / (label+"-request.json"), body)
        value = self.http("POST", path, body)
        h.save(folder / (label+"-response.json"), value)
        require(value["status"] == status and
                json.loads(value["raw"])["error"]["code"] == code, label+" mismatch")

    def case(self, folder, stream):
        body = h.read(stage.PACKAGE / "payload-array" /
                      ("sse-request.json" if stream else "json-request.json"))
        self.request(folder, "long", body)
        submitted = time.monotonic()
        sock = slow_socket(self.port, body)
        gate = folder / "backpressure"
        queued = {}
        samples = []
        try:
            end = submitted + 170
            next_sample = 0
            while not gate.with_suffix(".entered").exists():
                self.budget()
                if time.monotonic() >= next_sample:
                    samples.append(self.memory())
                    next_sample = time.monotonic()+1
                require(self.server.poll() is None and time.monotonic() < end,
                        "strict response did not reach real EAGAIN")
                time.sleep(0.02)
            entered = time.monotonic()
            log = (folder / "server.log").read_text()
            h.save(folder / "eagain.json", dict(monotonic=entered, seconds=entered-submitted,
                server_log=log, actual_receive_buffer=sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)))
            require(re.search(r"id="+re.escape(body["request_id"])+
                    r" event=stage stage=response ", log), "stall preceded response validation")
            rows = self.snapshot(folder, "blocked")
            require(rows["q4t_structured_active"] == 1 and
                    rows["q4t_structured_workers_active"] == 1 and
                    rows["q4t_seq_slots_free"] == 0 and
                    rows["q4t_generation_tokens_total"] > 0, "strict ownership missing")
            normal = short_request()
            self.request(folder, "queued", normal)
            self.thread = threading.Thread(target=self.complete,
                args=(folder, "queued", normal, queued), daemon=True)
            self.thread.start()
            queue_end = time.monotonic()+3
            while True:
                rows = metrics(self.http("GET", "/metrics")["raw"])
                if rows["q4t_active_chats"] == 2:
                    break
                require(time.monotonic() < queue_end and not queued, "normal did not queue")
                time.sleep(0.01)
            require(not queued and rows["q4t_seq_slots_free"] == 0, "normal completed before unblock")
            self.snapshot(folder, "queued")
            self.reject(folder, "strict-busy", "/v1/chat/completions", short_request(True),
                        503, "structured_capacity_exhausted")
            self.health(folder, "blocked")
            wrong = dict(request_id=body["request_id"], cancel_token="0"*64)
            require(wrong["cancel_token"] != body["cancel_token"], "invalid negative credential")
            self.reject(folder, "wrong-cancel", "/v1/requests/cancel", wrong,
                        404, "request_not_found")
            if stream:
                cancel = self.http("POST", "/v1/requests/cancel",
                    {k: body[k] for k in ("request_id", "cancel_token")})
                h.save(folder / "cancel.json", cancel)
                require(cancel["status"] == 202, "cancel not accepted")
            require(time.monotonic()-entered < 8, "handshake consumed 10s gate budget")
            gate.with_suffix(".release").touch(exist_ok=False)
            released = time.monotonic()
            # Keep the original peer unread and open until all owners drain.
            # Reading earlier would relieve backpressure and invalidate timeout.
            drained = self.drain(folder, "after-blocked", timeout=42)
            cleaned = time.monotonic()
            if not stream:
                require(25 <= cleaned-entered <= 42 and cleaned-submitted < 180,
                        "not the 30-second write deadline before request expiry")
            require(drained['q4t_requests_aborted_by_reason_total{reason="'+
                    ("explicit_cancel" if stream else "client_disconnected")+'"}'] == 1,
                    "unexpected abort reason")
            require(drained['q4t_requests_aborted_by_reason_total{reason="deadline_exceeded"}'] == 0,
                    "request expired instead of write termination")
            received = time.monotonic()
            raw = bytearray()
            with (folder / "long-wire.bin").open("xb", buffering=0) as wire:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    raw.extend(chunk)
                    wire.write(chunk)
                    os.fsync(wire.fileno())
                    require(len(raw) <= 1024*1024, "unexpected long response size")
            proof = partial_contract(bytes(raw), stream)
            self.thread.join(timeout=5)
            require(not self.thread.is_alive() and "error" not in queued and
                    "result" in queued, "queued request failed")
            require(queued["result"].finish_reason == "stop" and
                    queued["result"].message["content"].strip() == "710003", "ordinary quality")
            strict = short_request(True)
            self.request(folder, "strict-recovery", strict)
            holder = {}
            self.complete(folder, "strict-recovery", strict, holder)
            require("error" not in holder and holder["result"].finish_reason == "stop" and
                    json.loads(holder["result"].message["content"]) == {"ok": True},
                    "strict recovery failed")
            final = self.drain(folder, "final", timeout=15)
            require(final["q4t_requests_success_total"] == 2 and
                    final["q4t_requests_aborted_total"] == 1, "terminal accounting mismatch")
            self.health(folder, "final")
            value = dict(stream=stream, submitted=submitted, eagain=entered,
                released=released, cleaned=cleaned, first_read=received,
                blocked_seconds=cleaned-entered, generation_to_eagain_seconds=entered-submitted,
                partial=proof, final_metrics=final, passed=True)
            h.save(folder / "case-result.json", value)
            self.cases.append(value)
        finally:
            sock.close()
            h.save(folder / "generation-memory-samples.json", samples)
            if gate.with_suffix(".entered").exists() and not gate.with_suffix(".release").exists():
                gate.with_suffix(".release").touch(exist_ok=False)

    def model(self, stream):
        self.budget("starts")
        folder = self.out / ("sse-cancel" if stream else "json-write-timeout")
        folder.mkdir()
        argv = [self.plan["binary"]["path"], "serve", "--model-dir", self.plan["model_dir"],
                "--host", "127.0.0.1", "--port", str(self.port), "--max-seq", "1",
                "--max-prefill", "2048", "--max-len", "8192"]
        env = {k:v for k,v in os.environ.items() if not k.startswith("Q4T_") and k != "LD_PRELOAD"}
        extra = dict(Q4T_SERVE_TEST_SNDBUF="1", Q4T_SERVE_PAUSE="write.backpressure",
                     Q4T_SERVE_GATE=str(folder / "backpressure"))
        env.update(extra)
        h.save(folder / "command.json", dict(argv=argv, binary=self.plan["binary"], validation_env=extra))
        self.server = self.server_fd = self.thread = None
        try:
            with (folder / "server.log").open("x") as log:
                self.server = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                self.server_fd = os.pidfd_open(self.server.pid)
            end = time.monotonic()+240
            while "serving on port" not in (folder / "server.log").read_text():
                self.budget()
                require(self.server.poll() is None and time.monotonic() < end, "startup failed")
                time.sleep(0.5)
            # Avoid a large discovery/metrics write consuming the first-EAGAIN
            # hook before the deliberately unread long response owns it.
            self.health(folder, "startup")
            self.case(folder, stream)
        finally:
            if self.server is not None:
                if self.server.poll() is None:
                    signal.pidfd_send_signal(self.server_fd, signal.SIGTERM)
                    try:
                        self.server.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        signal.pidfd_send_signal(self.server_fd, signal.SIGKILL)
                        self.server.wait(timeout=10)
                if self.server_fd is not None:
                    os.close(self.server_fd)
                if self.thread:
                    self.thread.join(timeout=5)
                clean = self.server.returncode == 0 and "shutdown complete (0 in-flight remaining)" in (folder / "server.log").read_text()
                h.save(folder / "shutdown.json", dict(clean=clean, returncode=self.server.returncode))
                require(clean, "unclean owned shutdown")
                require(not self.thread or not self.thread.is_alive(), "owned client did not stop")
            stage.a.require_idle_gpu(folder / "gpu-after.json")

    def run(self):
        state = dict(status="FAILED")
        try:
            self.model(True)
            self.model(False)
            require(self.counts == dict(generations=6, negatives=4, starts=2), "missing frozen case")
            state.update(status="PASS_BOUNDED_STRICT_BACKPRESSURE", cases=self.cases)
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(counts=self.counts, seconds=time.monotonic()-self.started,
                         default_replaced=False, performance_acceptance=False)
            h.save(self.out / "result.json", state)
