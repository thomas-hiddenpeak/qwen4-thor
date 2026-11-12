"""A03 real sampling/choice/logprob business acceptance under one GPU lease."""
import argparse
import copy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import threading
import math

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_grammar_reap import acceptance as lifecycle
from tools.serve_protocol.js_client import JsSdkClient, JsSdkError
from tools.serve_protocol.stage import sdk_identity
from tools.serve_recovery.bundle import assets, check_assets
from tools.serve_service.package import require, save, sha
from tools.examples.project_records import ProjectRecords, TOOLS, VALIDATORS
from tools.examples.chat_client import prepare_tool_calls, append_tool_results

LIMITS = dict(starts=2, chats=12, controls=2000, seconds=900)
DOCUMENT = "docs/MODEL.md"


def requests(bias_token):
    base = dict(model="qwen3.8-flash-next", enable_thinking=False,
                max_completion_tokens=32, stream=False,
                messages=[dict(role="user", content="Compute 17 + 25. Reply only with the decimal number.")])
    sampled = dict(base, temperature=0.8, top_p=0.95, n=2, seed=54321,
        presence_penalty=0.2, frequency_penalty=0.3, logprobs=True, top_logprobs=3)
    strict = dict(sampled, max_completion_tokens=128, stream=True,
        stream_options=dict(include_usage=True),
        response_format=dict(type="json_schema", json_schema=dict(name="sum", strict=True,
            schema=dict(type="object", properties=dict(answer=dict(type="string", enum=["42"])),
                        required=["answer"], additionalProperties=False))))
    tools = copy.deepcopy(TOOLS[:1])
    tools[0]["function"]["strict"] = True
    tools[0]["function"]["parameters"]["properties"]["path"] = dict(type="string", enum=[DOCUMENT])
    tool = dict(base, max_completion_tokens=256, temperature=0.6, seed=17,
        logprobs=True, top_logprobs=2, stream=True, stream_options=dict(include_usage=True),
        messages=[dict(role="user", content="Read the first 16 lines of docs/MODEL.md with read_project_record. Then return the exact title and citation from the tool.")],
        tools=tools, tool_choice=dict(type="function", function=dict(name="read_project_record")),
        parallel_tool_calls=False)
    cancel = dict(base, n=2, seed=23, max_completion_tokens=1024, logprobs=True,
        request_id="sampling-cancel", cancel_token="c"*64, stream=True,
        stream_options=dict(include_usage=True), messages=[dict(role="user", content=
            "Write a long essay of at least 1000 words about the history of mathematics.")])
    return dict(default=base, sampled_json=sampled,
        sampled_sse=dict(sampled, stream=True, stream_options=dict(include_usage=True)),
        bias=dict(base, temperature=0.7, seed=9, max_completion_tokens=1,
                  logprobs=True, top_logprobs=3, logit_bias={str(bias_token):100}),
        strict=strict, tool=tool, cancel=cancel, greedy=dict(base, temperature=0))


def require_probs(choice):
    value = choice.get("logprobs")
    require(isinstance(value, dict) and value["refusal"] is None,
            "missing real token probabilities")
    for item in value["content"]:
        require(math.isfinite(item["logprob"]) and item["logprob"] <= 0,
                "invalid selected probability")
        alternatives = item["top_logprobs"]
        require(len(alternatives) <= 20 and all(a["logprob"] >= b["logprob"]
            for a, b in zip(alternatives, alternatives[1:])), "raw top ordering")
    text = choice["message"].get("content")
    if text:
        require(value["content"], "visible content without probability")
        decoded = bytes(byte for item in value["content"] for byte in item["bytes"]).decode("utf-8", errors="replace")
        require(decoded == text, "plain business token bytes/content differ")


def require_choices(value, *, structured=False):
    rows = value.choices
    require(len(rows) == 2 and [r["index"] for r in rows] == [0, 1], "independent choice indexes")
    for row in rows:
        require(row["finish_reason"] == "stop" and not row["message"].get("tool_calls"), "unexpected choice finish")
        content = row["message"]["content"]
        require((json.loads(content) == {"answer":"42"}) if structured else content.strip() == "42", "arithmetic choice quality")
        require_probs(row)
    return rows


