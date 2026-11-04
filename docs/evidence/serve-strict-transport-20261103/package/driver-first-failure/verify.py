"""Actual strict long-output transport with explicit copy conditioning.

Reuses bounded connection/ownership helpers, not the old experiment's budget.
The server never injects/replaces model tokens. Captured IDs identify the real
path; an absent/truncated/incomplete capture cannot prove generation success.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

from tokenizers import Tokenizer
from tools.serve_backpressure import verify as base
from tools.serve_observability.verify import transport_rows, write_key
from tools.serve_strict_transport import stage
from chat_client import ProtocolError, require

ROOT, h = stage.ROOT, stage.h
short_request = base.short_request
slow_socket = base.slow_socket
metrics = base.metrics
partial_contract = base.partial_contract
ended_before_eagain = base.ended_before_eagain
preserve_ended_wire = base.preserve_ended_wire


def token_record(log, request_id):
    pattern = (r"^\[serve-test-generated\] request=" + re.escape(request_id) +
               r" total=(\d+) captured=(\d+) truncated=([01]) tokens=(\[.*\])$")
    rows = re.findall(pattern, log, re.M)
    require(len(rows) <= 1, "duplicate actual token evidence")
    if not rows:
        return None
    total, captured, truncated, raw = rows[0]
    ids = json.loads(raw)
    require(type(ids) is list and len(ids) == int(captured) <= 4096 and
            all(type(token) is int and 0 <= token < 248320 for token in ids) and
            int(captured) == min(int(total), 4096) and
            bool(int(truncated)) == (int(total) > 4096), "invalid token evidence")
    return dict(total=int(total), captured=int(captured), truncated=bool(int(truncated)), ids=ids)


class Run(base.Run):
    def __init__(self, plan, output):
        super().__init__(plan, output)
        self.deadline = plan["started"] + stage.LIMITS["seconds"] - 30
        self.tokenizer = Tokenizer.from_file(str(Path(plan["model_dir"]) / "tokenizer.json"))
        self.expected = json.loads((stage.PACKAGE / "payload/expected-content.json").read_text())

    def budget(self, kind=None):
        require(time.time() < self.deadline, "original independent stage deadline")
        if kind:
            require(self.counts[kind] < stage.LIMITS[kind], "stage budget: " + kind)
            self.counts[kind] += 1
            h.save(self.out / f"attempt-{kind}-{self.counts[kind]:02d}.json",
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def generated(self, folder, body, *, complete):
        record = token_record((folder / "server.log").read_text(), body["request_id"])
        value = dict(present=record is not None, complete=False, actual_model_tokens=True)
        if record is not None:
            value.update(record)
            text = self.tokenizer.decode(record["ids"], skip_special_tokens=True)
            (folder / "generated-content.txt").write_text(text)
            value.update(content_bytes=len(text.encode()),
                         content_sha256=hashlib.sha256(text.encode()).hexdigest(),
                         last_token=record["ids"][-1] if record["ids"] else None)
            try:
                value["schema_value_matches"] = json.loads(text) == self.expected
            except ValueError:
                value["schema_value_matches"] = False
            eos = self.plan["stop_ids"]
            value["complete"] = (not record["truncated"] and 0 < record["total"] <=
                body["max_tokens"] and value["last_token"] in eos and
                not any(token in eos for token in record["ids"][:-1]) and
                value["schema_value_matches"] and value["content_bytes"] >= 12443)
        h.save(folder / "generated.json", value)
        if complete:
            require(value["complete"], "actual long generation did not close/match schema")
        return value

    def drain(self, folder, label, timeout=45):
        rows = super().drain(folder, label, timeout)
        require(rows["q4t_queued_requests"] == 0, "capacity queue owner leaked")
        return rows

    def case(self, folder, stream):
        body = h.read(stage.PACKAGE / "payload" /
                      ("sse-request.json" if stream else "json-request.json"))
        self.request(folder, "long", body)
        submitted = time.monotonic()
        sock = slow_socket(self.port, body)
        self.pending_socket = sock
        gate = folder / "backpressure"
        queued = {}
        samples = []
        try:
            end = submitted + 170
            next_sample = 0
            while not gate.with_suffix(".entered").exists():
                self.budget()
                diagnostic = ended_before_eagain((folder / "server.log").read_text(),
                                                 body["request_id"])
                if diagnostic:
                    preserve_ended_wire(sock, folder, diagnostic)
                    raise ProtocolError("request ended before required EAGAIN; raw wire retained")
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
            self.generated(folder, body, complete=True)
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
            observed = transport_rows(final)
            reason = "explicit_cancel" if stream else "write_deadline"
            require(observed[write_key("handler", reason)] == 1 and
                    sum(observed.values()) == 1, "first-write metric accounting")
            log = (folder / "server.log").read_text()
            failures = re.findall(r"^\[q4t\]\[http-write\] .+$", log, re.M)
            require(len(failures) == 1 and "reason="+reason+" origin=handler " in failures[0],
                    "first failure not the expected handler write")
            trace = re.search(rb"(?im)^X-Request-Id: *([^\r\n]+)", bytes(raw))
            require(trace and "trace_id="+trace[1].decode()+" " in failures[0],
                    "write failure did not match actual response trace")
            h.save(folder / "first-write.json", dict(log=failures[0], metrics=observed))
            self.health(folder, "final")
            value = dict(stream=stream, submitted=submitted, eagain=entered,
                released=released, cleaned=cleaned, first_read=received,
                blocked_seconds=cleaned-entered, generation_to_eagain_seconds=entered-submitted,
                partial=proof, final_metrics=final, passed=True)
            h.save(folder / "case-result.json", value)
            self.cases.append(value)
        finally:
            h.save(folder / "generation-memory-samples.json", samples)
            if gate.with_suffix(".entered").exists() and not gate.with_suffix(".release").exists():
                gate.with_suffix(".release").touch(exist_ok=False)
            if (folder / "long-wire.bin").exists() or (folder / "long-ended-wire.bin").exists():
                sock.close()
                self.pending_socket = None

    def model(self, stream):
        self.budget("starts")
        folder = self.out / ("sse-cancel" if stream else "json-write-timeout")
        folder.mkdir()
        argv = [self.plan["binary"]["path"], "serve", "--model-dir", self.plan["model_dir"],
                "--host", "127.0.0.1", "--port", str(self.port), "--max-seq", "1",
                "--max-prefill", "2048", "--max-len", "8192", "--no-mtp"]
        env = {k:v for k,v in os.environ.items() if not k.startswith("Q4T_") and k != "LD_PRELOAD"}
        extra = dict(Q4T_SERVE_TEST_SNDBUF="1", Q4T_SERVE_PAUSE="write.backpressure",
                     Q4T_SERVE_GATE=str(folder / "backpressure"),
                     Q4T_SERVE_GENERATED_IDS="strict-copy-sse" if stream else "strict-copy-json")
        env.update(extra)
        h.save(folder / "command.json", dict(argv=argv, binary=self.plan["binary"], validation_env=extra))
        self.server = self.server_fd = self.thread = self.pending_socket = None
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
                if self.pending_socket is not None:
                    # Only after the owned server exits: no early reading can
                    # relieve a write under test. Retain bytes even on recv error.
                    failure = None
                    total = 0
                    try:
                        self.pending_socket.settimeout(1)
                        with (folder / "failed-wire.bin").open("xb", buffering=0) as wire:
                            while total < 1024*1024:
                                part = self.pending_socket.recv(min(65536, 1024*1024-total))
                                if not part:
                                    break
                                wire.write(part)
                                os.fsync(wire.fileno())
                                total += len(part)
                    except OSError as error:
                        failure = repr(error)
                    finally:
                        self.pending_socket.close()
                        self.pending_socket = None
                        h.save(folder / "failed-wire-capture.json", dict(bytes=total,
                            read_error=failure, after_owned_server_exit=True))
                clean = self.server.returncode == 0 and "shutdown complete (0 in-flight remaining)" in (folder / "server.log").read_text()
                h.save(folder / "shutdown.json", dict(clean=clean, returncode=self.server.returncode))
                require(clean, "unclean owned shutdown")
                require(not self.thread or not self.thread.is_alive(), "owned client did not stop")
            body = h.read(stage.PACKAGE / "payload" /
                          ("sse-request.json" if stream else "json-request.json"))
            if not (folder / "generated.json").exists():
                # Preserve the actual path even if generation ended before EAGAIN.
                self.generated(folder, body, complete=False)
            stage.a.require_idle_gpu(folder / "gpu-after.json")

    def run(self):
        state = dict(status="FAILED")
        try:
            self.model(True)
            self.model(False)
            require(self.counts == stage.PLANNED, "missing frozen case")
            state.update(status="PASS_BOUNDED_STRICT_COPY_TRANSPORT", cases=self.cases)
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(counts=self.counts, seconds=time.monotonic()-self.started,
                         default_replaced=False, performance_acceptance=False)
            h.save(self.out / "result.json", state)
