"""Bounded Thor transport checks, real project tools, proxy and TCP backpressure."""
import concurrent.futures
from dataclasses import asdict
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "transport_resources", ROOT / "tools/serve_resources/verify.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
s, wire = r.s, r.wire


def events(raw):
    data = [line[6:] for line in raw.decode().splitlines() if line.startswith("data: ")]
    wire.require(data and data[-1] == "[DONE]" and data.count("[DONE]") == 1,
                 "SSE needs exactly one final DONE")
    return [json.loads(item) for item in data[:-1]]


def nullable_contract(values, streaming, include_usage=True):
    identities = set()
    for value in values:
        wire.require("error" not in value, "unexpected stream error")
        identities.add((value["id"], value["model"], value["created"]))
        for choice in value["choices"]:
            wire.require("logprobs" in choice and choice["logprobs"] is None,
                         "missing explicit null logprobs")
        if streaming and value["choices"] and include_usage:
            wire.require("usage" in value and value["usage"] is None,
                         "missing explicit null stream usage")
    wire.require(len(identities) == 1, "completion identity changed")


def error_contract(raw, code, param=None):
    body = json.loads(raw)
    wire.require(set(body) == {"error"}, "mixed error and success")
    value = body["error"]
    wire.require(set(value) == {"message", "type", "code", "param"} and
                 value["code"] == code and value["param"] == param,
                 "error location/code mismatch")
    return body


class IdleProxy:
    """One owned loopback connection; enforce seven-second upstream idle timeout."""
    def __init__(self, target, request):
        self.target, self.request = target, request
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(15)
        self.address = self.listener.getsockname()
        self.heartbeat = threading.Event()
        self.times = []
        self.raw = bytearray()

    def run(self):
        with self.listener:
            peer, _ = self.listener.accept()
            with peer, socket.create_connection(self.target, timeout=15) as upstream:
                peer.settimeout(15)
                request = bytearray()
                while len(request) < len(self.request):
                    part = peer.recv(len(self.request) - len(request))
                    wire.require(part, "proxy request truncated")
                    request.extend(part)
                wire.require(bytes(request) == self.request, "proxy changed input")
                upstream.sendall(request)
                upstream.settimeout(7)
                while True:
                    part = upstream.recv(65536)
                    if not part:
                        break
                    self.times.append(time.monotonic())
                    self.raw.extend(part)
                    wire.require(len(self.raw) <= 2 * 1024**2, "proxy capture budget")
                    peer.sendall(part)
                    if b": keep-alive\r\n\r\n" in self.raw:
                        self.heartbeat.set()