def prepare(args):
    out = args.output.resolve()
    require(out.is_relative_to(ROOT / ".q4t-work"), "output must be in workspace artifacts")
    require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
            "commit reviewed source before freezing")
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    binary = args.binary.resolve(strict=True)
    model = Path.home() / "models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream"
    names = subprocess.check_output(["git", "ls-files", "src", "include", "cmake",
        "CMakeLists.txt", "tools/serve_sampling", "tools/serve_chat_input", "tools/serve_grammar_reap",
        "tools/serve_protocol", "tools/examples", "tools/evalscope", "tools/serve_recovery",
        "tools/serve_service", "tools/serve_gateway", "tools/release", DOCUMENT],
        cwd=ROOT, text=True).splitlines()
    protected = [ROOT / name for name in ("build/q4t", "build/q4t.release.json",
        "build/service.release.json", "build/service/manifest.json")]
    plan = dict(version=1, limits=LIMITS, output=str(out), binary=str(binary),
        binary_sha256=sha(binary), model=str(model), model_assets=assets(model),
        port=args.port, operations_port=args.port+1, bias_token=args.bias_token,
        requests=requests(args.bias_token),
        sources={name: sha(ROOT/name) for name in names},
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        protected={str(p): sha(p) for p in protected}, sdk=sdk_identity(),
        planned=dict(starts=1, chats=9), automatic_retries=False,
        acceptance="Sampling/choice/logprobs, real tool closure, cancellation and cleanup; not performance or full model quality")
    save(out / "plan.json", plan)
    print(json.dumps(dict(plan=str(out/"plan.json"), sources=len(names))))


