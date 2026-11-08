"""Pinned Node SDK transport for the existing journal/project query executor.

The Node child makes one request, validates full wire and tool batch, and never
executes tools. The Python executor keeps its durable before-side-effect log.
"""
import base64
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/examples"))
from chat_client import (_completion, _choices_completion, prepare_tool_calls,
                         require, validate_capabilities)
from project_records import VALIDATORS


class JsSdkError(RuntimeError):
    def __init__(self, details):
        self.details = details
        super().__init__(details.get("message", "Node SDK request failed"))


class JsSdkClient:
    def __init__(self, base_url, sdk_directory, *, protocol_sha256=None, timeout=180,
                 api_key=None, ca_file=None, connect_host=None):
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/v1"):
            self.base_url += "/v1"
        self.sdk_directory = Path(sdk_directory).resolve(strict=True)
        self.protocol_sha256, self.timeout = protocol_sha256, timeout
        self.connection = dict(api_key=api_key, ca_file=str(ca_file) if ca_file is not None else None,
                               connect_host=connect_host)
        self.last_result = None

    def call(self, action, request=None, *, model="qwen3.8-flash-next", sink=None):
        command = dict(action=action, base_url=self.base_url, model=model,
            protocol_sha256=self.protocol_sha256, timeout_ms=int(self.timeout*1000),
            request=request, tool_profile="project-records-v1", **self.connection)
        # A separate inherited pipe forwards raw bytes before the final JSON
        # result, including when the child fails. Only the parent owns the
        # journal writer. No ambient Node preload or module search override.
        env = {k: v for k, v in os.environ.items() if k not in
               ("NODE_OPTIONS", "NODE_PATH", "LD_PRELOAD", "Q4T_WIRE_FD")}
        env["Q4T_NODE_SDK_DIR"] = str(self.sdk_directory)
        read_fd, write_fd = os.pipe()
        env["Q4T_WIRE_FD"] = str(write_fd)
        try:
            process = subprocess.Popen(
                ["node", str(ROOT / "tools/serve_protocol/run_sdk.mjs")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=ROOT, pass_fds=(write_fd,))
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        chunks, capture_errors = [], []

        def capture():
            try:
                total = 0
                with os.fdopen(read_fd, "rb", buffering=0) as stream:
                    while chunk := stream.read(65536):
                        total += len(chunk)
                        require(total <= 8*1024*1024, "wire budget")
                        chunks.append(chunk)
                        if sink:
                            sink(chunk)
            except BaseException as error:
                capture_errors.append(error)
                if process.poll() is None:
                    process.terminate()

        reader = threading.Thread(target=capture, name="q4t-js-wire", daemon=True)
        reader.start()
        try:
            output, errors = process.communicate(
                json.dumps(command, ensure_ascii=False, allow_nan=False).encode(),
                timeout=self.timeout+15)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            reader.join(timeout=10)
        require(not reader.is_alive(), "capture pipe did not close")
        if capture_errors:
            raise capture_errors[0]
        require(len(output) <= 16*1024*1024, "bridge output limit")
        if not output:
            raise JsSdkError(dict(message="Node bridge failed before response",
                stderr=errors.decode(errors="replace"), returncode=process.returncode,
                captured_bytes=sum(map(len, chunks)), automatic_retry=False))
        payload = json.loads(output)
        self.last_result = payload
        require(payload["sdk_version"] == "7.47.0", "SDK bridge version drift")
        raw = base64.b64decode(payload["wire_base64"], validate=True)
        require(raw == b"".join(chunks), "live capture/bridge wire mismatch")
        if not payload["ok"]:
            require(process.returncode != 0, "bridge error exit mismatch")
            raise JsSdkError(payload["error"])
        require(process.returncode == 0 and not errors, "Node bridge diagnostics")
        return payload

    def capabilities(self, *, model="qwen3.8-flash-next", protocol_sha256=None,
                     template_sha256=None):
        payload = self.call("capabilities", model=model)
        return validate_capabilities(payload["result"], model,
            protocol_sha256=protocol_sha256 or self.protocol_sha256,
            template_sha256=template_sha256)

    def models(self):
        return self.call("models")["result"]

    def complete(self, request, *, sink=None):
        payload = self.call("chat", request, model=request["model"], sink=sink)
        value = payload["result"]
        if "choices" in value:
            return _choices_completion(value["id"], value["model"], value["choices"], value["usage"])
        completion = _completion(value["id"], value["model"], value["message"],
                                 value["finish_reason"], value["usage"], value.get("logprobs"))
        if completion.finish_reason in ("tool_calls", "function_call"):
            expected = [asdict(p) for p in prepare_tool_calls(completion, VALIDATORS)]
            require(payload["planned_tools"] == expected, "JS/Python tool plan disagreement")
        return completion
