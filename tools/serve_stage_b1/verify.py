"""Frozen role-profile SDK/HTTP quality and real project tool roundtrip."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


t = module("b1_transport", ROOT / "tools/serve_stage_a/verify.py")
d = module("b1_profile_cases", Path(__file__).with_name("template_diff.py"))
s, wire = t.s, t.wire


def simple(role="developer", *, stream=False, thinking=False):
    body = s.request("只回复数字710003。", stream=stream, thinking=thinking,
                     max_completion_tokens=128)
    body["messages"].insert(0, dict(role=role, content="遵循用户请求。"))
    return body


def command(binary, model, port, mtp):
    args = [str(binary), "serve", "--model-dir", str(model), "--host", "127.0.0.1",
            "--port", str(port), "--max-seq", "1", "--max-prefill", "2048",
            "--max-len", "8192"]
    if not mtp:
        args.append("--no-mtp")
    return args


def compare(left, right):
    wire.require(left.message == right.message and
                 left.finish_reason == right.finish_reason and
                 left.usage == right.usage,
                 "developer/system output or prompt/completion usage differs")


class Run(t.Run):
    def budget(self):
        wire.require(time.time() < self.deadline, "stage B1 deadline")
        wire.require(self.requests <= (1 if self.installed else 8) and
                     self.negative <= (0 if self.installed else 12) and
                     self.starts <= (1 if self.installed else 2) and
                     self.capability_gets <= (1 if self.installed else 2),
                     "stage B1 request/start budget")

    def success(self, folder, label, body):
        # Mutate the caller's history so the tool result roundtrip carries the
        # same instruction message, not a fresh instruction inserted afterward.
        if body["messages"][0]["role"] not in ("developer", "system"):
            body["messages"].insert(0, dict(role="developer", content="遵循用户请求。"))
        body.setdefault("extra_body", {})["request_timeout_ms"] = 180000
        return super().success(folder, label, body)

    def negatives(self, folder):
        before, _ = self.snapshot(folder, "before-negative")
        for index, (label, messages, param) in enumerate(d.negative_cases()):
            self.counted(negative=True)
            body = dict(messages=messages, stream=bool(index % 2), max_completion_tokens=1)
            raw = json.dumps(body, ensure_ascii=False).encode()
            status, _, value = self.exchange(folder, label,
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: " +
                str(len(raw)).encode() + b"\r\n\r\n" + raw)
            wire.require(status == 400, label + ": expected pre-stream 400")
            t.error_contract(value, "invalid_request", param)
        after, _ = self.snapshot(folder, "after-negative")
        for metric in ("q4t_prompt_tokens_total", "q4t_generation_tokens_total",
                       "q4t_structured_workers_started_total"):
            wire.require(before[metric] == after[metric], "rejected input performed work")
        self.resources_empty(folder, "negative-drained")

    def business(self, folder):
        self.negatives(folder)
        developer = self.success(folder, "developer-json", simple())
        system = self.success(folder, "system-json", simple("system"))
        compare(developer, system)
        wire.require(developer.finish_reason == "stop" and
                     developer.message["content"].strip() == "710003", "role quality")
        thinking = self.success(folder, "developer-thinking-sse",
                                simple(stream=True, thinking=True))
        wire.require(thinking.finish_reason == "stop" and
                     thinking.message["content"].strip() == "710003" and
                     bool(thinking.message.get("reasoning_content")), "thinking quality")
        self.project_loop(folder, strict=True, stream=True)
        self.resources_empty(folder, "business-drained")

    def plain(self, folder):
        result = self.success(folder, "developer-plain-sse", simple(stream=True))
        wire.require(result.finish_reason == "stop" and
                     result.message["content"].strip() == "710003", "plain quality")
        self.resources_empty(folder, "plain-drained")

    def health(self, folder):
        result = self.success(folder, "developer-installed", simple())
        wire.require(result.finish_reason == "stop" and
                     result.message["content"].strip() == "710003", "installed quality")
        self.resources_empty(folder, "installed-drained")

    def model(self, name, binary, mtp, action):
        self.starts += 1
        self.budget()
        folder = self.out / name
        folder.mkdir(mode=0o700)
        argv = command(binary, self.args.model_dir, self.args.port, mtp)
        env = {k: v for k, v in os.environ.items() if not k.startswith("Q4T_") and k != "LD_PRELOAD"}
        wire.save(folder / "command.json", dict(argv=argv, sha256=wire.digest(binary)))
        self.server = self.server_fd = None
        try:
            with (folder / "server.log").open("x") as log:
                self.server = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log,
                                               stderr=subprocess.STDOUT)
                self.server_fd = os.pidfd_open(self.server.pid)
                end = time.monotonic() + 240
                while "serving on port" not in (folder / "server.log").read_text():
                    self.budget()
                    wire.require(self.server.poll() is None and time.monotonic() < end,
                                 "server startup failed")
                    time.sleep(0.5)
                code, raw = self.request("GET", "/v1/capabilities")
                wire.save(folder / "capabilities.json", dict(status=code, raw=raw))
                caps = wire.validate_capabilities(json.loads(raw), "qwen3.8-flash-next",
                                                  protocol_sha256=self.args.protocol_sha256)
                wire.require(code == 200 and caps["runtime"]["mtp_enabled"] == mtp and
                    caps["template"]["message_profile"] == "qwen3.8-native-v2" and
                    caps["template"]["developer_mapping"] == "system", "capability mismatch")
                with self.sdk() as client:
                    models = client.models.list().model_dump(mode="json")
                    wire.save(folder / "models-sdk.json", models)
                    wire.require(any(m["id"] == "qwen3.8-flash-next" for m in models["data"]),
                                 "served model discovery failed")
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
            binary = ROOT / "build/q4t" if self.installed else Path(self.plan["candidate"])
            if self.installed:
                self.model("candidate-installed", binary, True, self.health)
            else:
                self.model("candidate-mtp", binary, True, self.business)
                self.model("candidate-plain", binary, False, self.plain)
            wire.require(self.requests == (1 if self.installed else 6) and
                         self.negative == (0 if self.installed else 12), "missing frozen cases")
            subprocess.run(["/usr/bin/python3", "-B",
                str(ROOT / "tools/serve_stage_a/check_schema.py"), str(self.out)],
                cwd=ROOT, check=True, timeout=30)
            state["status"] = "PASS_BOUNDED_SERVE_STAGE_B1"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(request_attempts=self.requests, negative_requests=self.negative,
                         starts=self.starts, seconds=time.monotonic() - self.started,
                         performance_acceptance=False)
            wire.save(self.out / "result.json", state)