class Stage(lifecycle.Stage):
    # Reuse the already exercised owned-process/lease/cleanup implementation.
    # Its fault injection methods are never called by this stage.
    def __init__(self, plan):
        self.plan, self.out = plan, Path(plan["output"])
        self.started = time.monotonic()
        self.counts = dict(starts=0, chats=0, controls=0)
        self.model = self.model_identity = self.fault_thread = None
        self.history, self.fault_result = {}, {}
        self.lease = int(os.environ["Q4T_MANAGED_LEASE_FD"])
        lifecycle.verify_lease(self.lease, lifecycle.guard.common_lock())
        require(plan["limits"] == LIMITS and plan["requests"] == requests(plan["bias_token"]), "plan drift")
        for name, digest in plan["sources"].items():
            require(sha(ROOT/name) == digest, "source drift: "+name)
        for name, digest in plan["protected"].items():
            require(sha(name) == digest, "default changed: "+name)
        require(sha(plan["binary"]) == plan["binary_sha256"], "binary changed")
        require(sdk_identity() == plan["sdk"], "SDK identity changed")
        check_assets(plan["model"], plan["model_assets"])
        self.client = JsSdkClient(f"http://127.0.0.1:{plan['port']}/v1",
            plan["sdk"]["sdk_directory"], timeout=90)

    def budget(self, kind):
        require(time.monotonic()-self.started < LIMITS["seconds"]-40, "stage deadline")
        require(self.counts[kind] < LIMITS[kind], "attempt limit: "+kind)
        self.counts[kind] += 1
        save(self.out/"counts.json", self.counts)

    def sdk_request(self, label, body):
        # Preflight uses the same preparation as generation. It does not take
        # an execution slot or count as an extra Chat/model attempt.
        self.budget("controls")
        status, raw = self.http("/v1/chat/preflight", dict(request=body,
            include_prompt=True, include_token_ids=True), label=label+"-preflight")
        require(status == 200, "preflight failed: "+label)
        self.budget("chats")
        save(self.out/(label+".request.json"), body)
        with (self.out/(label+".wire")).open("xb") as wire:
            value = self.client.complete(body, sink=wire.write)
        save(self.out/(label+".sdk.json"), self.client.last_result)
        save(self.out/(label+".result.json"), asdict(value))
        self.same_model()
        self.drained(label+"-drain")
        return value

    def cancel_multi(self):
        body = self.plan["requests"]["cancel"]
        self.budget("chats")
        save(self.out/"cancel.request.json", body)
        visible = threading.Event()
        observed = {}
        wire = self.out/"cancel.wire"
        def invoke():
            pending = bytearray()
            try:
                with wire.open("xb") as capture:
                    def sink(chunk):
                        capture.write(chunk)
                        pending.extend(chunk)
                        while b"\r\n\r\n" in pending:
                            frame, rest = pending.split(b"\r\n\r\n", 1)
                            pending[:] = rest
                            if frame.startswith(b"data: {"):
                                event = json.loads(frame[6:])
                                for choice in event.get("choices", []):
                                    if choice.get("delta", {}).get("content"):
                                        visible.set()
                    self.client.complete(body, sink=sink)
                observed["unexpected_success"] = True
            except JsSdkError as error:
                observed["error"] = error.details
            except BaseException:
                observed["unexpected_error"] = traceback.format_exc()
        self.fault_thread = threading.Thread(target=invoke, name="sampling-cancel-business")
        self.fault_thread.start()
        require(visible.wait(60), "no generated content before cancellation")
        self.budget("controls")
        status, raw = self.http("/v1/requests/cancel", {key:body[key] for key in
            ("request_id", "cancel_token")}, label="cancel-control")
        require(status == 202, "explicit cancellation rejected")
        self.fault_thread.join(30)
        require(not self.fault_thread.is_alive(), "cancelled request did not finish")
        save(self.out/"cancel.result.json", observed)
        require(observed.get("error", {}).get("code") == "explicit_cancel" and
                not observed.get("unexpected_success") and not observed.get("unexpected_error"),
                "cancelled multi-choice was not a final error")
        raw = wire.read_bytes()
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b"data: {")]
        require(raw.count(b"data: [DONE]") == 1 and events[-1]["error"]["code"] == "explicit_cancel",
                "cancel terminal incomplete")
        require(not any(e.get("usage") for e in events) and not any(c.get("finish_reason")
            for e in events for c in e.get("choices", [])), "cancelled request claimed success")
        self.same_model()
        self.drained("cancel-drain")

    def business(self):
        caps = json.loads((self.out/"capabilities.json").read_text())
        require(caps["chat"]["sampling_profile"] == "q4t-sampling-v1", "sampling profile mismatch")
        self.client.protocol_sha256 = caps["protocol_source_sha256"]
        self.budget("controls")
        save(self.out/"sdk-capabilities.json", self.client.capabilities())
        default = self.sdk_request("default", self.plan["requests"]["default"])
        require(default.finish_reason == "stop" and default.message["content"].strip() == "42", "default sampling quality")
        first = self.sdk_request("sampled-json", self.plan["requests"]["sampled_json"])
        second = self.sdk_request("sampled-sse", self.plan["requests"]["sampled_sse"])
        require(require_choices(first) == require_choices(second), "seed/choice/JSON-SSE equivalence")
        require(first.usage == second.usage, "choice usage differs by transport")
        bias = self.sdk_request("bias", self.plan["requests"]["bias"])
        require(bias.finish_reason == "length" and bias.message["content"] == "7", "bias did not override target preference")
        require_probs(asdict(bias))
        strict = self.sdk_request("strict", self.plan["requests"]["strict"])
        require_choices(strict, structured=True)
        body = self.plan["requests"]["tool"]
        answer = self.sdk_request("tool", body)
        calls = prepare_tool_calls(answer, VALIDATORS)
        require(len(calls) == 1 and calls[0].arguments ==
            dict(path=DOCUMENT, start_line=1, line_count=16), "tool arguments differ")
        result = ProjectRecords(ROOT).execute(calls[0])
        require(result["ok"] and result["source"]["sha256"] == self.plan["sources"][DOCUMENT],
                "real source identity differs")
        save(self.out/"tool-execution.json", dict(executions=1, result=result))
        follow = copy.deepcopy(body)
        follow["tool_choice"] = "none"
        follow["messages"] = append_tool_results(body["messages"], answer,
            {calls[0].call_id: json.dumps(result, ensure_ascii=False)})
        follow["messages"].append(dict(role="developer", content=
            "Return the first line title and the exact citation supplied by the tool. No more tool calls."))
        final = self.sdk_request("tool-final", follow)
        require(final.finish_reason == "stop" and not final.message.get("tool_calls") and
            "Qwen3.8-Flash-Next" in final.message["content"] and
            result["source"]["citation"] in final.message["content"], "real document closure")
        require_probs(asdict(final))
        self.cancel_multi()
        greedy = self.sdk_request("greedy-after-cancel", self.plan["requests"]["greedy"])
        require(greedy.finish_reason == "stop" and greedy.message["content"].strip() == "42", "greedy continuation quality")
        metrics = self.drained("final")
        require(metrics["q4t_target_ready_seconds_count"] == 9 and
                metrics["q4t_slot_wait_seconds_count"] == 9,
                "per-request timing counted individual choices")
        require(metrics["q4t_requests_total"] == 9 and
                metrics["q4t_requests_success_total"] == 8 and
                metrics["q4t_requests_aborted_total"] == 1 and
                metrics["q4t_requests_error_total"] == 0, "choice/replay/cancellation accounting")

    def run(self):
        result = dict(status="FAILED")
        try:
            self.start()
            self.business()
            result["status"] = "PASS_SAMPLING_BUSINESS"
        except BaseException:
            result["error"] = traceback.format_exc()
            raise
        finally:
            try:
                self.cleanup()
            except BaseException:
                result["status"] = "FAILED"
                result["cleanup_error"] = traceback.format_exc()
                raise
            finally:
                result.update(counts=self.counts, seconds=time.monotonic()-self.started,
                              binary_sha256=self.plan["binary_sha256"])
                save(self.out/"result.json", result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    stage = sub.add_parser("prepare")
    stage.add_argument("--binary", type=Path, required=True)
    stage.add_argument("--output", type=Path, required=True)
    stage.add_argument("--port", type=int, default=18164)
    stage.add_argument("--bias-token", type=int, required=True)
    run = sub.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args)
    else:
        with lifecycle.child_subreaper():
            Stage(json.loads(args.plan.read_text())).run()


if __name__ == "__main__":
    main()
