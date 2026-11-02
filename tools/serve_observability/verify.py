"""Bounded real transport observations and project tools; no automatic replay.

Ordinary incremental TCP backpressure is separate from SRV-03b's unaccepted
strict long-payload case. Fault builds only shrink the socket buffer and pause
once at actual EAGAIN. They neither inject model output nor become defaults.
"""
import json
import os
import re
import signal
import socket
import struct
import subprocess
import time

from tools.serve_admission import verify as base
from tools.serve_observability import stage
from chat_client import make_request, require

h, ROOT = stage.h, stage.ROOT
RECEIVE = ("malformed", "header_too_large", "body_too_large", "body_capacity",
    "header_deadline", "body_deadline", "header_eof", "body_eof", "socket_error",
    "admission", "connection_capacity", "server_draining", "unsupported_media",
    "method", "query", "unexpected_body", "route", "exception")
WRITE = ("write_deadline", "request_deadline", "explicit_cancel", "peer_closed",
         "server_shutdown", "request_failure", "socket_error", "exception")


def receive_key(reason):
    return f'q4t_http_receive_failures_total{{reason="{reason}"}}'


def write_key(origin, reason):
    return f'q4t_http_write_failures_total{{origin="{origin}",reason="{reason}"}}'


def transport_rows(rows):
    values = {key: value for key, value in rows.items() if key.startswith(
        ("q4t_http_receive_failures_total", "q4t_http_write_failures_total"))}
    expected = {receive_key(r) for r in RECEIVE} | {
        write_key(origin, r) for origin in ("handler", "heartbeat") for r in WRITE}
    require(set(values) == expected, "unbounded/missing transport metric labels")
    return values


def raw_request(label, start, headers=b"", payload=b""):
    return (start + b" HTTP/1.1\r\nHost: localhost\r\nX-Request-Id: srv01b." +
            label.encode() + b"\r\n" + headers + b"\r\n" + payload)


def slow_request():
    return make_request([dict(role="user", content=
        "从1开始逐行列出整数，一直列到1000，每行只写一个数字。")], [],
        stream=True, max_tokens=1024, timeout_ms=180000)


def decode_error(raw, status, code=None, param=None):
    head, payload = raw.split(b"\r\n\r\n", 1)
    require(int(head.split()[1]) == status, "incorrect HTTP rejection status")
    headers = dict(line.split(b":", 1) for line in head.split(b"\r\n")[1:])
    require(b"X-Request-Id" in headers, "rejection trace missing")
    value = json.loads(payload)
    require(set(value) == {"error"}, "error/success mixed")
    if code is not None:
        require(value["error"]["code"] == code and value["error"]["param"] == param,
                "error code/param mismatch")
    return value, headers[b"X-Request-Id"].strip().decode()


def check_failed_stream(raw):
    require(raw.startswith(b"HTTP/1.1 200 "), "stream did not start")
    require(b"data: [DONE]" not in raw, "failed writer appended terminal")
    payload = raw.split(b"\r\n\r\n", 1)[1]
    # The final event may be partial. Never promote this prefix to a turn.
    complete = payload.replace(b"\r\n", b"\n").split(b"\n\n")[:-1]
    count = 0
    for event in complete:
        if not event.startswith(b"data: "):
            continue
        value = json.loads(event[6:])
        require("error" not in value and value.get("usage") is None,
                "failed writer appended a terminal/usage envelope")
        for choice in value.get("choices", []):
            require(choice.get("finish_reason") is None, "failed writer reported success")
        count += 1
    require(count > 0, "missing streamed prefix")
    return dict(complete_events=count, complete_assistant_turn=False,
                automatic_retry=False, bytes=len(raw))


