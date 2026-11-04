"""Real high-floor refusal and normal tool business; never exhaust host RAM."""
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time

from tools.serve_admission import verify as base
from tools.serve_memory import stage
from jsonschema import Draft202012Validator

h, ROOT = stage.h, stage.ROOT
require = h.require
STATES = ("disabled", "open", "memory_pressure", "memory_observation_unavailable")
POINTS = ("headers", "prepare", "execute")
REASONS = ("memory_pressure", "memory_observation_unavailable")


def rejection_key(point, reason):
    return f'q4t_memory_rejected_total{{checkpoint="{point}",reason="{reason}"}}'


def memory_contract(rows, *, opened, floor):
    expected = {rejection_key(p, r) for p in POINTS for r in REASONS}
    require({k for k in rows if k.startswith("q4t_memory_rejected_total")} == expected,
            "unbounded or missing rejection labels")
    state_keys = {f'q4t_memory_admission_state{{state="{s}"}}' for s in STATES}
    require({k for k in rows if k.startswith("q4t_memory_admission_state")} == state_keys,
            "unbounded or missing state labels")
    state = "open" if opened else "memory_pressure"
    require(all(rows[k] == (1 if k == f'q4t_memory_admission_state{{state="{state}"}}' else 0)
                for k in state_keys), "memory state inconsistent")
    require(rows["q4t_memory_observation_valid"] == 1 and
            rows["q4t_memory_admission_enabled"] == 1 and
            rows["q4t_memory_observations_total"] >= 1 and
            rows["q4t_memory_observation_failures_total"] == 0 and
            rows["q4t_memory_admission_floor_bytes"] == floor * 1024**2 and
            rows["q4t_memory_admission_recovery_bytes"] == (floor+512)*1024**2 and
            rows["q4t_memory_admission_open"] == int(opened) and
            rows["q4t_ready"] == int(opened), "memory/ready/threshold mismatch")
    available = rows["q4t_memory_available_bytes"]
    require(available >= floor * 1024**2 if opened else available < floor * 1024**2,
            "real observation does not support expected gate")


