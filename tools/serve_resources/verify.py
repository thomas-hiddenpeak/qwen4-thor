"""Bounded mixed serving, cooperative cancellation and resource observations."""
import concurrent.futures
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "resource_input_wire", ROOT / "tools/input_contract/verify.py")
i = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i)
s, wire = i.s, i.wire
REASONS = {"client_disconnected", "explicit_cancel", "deadline_exceeded",
           "server_shutdown", "request_failed", "other"}


def metrics(text):
    rows = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.rsplit(" ", 1)
        wire.require(key not in rows and math.isfinite(float(value)), "invalid metric")
        rows[key] = float(value)
    return rows


def metric_contract(text):
    rows = metrics(text)
    for legacy, current in [("q4t_queue_seconds", "q4t_admission_seconds"),
                            ("q4t_e2e_seconds", "q4t_generation_complete_seconds")]:
        keys = [key for key in rows if key.startswith(current + "_")]
        wire.require(keys and all(rows.get(legacy + key[len(current):]) == rows[key]
                                 for key in keys), "legacy metric alias changed")
        wire.require("# HELP " + legacy + " Deprecated alias" in text,
                     "legacy metric lacks migration notice")
    prefix = 'q4t_requests_aborted_by_reason_total{reason="'
    reasons = {key[len(prefix):-2] for key in rows if key.startswith(prefix)}
    wire.require(reasons == REASONS, "unbounded or incomplete cancellation labels")
    wire.require(sum(rows[prefix + reason + '"}'] for reason in REASONS) ==
                 rows["q4t_requests_aborted_total"], "cancellation accounting differs")
    return rows


def cancellation_wire(raw, reason):
    head, separator, body = raw.partition(b"\r\n\r\n")
    wire.require(separator, "missing HTTP response")
    if head.startswith(b"HTTP/1.1 409 "):
        wire.require(json.loads(body)["error"]["code"] == reason, "wrong cancel JSON")
        return
    wire.require(head.startswith(b"HTTP/1.1 200 "), "wrong cancellation status")
    events = [line[6:] for line in body.decode().splitlines() if line.startswith("data: ")]
    wire.require(events and events[-1] == "[DONE]", "missing cancellation SSE terminal")
    parsed = [json.loads(value) for value in events[:-1]]
    wire.require(sum("error" in event for event in parsed) == 1 and
                 parsed[-1].get("error", {}).get("code") == reason,
                 "wrong cancel SSE error")
    wire.require(all(not choice.get("finish_reason") for event in parsed
                     for choice in event.get("choices", [])), "success after cancellation")


def long_request(request_id):
    shape = s.schema(dict(xs=dict(type="array", minItems=64, maxItems=64,
        items=dict(type="string", enum=["x" * 256]))))
    body = s.request("只输出符合schema的JSON对象，将xs填满。", stream=True,
        max_completion_tokens=4096, response_format=dict(type="json_schema",
            json_schema=dict(name="long", strict=True, schema=shape)))
    body.update(body.pop("extra_body"))
    body.update(request_id=request_id, cancel_token="a" * 64)
    return body