class Run(base.Run):
    def __init__(self, plan, output):
        super().__init__(plan, output)
        self.deadline = plan["started"] + stage.LIMITS["seconds"] - 60

    def budget(self, kind=None):
        require(time.time() < self.deadline, "original stage deadline")
        if kind:
            require(self.counts[kind] < stage.LIMITS[kind], "stage budget: " + kind)
            self.counts[kind] += 1
            h.save(self.out / f"attempt-{kind}-{self.counts[kind]:02d}.json",
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def wait(self, label, predicate, *, timeout=10):
        end, samples = time.monotonic()+timeout, []
        try:
            while True:
                self.budget()
                rows = self.rows()
                samples.append(dict(monotonic=time.monotonic(), metrics=rows))
                if predicate(rows):
                    return rows
                require(self.server.poll() is None and time.monotonic() < end,
                        "observation timed out: " + label)
                time.sleep(0.1)
        finally:
            h.save(self.folder / (label+"-samples.json"), samples)

    def connect(self, label, wire, *, kind="negatives", small=False):
        self.budget(kind)
        (self.folder / (label+"-request.bin")).write_bytes(wire)
        sock = socket.socket()
        self.sockets[sock] = label
        if small:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        sock.settimeout(40)
        sock.connect(("127.0.0.1", self.port))
        sock.sendall(wire)
        return sock

    def read(self, sock, label):
        # Always save bytes obtained before timeout/reset. This never replays.
        raw = bytearray()
        try:
            while True:
                part = sock.recv(65536)
                if not part:
                    return bytes(raw)
                raw.extend(part)
                require(len(raw) <= 1024*1024, "unexpected response volume")
        finally:
            (self.folder / (label+"-response.bin")).write_bytes(raw)
            sock.close()
            self.sockets.pop(sock, None)

    def rejected(self, label, wire, status, reason, code=None, param=None,
                 *, eof=False, sock=None):
        if sock is None:
            sock = self.connect(label, wire)
        if eof:
            sock.shutdown(socket.SHUT_WR)  # Half-close preserves error response.
        raw = self.read(sock, label)
        value, trace = decode_error(raw, status, code, param)
        self.transport.validate(value)
        self.expected_receives[trace] = reason
        h.save(self.folder / (label+"-rejection.json"), dict(status=status,
            trace_id=trace, reason=reason, response=value, wire=h.identity(
                self.folder / (label+"-response.bin"))))

    def receives(self):
        before = self.snapshot("receive-before")
        held = []
        for index in range(3):
            label = "body-holder-" + str(index)
            wire = raw_request(label, b"POST /v1/chat/completions",
                b"Content-Type: application/json\r\nContent-Length: 16777216\r\n")
            held.append((label, self.connect(label, wire)))
        self.wait("body-full", lambda m: m["q4t_request_body_bytes"] == 48*1024**2)
        self.health("body-held")
        for endpoint in ("completions", "preflight"):
            label = "body-reject-" + endpoint
            self.rejected(label, raw_request(label, ("POST /v1/chat/"+endpoint).encode(),
                b"Content-Type: application/json\r\nContent-Length: 1\r\n"),
                503, "body_capacity", "body_capacity_exhausted", "body")
        require(self.rows()["q4t_request_body_bytes"] == 48*1024**2,
                "rejection released someone else's body lease")
        for label, sock in held:
            self.rejected(label, None, 400, "body_eof", eof=True, sock=sock)
        self.drain("body-released")
        cases = [
            ("malformed", b"GET / HTTP/9\r\n\r\n", 400, "malformed"),
            ("media", raw_request("media", b"POST /v1/chat/completions",
                b"Content-Type: text/plain\r\nContent-Length: 2\r\n", b"{}"), 415, "unsupported_media"),
            ("route", raw_request("route", b"GET /missing"), 404, "route"),
            ("method", raw_request("method", b"GET /v1/chat/completions"), 405, "method"),
            ("query", raw_request("query", b"GET /healthz?x"), 400, "query"),
            ("get-body", raw_request("get-body", b"GET /healthz", b"Content-Length: 1\r\n", b"x"),
                400, "unexpected_body"),
            ("body-large", raw_request("body-large", b"POST /v1/chat/completions",
                b"Content-Type: application/json\r\nContent-Length: 16777217\r\n"), 400, "body_too_large")]
        for label, wire, status, reason in cases:
            self.rejected(label, wire, status, reason)
        self.rejected("header-eof", b"GET /", 400, "header_eof", eof=True)
        # Original 30-second reader deadline, simultaneous independent phases.
        head = self.connect("header-deadline", b"GET /")
        body = self.connect("body-deadline", raw_request("body-deadline",
            b"POST /v1/chat/completions", b"Content-Type: application/json\r\nContent-Length: 2\r\n", b"x"))
        self.health("read-deadline-pending")
        self.rejected("header-deadline", None, 408, "header_deadline", sock=head)
        self.rejected("body-deadline", None, 408, "body_deadline", sock=body)
        # Business contract errors must not inflate transport receive counters.
        payload = b'{"messages":[]}'
        self.rejected("business-validation", raw_request("business-validation",
            b"POST /v1/chat/completions", b"Content-Type: application/json\r\nContent-Length: " +
            str(len(payload)).encode()+b"\r\n", payload), 400, None)
        after = self.drain("receive-after")
        expected = {reason: list(self.expected_receives.values()).count(reason) for reason in RECEIVE}
        for reason in RECEIVE:
            require(after[receive_key(reason)]-before[receive_key(reason)] == expected[reason],
                    "receive accounting mismatch: " + reason)
        for key in ("q4t_prompt_tokens_total", "q4t_generation_tokens_total"):
            require(after[key] == before[key], "receive rejection executed model")
        require(sum(after[write_key(o,r)] for o in ("handler", "heartbeat") for r in WRITE) == 0,
                "unexpected rejection write failure")

    def disconnected(self):
        body = base.constrained(hold=True, stream=True)
        h.save(self.folder / "reset-request.json", body)
        payload = json.dumps(body).encode()
        wire = raw_request("reset", b"POST /v1/chat/completions",
            b"Content-Type: application/json\r\nContent-Length: " + str(len(payload)).encode()+b"\r\n", payload)
        sock = self.connect("reset", wire, kind="generations")
        prefix = bytearray()
        try:
            while b"\r\n\r\n" not in prefix:
                part = sock.recv(4096)
                require(part, "reset fixture missing HTTP header")
                prefix.extend(part)
            self.wait("reset-active", lambda m: m["q4t_active_chats"] == 1 and
                      m["q4t_seq_slots_free"] == 0)
        finally:
            (self.folder / "reset-prefix.bin").write_bytes(prefix)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()
            self.sockets.pop(sock, None)
        after = self.drain("reset-drained")
        self.wait("reset-write-published", lambda m: sum(m[write_key(o,"peer_closed")]
                  for o in ("handler", "heartbeat")) == 1)
        require(after['q4t_requests_aborted_by_reason_total{reason="client_disconnected"}'] == 1,
                "reset task outcome was not cancelled")
        self.expected_writes["srv01b.reset"] = "peer_closed"
        h.save(self.folder / "reset-unknown-result.json", dict(complete_assistant_turn=False,
            automatic_retry=False, prefix=h.identity(self.folder / "reset-prefix.bin")))

    def slow(self, *, cancel):
        label = "slow-cancel" if cancel else "slow-deadline"
        before = self.snapshot(label+"-before")
        body = slow_request()
        h.save(self.folder / (label+"-request.json"), body)
        payload = json.dumps(body).encode()
        sock = self.connect(label, raw_request(label, b"POST /v1/chat/completions",
            b"Content-Type: application/json\r\nContent-Length: " + str(len(payload)).encode()+b"\r\n", payload),
            kind="generations", small=True)
        start = time.monotonic()
        gate = self.folder / "backpressure"
        if cancel:
            end = start + 60
            while not gate.with_suffix(".entered").exists():
                self.budget()
                require(self.server.poll() is None and time.monotonic() < end, "TCP EAGAIN not reached")
                time.sleep(0.02)
            try:
                self.health("slow-held")
                value = self.http("POST", "/v1/requests/cancel", {
                    key: body[key] for key in ("request_id", "cancel_token")})
                h.save(self.folder / "slow-cancel-http.json", value)
                require(value["status"] == 202, "slow writer blocks cancellation")
            finally:
                gate.with_suffix(".release").touch(exist_ok=False)
        reason = "explicit_cancel" if cancel else "write_deadline"
        # Do not read: that would relieve the pressure being verified.
        self.wait(label+"-write-published", lambda m:
                  m[write_key("handler", reason)]-before[write_key("handler", reason)] == 1,
                  timeout=15 if cancel else 75)
        raw = self.read(sock, label)
        report = check_failed_stream(raw)
        report.update(seconds=time.monotonic()-start, expected_reason=reason,
                      socket_window_small=True, strict_output=False)
        h.save(self.folder / (label+"-incomplete.json"), report)
        self.expected_writes["srv01b."+label] = reason
        self.drain(label+"-drained")
        self.health(label+"-recovered-control")

    def reconcile(self):
        rows = transport_rows(self.snapshot("final-observation"))
        log = (self.folder / "server.log").read_text()
        receives = re.findall(r"\[q4t\]\[http-receive\] trace_id=(\S+) reason=(\S+) status=(\d+) system_error=(\d+)", log)
        writes = re.findall(r"\[q4t\]\[http-write\] trace_id=(\S+) reason=(\S+) origin=(\S+) transferred=(\d+) system_error=(\d+) poll_events=(\d+)", log)
        wanted = {key:value for key,value in self.expected_receives.items() if value is not None}
        require(len(receives) == len(wanted) and {r[0]:r[1] for r in receives} == wanted,
                "receive trace/log duplication or mismatch")
        require(len(writes) == len(self.expected_writes) and {r[0]:r[1] for r in writes} == self.expected_writes,
                "first-write trace/log duplication or mismatch")
        for reason in RECEIVE:
            require(rows[receive_key(reason)] == sum(r[1] == reason for r in receives), "receive log/metric mismatch")
        for origin in ("handler", "heartbeat"):
            for reason in WRITE:
                require(rows[write_key(origin,reason)] == sum(r[1] == reason and r[2] == origin for r in writes),
                        "write log/metric mismatch")
        h.save(self.folder / "observation-reconciled.json", dict(passed=True,
            metrics=rows, receive_logs=receives, write_logs=writes,
            local_bytes_not_peer_receipt=True))

    def model(self, fault):
        self.budget("starts")
        self.folder = self.out / ("ordinary-writes-fault" if fault else "production-mtp")
        self.folder.mkdir()
        self.expected_receives, self.expected_writes, self.sockets = {}, {}, {}
        binary = self.plan["fault_binary"] if fault else self.plan["binary"]
        argv = [binary["path"], "serve", "--model-dir", self.plan["model_dir"], "--host",
            "127.0.0.1", "--port", str(self.port), "--max-prefill", "2048", "--max-len", "8192",
            "--max-seq", "1", "--max-inflight-requests", "8", "--max-queued-requests", "2"]
        if fault:
            argv.append("--no-mtp")
        env = {k:v for k,v in os.environ.items() if not k.startswith("Q4T_") and k != "LD_PRELOAD"}
        extra = dict(Q4T_SERVE_TEST_SNDBUF="1", Q4T_SERVE_PAUSE="write.backpressure",
                     Q4T_SERVE_GATE=str(self.folder / "backpressure")) if fault else {}
        env.update(extra)
        h.save(self.folder / "command.json", dict(argv=argv, binary=binary, validation_env=extra))
        self.calls, self.server, self.server_fd = [], None, None
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
            value = self.http("GET", "/v1/capabilities")
            h.save(self.folder / "capabilities.json", value)
            caps = json.loads(value["raw"])
            require(caps["protocol_source_sha256"] == self.plan["protocol_sha256"] and
                    caps["observability"]["version"] == 3, "capability identity drift")
            value = self.http("GET", "/v1/config")
            h.save(self.folder / "configuration.json", value)
            self.config_schema.validate(json.loads(value["raw"]))
            require(json.loads(value["raw"])["runtime"]["mtp_enabled"] is (not fault), "decode mode drift")
            self.drain("startup-empty")
            if fault:
                self.slow(cancel=True)
                self.slow(cancel=False)
            else:
                self.receives()
                self.disconnected()
            self.normal(self.submit("ordinary-recovery", base.ordinary()))
            if fault:
                call = self.submit("strict-recovery", base.constrained())
                value = call.finish()
                self.check_wire(call)
                require(value.finish_reason == "stop" and json.loads(value.message["content"]) == {"ok":True},
                        "strict recovery quality")
            else:
                self.project_tool()
            self.drain("final-empty")
            self.health("final")
            self.reconcile()
        finally:
            # Failed fixtures retain any response prefix too. Release only our
            # validation gate; consuming bytes here cannot retroactively pass
            # backpressure. At most one second per socket, within stage budget.
            gate = self.folder / "backpressure"
            if gate.with_suffix(".entered").exists() and not gate.with_suffix(".release").exists():
                gate.with_suffix(".release").touch(exist_ok=False)
            for sock, label in list(self.sockets.items()):
                end, raw, error = time.monotonic()+1, bytearray(), None
                try:
                    while time.monotonic() < end:
                        sock.settimeout(max(0.001, end-time.monotonic()))
                        part = sock.recv(min(65536, 1024*1024-len(raw)+1))
                        if not part:
                            break
                        raw.extend(part)
                        require(len(raw) <= 1024*1024, "failure evidence volume exceeded")
                except Exception as exc:
                    error = repr(exc)
                finally:
                    (self.folder / (label+"-failure-prefix.bin")).write_bytes(raw)
                    h.save(self.folder / (label+"-failure-capture.json"), dict(
                        bytes=len(raw), error=error, acceptance=False, automatic_retry=False))
                    sock.close()
                    self.sockets.pop(sock, None)
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
            require(self.counts == stage.PLANNED, "frozen case missing")
            h.save(self.out / "wire-schema-result.json", dict(passed=True, records=self.wires))
            state["status"] = "PASS_BOUNDED_OBSERVABILITY"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(counts=self.counts, seconds=time.monotonic()-self.started,
                         performance_acceptance=False, automatic_retry=False)
            h.save(self.out / "result.json", state)