class Run(base.Run):
    def __init__(self, plan, output):
        super().__init__(plan, output)
        self.deadline = plan["started"] + stage.LIMITS["seconds"] - 60

    def budget(self, kind=None):
        require(time.time() < self.deadline, "original memory-stage deadline")
        if kind:
            require(self.counts[kind] < stage.LIMITS[kind], "stage budget: " + kind)
            self.counts[kind] += 1
            h.save(self.out / f"attempt-{kind}-{self.counts[kind]:02d}.json",
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def refused(self):
        before = self.drain("before-refusals")
        memory_contract(before, opened=False, floor=1048576)
        for label, path in (("chat-json", "/v1/chat/completions"),
                            ("chat-stream-intent", "/v1/chat/completions"),
                            ("preflight", "/v1/chat/preflight")):
            self.budget("negatives")
            # Header admission intentionally cannot know the body's stream flag.
            # No body is sent; a full body reservation/read would time out.
            request = (f"POST {path} HTTP/1.1\r\nHost: localhost\r\n"
                f"X-Request-Id: memory.{label}\r\nContent-Type: application/json\r\n"
                "Content-Length: 16777216\r\n\r\n").encode()
            (self.folder / (label+"-request.bin")).write_bytes(request)
            raw = bytearray()
            started = time.monotonic()
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
                    sock.sendall(request)
                    while True:
                        part = sock.recv(65536)
                        if not part:
                            break
                        raw.extend(part)
                        require(len(raw) < 65536, "rejection too large")
            finally:
                (self.folder / (label+"-wire.bin")).write_bytes(raw)
                h.save(self.folder / (label+"-elapsed.json"),
                       dict(seconds=time.monotonic()-started))
            header, body = bytes(raw).split(b"\r\n\r\n", 1)
            value = json.loads(body)
            self.transport.validate(value)
            require(header.startswith(b"HTTP/1.1 503 ") and
                    value["error"]["code"] == "memory_pressure" and
                    b"X-Request-Id: memory."+label.encode() in header,
                    "early memory rejection/trace")
            self.wires.append(dict(file=h.identity(self.folder / (label+"-wire.bin")),
                                   envelopes=1, success=False))
        for path, expected in (("/readyz", 503), ("/livez", 200), ("/healthz", 200)):
            value = self.http("GET", path)
            h.save(self.folder / ("pressure"+path.replace("/", "-")+".json"), value)
            require(value["status"] == expected, "pressure control availability")
            if path != "/healthz":
                state = json.loads(value["raw"])
                self.transport.validate(state)
                require(state == dict(status="memory_pressure", live=True, ready=False),
                        "pressure readiness body")
        self.budget("negatives")
        cancel = self.http("POST", "/v1/requests/cancel",
                           dict(request_id="memory-unknown", cancel_token="a"*64))
        h.save(self.folder / "unknown-cancel.json", cancel)
        require(cancel["status"] == 404 and json.loads(cancel["raw"])["error"]["code"] ==
                "request_not_found", "cancel control incorrectly blocked")
        self.transport.validate(json.loads(cancel["raw"]))
        after = self.drain("after-refusals")
        memory_contract(after, opened=False, floor=1048576)
        require(after[rejection_key("headers", "memory_pressure")] -
                before[rejection_key("headers", "memory_pressure")] == 3 and
                after["q4t_requests_total"] == 0 and
                after["q4t_structured_workers_started_total"] == 0,
                "memory refusal allocated generation/worker or lost counts")
        for point in POINTS:
            for reason in REASONS:
                require(after[rejection_key(point, reason)] ==
                        (3 if (point, reason) == ("headers", "memory_pressure") else 0),
                        "duplicate/wrong rejection checkpoint")

    def business(self):
        before = self.drain("before-business")
        memory_contract(before, opened=True, floor=2048)
        preflight_request = dict(request=base.ordinary(), include_token_ids=True)
        self.schemas["preflight"].validate(preflight_request)
        h.save(self.folder / "preflight-request.json", preflight_request)
        preflight = self.http("POST", "/v1/chat/preflight", preflight_request)
        h.save(self.folder / "preflight-response.json", preflight)
        require(preflight["status"] == 200, "normal preflight")
        preflight_schema = Draft202012Validator(
            h.read(ROOT / "docs/serve-preflight-v1.schema.json"))
        preflight_schema.validate(json.loads(preflight["raw"]))
        self.normal(self.submit("ordinary-recovery", base.ordinary()))
        for stream in (False, True):
            call = self.submit("strict-"+("sse" if stream else "json"),
                               base.constrained(stream=stream))
            value = call.finish()
            self.check_wire(call)
            require(value.finish_reason == "stop" and
                    json.loads(value.message["content"]) == {"ok": True},
                    "strict business quality")
        self.project_tool()
        after = self.drain("after-business")
        memory_contract(after, opened=True, floor=2048)
        require(after["q4t_requests_success_total"] == 5 and
                after["q4t_requests_error_total"] == 0 and
                after["q4t_requests_aborted_total"] == 0 and
                all(after[rejection_key(p, r)] == 0 for p in POINTS for r in REASONS),
                "normal business outcome/rejection mismatch")

    def model(self, blocked):
        self.budget("starts")
        self.folder = self.out / ("production-high-floor" if blocked else "production-mtp")
        self.folder.mkdir()
        binary = self.plan["binary"]
        argv = [binary["path"], "serve", "--model-dir", self.plan["model_dir"],
            "--host", "127.0.0.1", "--port", str(self.port), "--max-prefill", "2048",
            "--max-len", "8192", "--max-seq", "1", "--max-inflight-requests", "8",
            "--max-queued-requests", "2"]
        if blocked:
            argv += ["--min-available-memory-mib", "1048576"]
        env = {k:v for k,v in os.environ.items() if not k.startswith("Q4T_") and k != "LD_PRELOAD"}
        h.save(self.folder / "command.json", dict(argv=argv, binary=binary, validation_env={}))
        self.calls = []
        self.server = self.server_fd = None
        try:
            with (self.folder / "server.log").open("x") as log:
                self.server = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=log)
                self.server_fd = os.pidfd_open(self.server.pid)
            end = time.monotonic()+240
            while "serving on port" not in (self.folder / "server.log").read_text():
                self.budget()
                require(self.server.poll() is None and time.monotonic() < end, "model startup failed")
                time.sleep(0.25)
            config_reply = self.http("GET", "/v1/config")
            h.save(self.folder / "configuration.json", config_reply)
            config = json.loads(config_reply["raw"])
            self.config_schema.validate(config)
            require(config_reply["status"] == 200 and config["protocol_source_sha256"] ==
                    self.plan["protocol_sha256"] and config["runtime"]["mtp_enabled"] and
                    config["memory_admission"]["min_available_mib"]["effective"] ==
                    (1048576 if blocked else 2048), "configuration mismatch")
            caps = self.http("GET", "/v1/capabilities")
            h.save(self.folder / "capabilities.json", caps)
            require(caps["status"] == 200, "capabilities unavailable")
            (self.refused if blocked else self.business)()
            if not blocked:
                self.health("final")
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
                for call in self.calls:
                    call.thread.join(timeout=10)
                log = (self.folder / "server.log").read_text()
                clean = self.server.returncode == 0 and "shutdown complete (0 in-flight remaining)" in log
                h.save(self.folder / "shutdown.json", dict(clean=clean, returncode=self.server.returncode))
                if blocked:
                    observed = re.findall(r"\[q4t\]\[memory-admission\] trace_id=(\S+) checkpoint=(\S+) reason=(\S+)", log)
                    h.save(self.folder / "rejection-logs.json", observed)
                    require(len(observed) == 3 and all(p == "headers" and r == "memory_pressure"
                                                     for _, p, r in observed), "rejection log accounting")
                require(clean and all(not c.thread.is_alive() for c in self.calls), "owned shutdown")
            stage.a.require_idle_gpu(self.folder / "gpu-after.json")

    def run(self):
        result = dict(status="FAILED")
        try:
            self.model(True)
            self.model(False)
            require(self.counts == stage.PLANNED, "frozen case count")
            h.save(self.out / "wire-schema-result.json", dict(passed=True, records=self.wires))
            result.update(status="PASS_BOUNDED_MEMORY_ADMISSION", counts=self.counts,
                genuine_memory_exhaustion_tested=False, same_process_model_recovery_tested=False,
                recovery_evidence="native unloaded HTTP and queue contracts; simulated observations",
                native_raw_output_archived=False, performance_acceptance=False)
        finally:
            result.update(seconds=time.monotonic()-self.started, counts=self.counts)
            h.save(self.out / "result.json", result)
