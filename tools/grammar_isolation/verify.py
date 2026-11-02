"""Bounded worker interruption, real tool round-trip and installed health."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "grammar_resource_checks", ROOT / "tools/serve_resources/verify.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
wire = r.wire


def worker_contract(rows):
    wire.require(rows["q4t_structured_workers_active"] == 0 and
        rows["q4t_structured_workers_started_total"] ==
        rows["q4t_structured_workers_reaped_total"], "worker leak or missing reap")
    wire.require(0 < rows["q4t_structured_worker_reported_rss_bytes"] <= 2 * 1024**3,
                 "worker RSS outside measured bounded scope")


class Run(r.Run):
    def budget(self):
        wire.require(time.time() < self.deadline, "grammar stage deadline")
        wire.require(self.requests <= (2 if self.installed else 8) and
            self.negative <= (0 if self.installed else 1) and
            self.starts <= 1 and self.capability_gets <= 1, "grammar stage budget")

    def owned_worker(self):
        # Discover only a direct child of the server this driver created.
        children = set()
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                status = (proc / "status").read_text()
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            if f"PPid:\t{self.server.pid}\n" in status:
                children.add(int(proc.name))
        wire.require(len(children) == 1, "expected exactly one owned worker")
        pid = children.pop()
        descriptor = os.pidfd_open(pid)
        try:
            proc = Path(f"/proc/{pid}")
            command = (proc / "cmdline").read_bytes().split(b"\0")
            status = (proc / "status").read_text()
            wire.require(command[1] == b"--q4t-grammar-worker" and
                command[2] == str(self.server.pid).encode() and
                f"PPid:\t{self.server.pid}\n" in status, "worker identity mismatch")
            wire.require(os.path.samefile(proc / "exe", f"/proc/{self.server.pid}/exe"),
                         "worker executable differs from owned server")
            return descriptor, dict(pid=pid, status=status,
                limits=(proc / "limits").read_text(),
                command=[part.decode() for part in command if part])
        except BaseException:
            os.close(descriptor)
            raise

    def compile_interruption(self, folder, *, stall):
        label = "compile-timeout" if stall else "compile-cancel"
        before, _ = self.snapshot(folder, "before-" + label)
        body = r.long_request("grammar-" + label)
        sock = self.open_unread(folder, label, body)
        descriptor = None
        start = time.monotonic()
        try:
            self.observe(lambda m: m["q4t_structured_workers_active"] == 1 and
                         m["q4t_structured_preparing"] == 1 and
                         m["q4t_structured_workers_ready_total"] >
                         before["q4t_structured_workers_ready_total"], timeout=5)
            if stall:
                descriptor, identity = self.owned_worker()
                signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
                wire.save(folder / "stopped-worker.json", identity)
                # proc status is post-exec. wait4 ru_maxrss also includes the
                # inherited pre-exec mm and cannot be compared with this cap.
                vm_size = int(next(line for line in identity["status"].splitlines()
                    if line.startswith("VmSize:")).split()[1]) * 1024
                wire.require(vm_size <= 2 * 1024**3 and
                    "2147483648" in identity["limits"], "worker AS limit absent")
                rows, _ = self.snapshot(folder, "worker-stopped")
                wire.require(rows["q4t_structured_preparing"] == 1 and
                    rows["q4t_target_ready_seconds_count"] ==
                    before["q4t_target_ready_seconds_count"],
                    "worker passed preparation before controlled stop")
            else:
                code, raw = self.request("POST", "/v1/requests/cancel",
                    dict(request_id=body["request_id"], cancel_token=body["cancel_token"]))
                wire.save(folder / (label + "-accepted.json"), dict(status=code, raw=raw))
                wire.require(code == 202, "compile cancellation not accepted")
            response = self.receive(sock)
            (folder / (label + "-wire.bin")).write_bytes(response)
            elapsed = time.monotonic() - start
            if stall:
                head, _, content = response.partition(b"\r\n\r\n")
                wire.require(head.startswith(b"HTTP/1.1 503 ") and
                    json.loads(content)["error"]["code"] == "constraint_compile_timeout",
                    "stalled compile did not produce bounded timeout")
                wire.require(9 <= elapsed <= 14, "compile timeout outside bounded window")
            else:
                r.cancellation_wire(response, "explicit_cancel")
                wire.require(elapsed < 5, "compile cancellation did not interrupt")
            self.observe(lambda m: m["q4t_structured_workers_active"] == 0 and
                         m["q4t_structured_active"] == 0)
            after, _ = self.snapshot(folder, "after-" + label)
            worker_contract(after)
            wire.require(after["q4t_generation_tokens_total"] ==
                before["q4t_generation_tokens_total"], "failed preparation generated tokens")
            wire.save(folder / (label + "-result.json"), dict(passed=True,
                seconds=elapsed, stopped_owned_worker=stall))
        finally:
            # The server owns/reaps its child. Closing pidfd does not release a
            # stopped child: on error, model() finally shuts down that server.
            if descriptor is not None:
                os.close(descriptor)
            sock.close()

    def capabilities(self, folder):
        caps = json.loads(json.loads((folder / "capabilities.json").read_text())["raw"])
        resource = caps["structured_output"]["resources"]
        wire.require(resource["compile_interruptible"] and
            resource["worker_address_space_bytes"] == 2 * 1024**3 and
            resource["compile_timeout_ms"] == 10000 and
            resource["match_timeout_ms"] == 2000 and
            resource["worker_memory_enforcement"] == "RLIMIT_AS" and
            not resource["whole_server_ram_limit"], "isolation capability mismatch")

    def business(self, folder):
        self.capabilities(folder)
        self.compile_interruption(folder, stall=True)
        self.compile_interruption(folder, stall=False)
        self.mixed_cancel(folder)
        self.deadline_case(folder)
        self.project_loop(folder, strict=True, stream=True)
        rows, raw = self.snapshot(folder, "before-final-shutdown")
        r.metric_contract(raw)
        worker_contract(rows)
        wire.require(rows["q4t_structured_compile_timeouts_total"] == 1 and
            rows["q4t_structured_prepare_failed_total"] == 1 and
            rows["q4t_structured_resource_exhausted_total"] == 0,
            "unexpected grammar failure accounting")
        wire.require(rows['q4t_requests_aborted_by_reason_total{reason="explicit_cancel"}'] == 2,
                     "compile/matching cancellation accounting")
        self.shutdown_case(folder)

    def health(self, folder):
        self.capabilities(folder)
        super().health(folder)
        self.observe(lambda m: m["q4t_structured_workers_active"] == 0)
        rows, raw = self.snapshot(folder, "installed-health")
        r.metric_contract(raw)
        worker_contract(rows)

    def run(self):
        state = dict(status="FAILED")
        try:
            binary = ROOT / "build/q4t" if self.installed else Path(self.plan["candidate"])
            self.model("candidate-installed" if self.installed else "candidate-mtp",
                binary, True, self.health if self.installed else self.business)
            wire.require(self.requests == (2 if self.installed else 8) and
                self.negative == (0 if self.installed else 1), "missing frozen cases")
            state["status"] = "PASS_BOUNDED_GRAMMAR_ISOLATION"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(request_attempts=self.requests, overload_rejections=self.negative,
                starts=self.starts, seconds=time.monotonic() - self.started,
                performance_acceptance=False, worker_address_space_limit=2 * 1024**3,
                whole_server_ram_limit=False, compile_timeout_ms=10000)
            wire.save(self.out / "result.json", state)
