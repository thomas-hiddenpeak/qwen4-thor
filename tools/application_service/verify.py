"""Bounded application/SDK checks; always run under the shared GPU lease."""
import argparse
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/response_protocol"),
               str(ROOT / "tools/examples"),
               str(ROOT / "tools/verify/startup_delivery")]
# This script has the same basename as the old verifier; load explicitly.
import importlib.util
spec = importlib.util.spec_from_file_location("chat_wire",
    ROOT / "tools/response_protocol/verify.py")
wire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wire)
import deploy as delivery
from acceptance import install_interrupt_handlers
from chat_client import ChatClient, append_tool_results, prepare_tool_calls
import project_query
from project_records import ProjectRecords, TOOLS, VALIDATORS
from tool_recovery import inspect_recovery


class Run(wire.Run):
    def __init__(self, plan, output, *, installed=False):
        self.plan, self.installed = plan, installed
        self.deadline = min(time.time() + (300 if installed else 1200),
            plan.get("original_started", time.time()) + 1800 - 30)
        super().__init__(SimpleNamespace(output=Path(output), port=18098,
            model_dir=Path(plan["model_dir"]),
            protocol_sha256=plan["protocol_sha256"]))
        self.out.mkdir(mode=0o700)

    def budget(self):
        wire.require(time.time() < self.deadline, "application stage deadline")
        limit = 11 if self.plan.get("reuse_sdk_http") else 15
        wire.require(self.requests <= (1 if self.installed else limit) and
                     self.negative <= 12 and self.starts <=
                     (1 if self.installed else 2) and self.capability_gets <= 8,
                     "application stage request/start budget")

    def counted(self, negative=False):
        if negative:
            self.negative += 1
        else:
            self.requests += 1
        self.budget()

    def sdk(self):
        import openai
        wire.require(openai.__version__ == self.plan["sdk_version"],
                     "SDK version changed")
        return openai.OpenAI(base_url=f"http://127.0.0.1:{self.args.port}/v1",
            api_key="local-q4t-no-auth", max_retries=0, timeout=120,
            _strict_response_validation=True,
            http_client=openai.DefaultHttpxClient(trust_env=False))

    def sdk_chat(self, client, folder, label, request, *, error_status=None):
        import openai
        self.counted(error_status is not None)
        wire.save(folder / (label + "-request.json"), request)
        start = time.monotonic()
        try:
            if request.get("stream"):
                chunks, raw = [], bytearray()
                with client.chat.completions.create(**request) as stream:
                    original = stream.response.iter_bytes

                    def capture(*args, **kwargs):
                        for data in original(*args, **kwargs):
                            raw.extend(data)
                            yield data

                    with patch.object(stream.response, "iter_bytes", capture):
                        for chunk in stream:
                            chunks.append(chunk.model_dump(mode="json"))
                (folder / (label + "-wire.bin")).write_bytes(raw)
                wire.save(folder / (label + "-sdk.json"), chunks)
                wire.require(len({c["created"] for c in chunks}) == 1,
                             "SSE created changed within a completion")
                result = wire.read_sse(io.BytesIO(raw))
                sdk_content = "".join(c["choices"][0]["delta"].get("content")
                    or "" for c in chunks if c["choices"])
                wire.require(sdk_content == (result.message["content"] or ""),
                             "SDK/wire content mismatch")
            else:
                response = client.chat.completions.with_raw_response.create(
                    **request)
                raw = response.http_response.content
                (folder / (label + "-wire.bin")).write_bytes(raw)
                parsed = response.parse()
                wire.save(folder / (label + "-sdk.json"),
                          parsed.model_dump(mode="json"))
                result = wire.read_json(io.BytesIO(raw))
                wire.require(parsed.choices[0].message.content ==
                             result.message["content"], "SDK/wire mismatch")
            wire.require(error_status is None, "expected error was accepted")
            wire.save(folder / (label + "-result.json"),
                      dict(**asdict(result), seconds=time.monotonic() - start))
            self.wait_slot(label)
            return result
        except openai.APIStatusError as error:
            wire.save(folder / (label + "-error.json"), dict(
                status=error.status_code, raw=error.response.text,
                sdk_class=type(error).__name__, code=error.code,
                type=error.type, param=error.param))
            wire.require(error_status == error.status_code, "unexpected HTTP error")
            body = json.loads(error.response.text)["error"]
            wire.require(set(body) == {"message", "type", "code", "param"} and
                         body["type"] == "invalid_request_error" and
                         body["code"] == "invalid_request", "error contract")
            return None

    @staticmethod
    def request_body(stream=False):
        request = dict(model="qwen3.8-flash-next", temperature=0,
            max_completion_tokens=64, stream=stream,
            messages=[dict(role="user", content="只回复数字710003。")],
            extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        if stream:
            request["stream_options"] = {"include_usage": True}
        return request

    def query(self, folder, label, question, stream, expected):
        stage = self

        class CountedClient(ChatClient):
            def complete(self, request, **kwargs):
                stage.counted()
                return super().complete(request, **kwargs)

            def capabilities(self, **kwargs):
                stage.capability_gets += 1
                stage.budget()
                return super().capabilities(**kwargs)

        url = f"http://127.0.0.1:{self.args.port}"
        client = CountedClient(url)
        # No proxies or redirects, even when the developer's shell has proxies.
        import urllib.request
        client.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), wire._NoRedirect())
        answer = project_query.run(folder / label, url, question, stream=stream,
                                  max_rounds=4 if label == "acceptance-query"
                                             else 3, client=client)
        wire.require(expected in answer, label + ": business answer incorrect")
        recovered = inspect_recovery(folder / label)
        wire.require(recovered["state"] == "COMPLETED", label + ": journal invalid")
        wire.save(folder / (label + "-business.json"), dict(answer=answer,
            expected=expected, recovery=recovered))
        self.wait_slot(label)

    def sdk_checks(self, folder):
        with self.sdk() as client:
            models = client.models.list()
            wire.save(folder / "sdk-models.json", models.model_dump(mode="json"))
            wire.require([m.id for m in models.data] == ["qwen3.8-flash-next"],
                         "model discovery")
            for stream in (False, True):
                result = self.sdk_chat(client, folder, f"text-{stream}",
                                       self.request_body(stream))
                wire.require(result.message["content"].strip() == "710003" and
                             result.finish_reason == "stop", "text quality")
            limited = self.request_body()
            limited["max_completion_tokens"] = 1
            result = self.sdk_chat(client, folder, "limit-one", limited)
            wire.require(result.usage["completion_tokens"] == 1 and
                         result.finish_reason == "length", "budget not enforced")
            negatives = [dict(max_tokens=64), dict(max_completion_tokens=0),
                         dict(tool_choice="required"), dict(model="unknown")]
            for name in ("name", "refusal", "reasoning"):
                message = dict(role="user", content="hello", **{name: "lost"})
                negatives.append(dict(messages=[message]))
            negatives.append(dict(messages=[dict(role="user", content=[
                dict(type="file", text="silently lost")])]))
            for i, values in enumerate(negatives):
                request = self.request_body()
                request.update(values)
                self.sdk_chat(client, folder, f"invalid-{i}", request,
                              error_status=400)
        self.counted(negative=True)
        status, raw = self.request("GET", "/v1/responses")
        wire.save(folder / "unsupported-route.json", dict(status=status, raw=raw))
        wire.require(status == 404 and json.loads(raw)["error"]["code"] ==
                     "not_found", "unsupported route error")

    def ordinary(self, folder):
        if not self.plan.get("reuse_sdk_http"):
            self.sdk_checks(folder)
        previous = self.plan["previous_binary_sha256"]
        self.query(folder, "release-query",
            "调用read_project_record读取build/q4t.release.json第1行起20行。"
            "回答已安装程序的binary_sha256完整值，并引用来源。", False, previous)
        self.query(folder, "acceptance-query",
            "调用read_project_record读取docs/CHAT_DELIVERY_2026-11-02.md"
            "第1行起45行。该次聊天交付的完整head与subset head工具往返"
            "验证得到的六位数字是什么？请给出数字并引用来源。", True, "831907")
        self.query(folder, "missing-query",
            "调用read_project_record读取docs/NO_SUCH_ACCEPTANCE_RECORD.md"
            "第1行起10行。如果找不到，报告错误码并说明无法核实，"
            "不要搜索其他文档或编造验收结果。", True, "record_not_found")

    def mtp(self, folder):
        request = self.request_body()
        request.update(max_completion_tokens=512, tools=[TOOLS[0]],
            tool_choice="auto", messages=[dict(role="user", content=
            "先调用read_project_record读取build/q4t.release.json，"
            "start_line=1，line_count=10。然后只回答工具结果中"
            "binary_sha256的完整值。")])
        with self.sdk() as client:
            first = self.sdk_chat(client, folder, "sdk-tool", request)
            invocations = prepare_tool_calls(first, VALIDATORS)
            wire.require(len(invocations) == 1, "unexpected SDK tool batch")
            results = {call.call_id: json.dumps(ProjectRecords(ROOT).execute(call),
                       ensure_ascii=False) for call in invocations}
            wire.save(folder / "sdk-tool-results.json", results)
            request["messages"] = append_tool_results(request["messages"],
                                                       first, results)
            request.update(stream=True, stream_options={"include_usage": True})
            final = self.sdk_chat(client, folder, "sdk-tool-final", request)
            wire.require(final.finish_reason == "stop" and
                self.plan["previous_binary_sha256"] in final.message["content"],
                "SDK tool round trip failed")

    def health(self, folder):
        with self.sdk() as client:
            result = self.sdk_chat(client, folder, "installed-sdk-sse",
                                   self.request_body(True))
            wire.require(result.message["content"].strip() == "710003" and
                         result.finish_reason == "stop", "installed quality")

    def run(self):
        binary = ROOT / "build/q4t" if self.installed else Path(self.plan["candidate"])
        status = "FAIL"
        try:
            if self.installed:
                self.model("candidate-installed", binary, True, self.health)
            else:
                self.model("candidate-ordinary", binary, False, self.ordinary)
                self.model("candidate-mtp", binary, True, self.mtp)
            # Inspect every actual reservation finalization, not only exit 0.
            for path in self.out.glob("*/server.log"):
                records = [json.loads(line.split("] ", 1)[1])
                    for line in path.read_text().splitlines()
                    if line.startswith("[startup-reservation] ") and
                    '"event":"finalized"' in line]
                wire.require(len(records) == 1 and records[0]["status"] == "PASS"
                    and all(records[0][key] == 0 for key in
                    ("handed_live", "unhanded_remaining", "release_unknown")),
                    "model resources not fully released")
            status = "PASS_BOUNDED_APPLICATION_SERVICE"
        finally:
            wire.save(self.out / "result.json", dict(status=status,
                candidate_sha256=wire.digest(binary), sdk=self.plan["sdk_version"],
                requests=self.requests, negatives=self.negative, starts=self.starts,
                capability_gets=self.capability_gets,
                seconds=time.monotonic() - self.started))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lease-output", type=Path, required=True)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    delivery.require_guard(args.lease_output.resolve())
    install_interrupt_handlers()
    Run(json.loads(args.plan.read_text()), args.output,
        installed=args.installed).run()


if __name__ == "__main__":
    main()