class Run(i.Run):
    def budget(self):
        wire.require(time.time() < self.deadline, "resource stage deadline")
        wire.require(self.requests <= (2 if self.installed else 6) and
                     self.negative <= (0 if self.installed else 1) and
                     self.starts <= 1 and self.capability_gets <= 1,
                     "resource stage request/start budget")

    def snapshot(self, folder, label):
        status, raw = self.request("GET", "/metrics")
        wire.require(status == 200, "metrics unavailable")
        wire.save(folder / (label + "-metrics.json"), dict(status=status, raw=raw))
        proc = Path(f"/proc/{self.server.pid}/status").read_text()
        mem = Path("/proc/meminfo").read_text()
        wire.save(folder / (label + "-memory.json"), dict(pid=self.server.pid,
            time=time.time(), process_status=proc, meminfo=mem,
            scope="raw RSS/HWM and system RAM; overlapping counters are not added"))
        return metrics(raw), raw

    def observe(self, predicate, *, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.budget()
            status, raw = self.request("GET", "/metrics")
            wire.require(status == 200, "observation HTTP failed")
            rows = metrics(raw)
            if predicate(rows):
                return rows
            time.sleep(0.01)
        raise AssertionError("bounded state observation timed out")

    def open_unread(self, folder, label, body):
        self.counted()
        sock = socket.create_connection(("127.0.0.1", self.args.port), timeout=15)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        payload = json.dumps(body, ensure_ascii=False).encode()
        wire.save(folder / (label + "-request.json"), body)
        sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                     b"Content-Type: application/json\r\nContent-Length: " +
                     str(len(payload)).encode() + b"\r\n\r\n" + payload)
        return sock

    @staticmethod
    def receive(sock):
        raw = bytearray()
        while True:
            part = sock.recv(65536)
            if not part:
                return bytes(raw)
            raw.extend(part)
            wire.require(len(raw) <= 2 * 1024 * 1024, "wire capture budget")

    def stop(self):
        if self.server.poll() is None:
            # Descriptor opened at creation, never discover/kill another service.
            signal.pidfd_send_signal(self.server_fd, signal.SIGTERM)

    def mixed_cancel(self, folder):
        before, _ = self.snapshot(folder, "before-mixed")
        sock = self.open_unread(folder, "mixed-long", long_request("resource-long"))
        try:
            self.observe(lambda m: m["q4t_structured_active"] == 1 and
                m["q4t_target_ready_seconds_count"] > before["q4t_target_ready_seconds_count"])
            self.snapshot(folder, "structured-running-unread")
            self.counted(negative=True)
            busy = s.request("只输出JSON对象", response_format=dict(type="json_object"))
            busy.update(busy.pop("extra_body"))
            code, raw = self.request("POST", "/v1/chat/completions", busy)
            wire.save(folder / "busy-response.json", dict(status=code, raw=raw, request=busy))
            wire.require(code == 503 and json.loads(raw)["error"]["code"] ==
                         "structured_capacity_exhausted", "structured admission failed")
            ordinary = s.request("只回复数字710003。")
            ordinary["extra_body"].update(request_id="resource-ordinary", cancel_token="b" * 64)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.success, folder, "queued-ordinary", ordinary)
                try:
                    self.observe(lambda m: m["q4t_active_chats"] >= 2)
                    self.snapshot(folder, "ordinary-and-structured")
                    code, raw = self.request("POST", "/v1/requests/cancel",
                        dict(request_id="resource-long", cancel_token="a" * 64))
                    wire.save(folder / "explicit-cancel.json", dict(status=code, raw=raw))
                    wire.require(code == 202, "cancel not accepted")
                    response = self.receive(sock)
                    (folder / "cancelled-wire.bin").write_bytes(response)
                    cancellation_wire(response, "explicit_cancel")
                    answer = future.result(timeout=120)
                    wire.require(answer.finish_reason == "stop" and
                        answer.message["content"].strip() == "710003", "queued ordinary quality")
                except BaseException:
                    self.stop()
                    raise
        finally:
            sock.close()
        self.wait_slot("mixed drained")
        self.observe(lambda m: m["q4t_structured_active"] == 0 and
                     m["q4t_structured_preparing"] == 0)
        self.snapshot(folder, "after-mixed")

    def deadline_case(self, folder):
        self.counted()
        body = long_request("resource-deadline")
        body.update(stream=False, request_timeout_ms=1)
        code, raw = self.request("POST", "/v1/chat/completions", body)
        wire.save(folder / "deadline-response.json", dict(status=code, raw=raw, request=body))
        wire.require(code == 409 and json.loads(raw)["error"]["code"] ==
                     "deadline_exceeded", "short deadline contract")
        self.wait_slot("deadline drained")

    def shutdown_case(self, folder):
        before, _ = self.snapshot(folder, "before-shutdown")
        sock = self.open_unread(folder, "shutdown-long", long_request("resource-shutdown"))
        try:
            self.observe(lambda m: m["q4t_structured_active"] == 1 and
                m["q4t_target_ready_seconds_count"] > before["q4t_target_ready_seconds_count"])
            self.snapshot(folder, "shutdown-active")
            self.stop()
            response = self.receive(sock)
            (folder / "shutdown-wire.bin").write_bytes(response)
            # Shutdown closes peer sockets; a complete protocol tail is not promised.
            wire.require(b'"finish_reason":"stop"' not in response and
                         b'"finish_reason":"tool_calls"' not in response,
                         "unexpected success during shutdown")
            self.server.wait(timeout=45)
            log = (folder / "server.log").read_text()
            wire.require("id=resource-shutdown" in log and
                         "reason=server_shutdown" in log, "shutdown reason missing")
        finally:
            sock.close()

    def business(self, folder):
        self.mixed_cancel(folder)
        self.deadline_case(folder)
        self.project_loop(folder, strict=True, stream=True)
        rows, raw = self.snapshot(folder, "before-final-shutdown")
        metric_contract(raw)
        wire.require(rows["q4t_structured_active"] == rows["q4t_structured_preparing"] ==
                     rows["q4t_structured_vocab_bytes"] == 0, "structured resources retained")
        wire.require(rows["q4t_structured_rejected_total"] == 1 and
                     rows["q4t_structured_prepare_failed_total"] == 0, "unexpected prepare failure")
        for reason in ("explicit_cancel", "deadline_exceeded"):
            wire.require(rows['q4t_requests_aborted_by_reason_total{reason="' +
                              reason + '"}'] == 1, "wrong abort accounting")
        log = (folder / "server.log").read_text()
        ends = [line for line in log.splitlines() if "event=end" in line]
        waits = [float(re.search(r"slot_wait_ms=([0-9.]+)", line)[1]) for line in ends]
        ordinary = next(line for line in ends if "id=resource-ordinary " in line)
        wire.require(float(re.search(r"slot_wait_ms=([0-9.]+)", ordinary)[1]) > 0,
                     "ordinary request did not wait for slot")
        wire.require(abs(sum(waits) / 1000 - rows["q4t_slot_wait_seconds_sum"]) < 0.0001,
                     "slot metric includes time outside recorded wait")
        wire.require(rows["q4t_admission_seconds_sum"] > rows["q4t_slot_wait_seconds_sum"],
                     "preprocessing and slot timing not separated")
        self.shutdown_case(folder)

    def model(self, name, binary, mtp, action):
        self.starts += 1
        self.budget()
        folder = self.out / name
        folder.mkdir(mode=0o700)
        command = [str(binary), "serve", "--model-dir", str(self.args.model_dir),
            "--host", "127.0.0.1", "--port", str(self.args.port), "--max-seq", "1",
            "--max-prefill", "2048", "--max-len", "8192"]
        wire.require(mtp, "frozen resource stage uses default MTP service")
        wire.save(folder / "command.json", dict(argv=command, sha256=wire.digest(binary)))
        env = {k: v for k, v in os.environ.items() if not k.startswith("Q4T_") and k != "LD_PRELOAD"}
        self.server = None
        self.server_fd = None
        try:
            with (folder / "server.log").open("x") as log:
                self.server = subprocess.Popen(command, cwd=ROOT, env=env,
                    stdout=log, stderr=subprocess.STDOUT)
                self.server_fd = os.pidfd_open(self.server.pid)
                start = time.monotonic()
                while "serving on port" not in (folder / "server.log").read_text():
                    self.budget()
                    wire.require(self.server.poll() is None and time.monotonic() - start < 240,
                                 "server startup failed/timed out")
                    time.sleep(1)
                code, raw = self.request("GET", "/v1/capabilities")
                wire.save(folder / "capabilities.json", dict(status=code, raw=raw))
                caps = wire.validate_capabilities(json.loads(raw), "qwen3.8-flash-next",
                    protocol_sha256=self.args.protocol_sha256)
                wire.require(code == 200 and caps["structured_output"]["resources"]["active_limit"] == 1
                             and caps["observability"]["version"] == 2, "capability mismatch")
                action(folder)
        finally:
            if self.server is not None and self.server.poll() is None:
                if self.server_fd is not None:
                    self.stop()
                else:
                    self.server.terminate()
                try:
                    self.server.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    if self.server_fd is not None:
                        signal.pidfd_send_signal(self.server_fd, signal.SIGKILL)
                    self.server.wait(timeout=10)
            if self.server_fd is not None:
                os.close(self.server_fd)
            log = (folder / "server.log").read_text()
            clean = self.server is not None and self.server.returncode == 0 and (
                "shutdown complete (0 in-flight remaining)" in log)
            wire.save(folder / "shutdown.json", dict(clean=clean,
                returncode=self.server.returncode if self.server else None))
            wire.require(clean, "unclean shutdown")

    def run(self):
        state = dict(status="FAILED")
        try:
            binary = ROOT / "build/q4t" if self.installed else Path(self.plan["candidate"])
            self.model("candidate-installed" if self.installed else "candidate-mtp",
                       binary, True, self.health if self.installed else self.business)
            wire.require(self.requests == (2 if self.installed else 6) and
                         self.negative == (0 if self.installed else 1), "missing cases")
            state["status"] = "PASS_BOUNDED_SERVE_RESOURCES"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(request_attempts=self.requests, overload_rejections=self.negative,
                starts=self.starts, seconds=time.monotonic() - self.started,
                performance_acceptance=False, hard_compile_memory_or_time_bound=False)
            wire.save(self.out / "result.json", state)