class Run(r.Run):
    def budget(self):
        wire.require(time.time() < self.deadline, "stage A deadline")
        wire.require(self.requests <= (2 if self.installed else 10) and
                     self.negative <= (0 if self.installed else 16) and
                     self.starts <= (1 if self.installed else 2) and
                     self.capability_gets <= (1 if self.installed else 2),
                     "stage A request/start budget")

    def exchange(self, folder, label, request):
        self.budget()
        (folder / (label + "-request.bin")).write_bytes(request)
        with socket.create_connection(("127.0.0.1", self.args.port), timeout=15) as peer:
            peer.sendall(request)
            raw = self.receive(peer)
        (folder / (label + "-response.bin")).write_bytes(raw)
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = dict(line.lower().split(": ", 1) for line in lines[1:])
        wire.require(re.fullmatch(r"[a-z0-9_.:-]{1,128}", headers.get("x-request-id", "")),
                     "missing safe response trace ID")
        return int(lines[0].split()[1]), headers, body

    def sdk_chat(self, client, folder, label, request, **unused):
        self.counted()
        request = dict(request, extra_headers={"X-Request-Id": "trace-" + label})
        wire.save(folder / (label + "-request.json"), request)
        begin = time.monotonic()
        streaming = request.get("stream", False)
        if streaming:
            raw = bytearray()
            with client.chat.completions.create(**request) as stream:
                headers = dict(stream.response.headers)
                original = stream.response.iter_bytes
                def capture(*args, **kwargs):
                    for part in original(*args, **kwargs):
                        raw.extend(part)
                        yield part
                with patch.object(stream.response, "iter_bytes", capture):
                    sdk = [chunk.model_dump(mode="json") for chunk in stream]
            values = events(raw)
            result = wire.read_sse(io.BytesIO(raw))
        else:
            response = client.chat.completions.with_raw_response.create(**request)
            headers = dict(response.http_response.headers)
            raw = response.http_response.content
            parsed = response.parse()
            sdk = parsed.model_dump(mode="json")
            wire.require(parsed._request_id == "trace-" + label, "SDK request ID missing")
            values = [json.loads(raw)]
            result = wire.read_json(io.BytesIO(raw))
        (folder / (label + "-wire.bin")).write_bytes(raw)
        wire.save(folder / (label + "-headers.json"), headers)
        wire.save(folder / (label + "-sdk.json"), sdk)
        wire.save(folder / (label + "-result.json"), dict(**asdict(result),
                  seconds=time.monotonic() - begin))
        wire.require(headers["x-request-id"] == "trace-" + label and
                     result.id != headers["x-request-id"], "trace/cancel IDs conflated")
        nullable_contract(values, streaming)
        self.wait_slot(label)
        return result

    def success(self, folder, label, body):
        with self.sdk() as client:
            return self.sdk_chat(client, folder, label, body)

    def transport(self, folder):
        cases = [
            ("method", b"GET /v1/chat/completions", b"", b"", 405, "method_not_allowed", "method"),
            ("query", b"GET /readyz?load=1", b"", b"", 400, "unsupported_query", "path"),
            ("media", b"POST /v1/chat/completions", b"Content-Type: text/plain\r\n", b"{}", 415, "unsupported_media_type", "Content-Type"),
            ("missing-media", b"POST /v1/requests/cancel", b"", b"{}", 415, "unsupported_media_type", "Content-Type"),
            ("duplicate-id", b"GET /readyz", b"X-Request-Id: second\r\n", b"", 400, "invalid_request_id_header", "X-Request-Id"),
            ("unknown", b"GET /missing", b"", b"", 404, "not_found", None),
            ("get-body", b"GET /readyz", b"", b"{}", 400, "unexpected_body", "body"),
            ("parameter", b"POST /v1/chat/completions", b"Content-Type: application/json\r\n", b'{"messages":[],"temperature":"bad"}', 400, "invalid_request", "temperature")
        ]
        for label, first, extra, body, status, code, param in cases:
            self.counted(negative=True)
            request = first + b" HTTP/1.1\r\nHost: localhost\r\nX-Request-Id: transport-test\r\n" + extra
            request += b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            actual, headers, raw = self.exchange(folder, label, request)
            wire.require(actual == status, "wrong HTTP status: " + label)
            error_contract(raw, code, param)
            if label != "duplicate-id":
                wire.require(headers["x-request-id"] == "transport-test", "trace not echoed")
            if status == 405:
                wire.require(headers.get("allow") == "post", "missing Allow")
        code, _, raw = self.exchange(folder, "ready", b"GET /readyz HTTP/1.1\r\nHost: localhost\r\n\r\n")
        wire.require(code == 200 and json.loads(raw)["ready"], "model not ready")

    def proxy_cancel(self, folder):
        self.counted()
        body = r.long_request("transport-proxy")
        payload = json.dumps(body).encode()
        request = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                   b"Content-Type: application/json\r\nX-Request-Id: proxy-trace\r\nContent-Length: " +
                   str(len(payload)).encode() + b"\r\n\r\n" + payload)
        wire.save(folder / "proxy-request.json", body)
        proxy = IdleProxy(("127.0.0.1", self.args.port), request)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            forwarded = pool.submit(proxy.run)
            try:
                with socket.create_connection(proxy.address, timeout=15) as peer:
                    peer.sendall(request)
                    wire.require(proxy.heartbeat.wait(12), "no heartbeat through seven-second idle proxy")
                    self.counted(negative=True)
                    busy = s.request("只输出JSON对象", response_format=dict(type="json_object"))
                    busy.update(busy.pop("extra_body"))
                    code, raw = self.request("POST", "/v1/chat/completions", busy)
                    wire.save(folder / "busy.json", dict(status=code, raw=raw))
                    wire.require(code == 503, "structured admission not bounded")
                    error_contract(raw, "structured_capacity_exhausted")
                    ordinary = pool.submit(self.success, folder, "queued-ordinary",
                                           s.request("只回复数字710003。"))
                    self.observe(lambda rows: rows["q4t_active_chats"] >= 2)
                    code, raw = self.request("GET", "/readyz")
                    wire.save(folder / "ready-under-load.json", dict(status=code, raw=raw))
                    wire.require(code == 200 and json.loads(raw)["ready"], "capacity is not readiness failure")
                    code, raw = self.request("POST", "/v1/requests/cancel",
                        dict(request_id="transport-proxy", cancel_token="a" * 64))
                    wire.save(folder / "proxy-cancel.json", dict(status=code, raw=raw))
                    wire.require(code == 202, "control cancellation unavailable")
                    response = self.receive(peer)
                    (folder / "proxy-response.bin").write_bytes(response)
                    r.cancellation_wire(response, "explicit_cancel")
                    wire.require(ordinary.result(timeout=120).message["content"].strip() == "710003",
                                 "queued ordinary quality")
                forwarded.result(timeout=15)
            except BaseException:
                self.stop()
                raise
        wire.require(bytes(proxy.raw) == response, "proxy/client bytes differ")
        wire.save(folder / "proxy-timing.json", dict(idle_timeout_seconds=7,
            chunks=len(proxy.times), heartbeat_received=proxy.heartbeat.is_set(),
            max_observed_gap_seconds=max((b-a for a,b in zip(proxy.times, proxy.times[1:])), default=0)))
        self.wait_slot("proxy drained")

    def deadline_case(self, folder):
        self.counted()
        body = r.long_request("transport-deadline")
        body["request_timeout_ms"] = 1
        payload = json.dumps(body).encode()
        status, _, raw = self.exchange(folder, "deadline", b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
        r.cancellation_wire(b"HTTP/1.1 " + str(status).encode() + b" result\r\n\r\n" + raw,
                            "deadline_exceeded")
        self.wait_slot("deadline drained")

    def resources_empty(self, folder, label):
        # A response/slot release can precede handler and worker destruction.
        self.observe(lambda rows: all(rows[key] == 0 for key in (
            "q4t_active_chats", "q4t_structured_active", "q4t_structured_preparing",
            "q4t_structured_vocab_bytes", "q4t_structured_workers_active")))
        rows, text = self.snapshot(folder, label)
        r.metric_contract(text)
        wire.require(all(rows[key] == 0 for key in ("q4t_active_chats", "q4t_structured_active",
            "q4t_structured_preparing", "q4t_structured_vocab_bytes", "q4t_structured_workers_active")),
            "request resources retained")
        wire.require(rows["q4t_structured_workers_started_total"] ==
                     rows["q4t_structured_workers_reaped_total"], "worker not reaped")

    def business(self, folder):
        self.transport(folder)
        wire.require(self.success(folder, "ordinary", s.request("只回复数字710003。")).message[
            "content"].strip() == "710003", "ordinary quality")
        self.project_loop(folder, strict=True, stream=True)
        self.proxy_cancel(folder)
        self.deadline_case(folder)
        self.resources_empty(folder, "before-shutdown")
        self.shutdown_case(folder)

    def backpressure(self, folder):
        body = r.long_request("transport-slow")
        self.counted()
        wire.save(folder / "slow-request.json", body)
        sock = socket.socket()
        # Set before connect, so the SYN already advertises the small window.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        sock.settimeout(15)
        sock.connect(("127.0.0.1", self.args.port))
        payload = json.dumps(body).encode()
        sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                     b"Content-Type: application/json\r\nContent-Length: " +
                     str(len(payload)).encode() + b"\r\n\r\n" + payload)
        gate = folder / "backpressure"
        try:
            end = time.monotonic() + 180
            while not gate.with_suffix(".entered").exists():
                self.budget()
                wire.require(self.server.poll() is None and time.monotonic() < end,
                             "real TCP EAGAIN was not observed")
                time.sleep(0.02)
            code, raw = self.request("POST", "/v1/requests/cancel",
                dict(request_id="transport-slow", cancel_token="a" * 64))
            wire.save(folder / "slow-cancel.json", dict(status=code, raw=raw))
            wire.require(code == 202, "slow writer blocks control cancellation")
            gate.with_suffix(".release").touch(exist_ok=False)
            response = self.receive(sock)
            (folder / "slow-response.bin").write_bytes(response)
            wire.require(b"[DONE]" not in response and b'"finish_reason":"stop"' not in response,
                         "failed partial write published success/terminal")
        finally:
            sock.close()
        self.wait_slot("slow drained")
        wire.require(self.success(folder, "after-slow", s.request("只回复数字710003。")).message[
            "content"].strip() == "710003", "recovery after slow reader")
        self.resources_empty(folder, "after-slow")

    def model(self, name, binary, mtp, action, *, fault=False):
        self.starts += 1
        self.budget()
        folder = self.out / name
        folder.mkdir(mode=0o700)
        command = [str(binary), "serve", "--model-dir", str(self.args.model_dir),
            "--host", "127.0.0.1", "--port", str(self.args.port), "--max-seq", "1",
            "--max-prefill", "2048", "--max-len", "8192"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("Q4T_") and k != "LD_PRELOAD"}
        extra = dict(Q4T_SERVE_TEST_SNDBUF="1", Q4T_SERVE_PAUSE="write.backpressure",
                     Q4T_SERVE_GATE=str(folder / "backpressure")) if fault else {}
        env.update(extra)
        wire.save(folder / "command.json", dict(argv=command, sha256=wire.digest(binary), validation_env=extra))
        self.server = self.server_fd = None
        try:
            with (folder / "server.log").open("x") as log:
                self.server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                self.server_fd = os.pidfd_open(self.server.pid)
                end = time.monotonic() + 240
                while "serving on port" not in (folder / "server.log").read_text():
                    self.budget()
                    wire.require(self.server.poll() is None and time.monotonic() < end, "server startup failed")
                    time.sleep(0.5)
                code, raw = self.request("GET", "/v1/capabilities")
                wire.save(folder / "capabilities.json", dict(status=code, raw=raw))
                caps = wire.validate_capabilities(json.loads(raw), "qwen3.8-flash-next",
                                                  protocol_sha256=self.args.protocol_sha256)
                wire.require(code == 200 and caps["http"]["request_id_header"] == "X-Request-Id",
                             "capability mismatch")
                action(folder)
        finally:
            if self.server is not None and self.server.poll() is None:
                self.stop() if self.server_fd is not None else self.server.terminate()
                try:
                    self.server.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    if self.server_fd is not None:
                        signal.pidfd_send_signal(self.server_fd, signal.SIGKILL)
                    self.server.wait(timeout=10)
            if self.server_fd is not None:
                os.close(self.server_fd)
            clean = self.server is not None and self.server.returncode == 0 and (
                "shutdown complete (0 in-flight remaining)" in (folder / "server.log").read_text())
            wire.save(folder / "shutdown.json", dict(clean=clean,
                returncode=self.server.returncode if self.server else None))
            wire.require(clean, "unclean shutdown")

    def run(self):
        state = dict(status="FAILED")
        try:
            if self.installed:
                def health(folder):
                    self.health(folder)
                    self.resources_empty(folder, "installed-drained")
                self.model("candidate-installed", ROOT / "build/q4t", True, health)
            else:
                self.model("candidate-mtp", Path(self.plan["candidate"]), True, self.business)
                self.model("candidate-fault", Path(self.plan["fault_candidate"]["path"]), True,
                           self.backpressure, fault=True)
            wire.require(self.requests == (2 if self.installed else 9) and
                         self.negative == (0 if self.installed else 9), "missing frozen cases")
            subprocess.run(["/usr/bin/python3", "-B", str(Path(__file__).with_name("check_schema.py")),
                            str(self.out)], cwd=ROOT, check=True, timeout=30)
            state["status"] = "PASS_BOUNDED_SERVE_STAGE_A"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(request_attempts=self.requests, negative_requests=self.negative,
                         starts=self.starts, seconds=time.monotonic() - self.started,
                         performance_acceptance=False)
            wire.save(self.out / "result.json", state)
