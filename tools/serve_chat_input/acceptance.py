"""A01/A02 bounded SDK business acceptance; one model, six planned Chat calls."""
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

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_grammar_reap import acceptance as lifecycle
from tools.serve_protocol.js_client import JsSdkClient
from tools.serve_protocol.stage import sdk_identity
from tools.serve_recovery.bundle import assets, check_assets
from tools.serve_service.package import require, save, sha
from tools.examples.project_records import ProjectRecords, TOOLS, VALIDATORS
from tools.examples.chat_client import prepare_tool_calls, append_tool_results

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
DOCUMENT = "docs/MODEL.md"


def requests():
    base = dict(model="qwen3.8-flash-next", enable_thinking=False,
                max_completion_tokens=128, temperature=0, stream=False)
    ordered = dict(base, messages=[dict(role="system", content="Answer concisely."),
        dict(role="developer", content="Return only a number."),
        dict(role="user", content="What is 17 + 25?"),
        dict(role="developer", content="Return only the decimal answer, without explanation.")])
    named = dict(base, stream=True, stream_options=dict(include_usage=True), messages=[
        dict(role="system", content="Remember each named participant's code. Answer only the requested code."),
        dict(role="user", name="Alice", content="My code is 314159."),
        dict(role="user", name="Bob", content="My code is 271828."),
        dict(role="user", name="Auditor", content="What is Alice's code? Return only those six digits.")])
    refusal = dict(base, messages=[dict(role="user", content="Perform an unavailable operation."),
        dict(role="assistant", content=[dict(type="refusal", refusal="I cannot perform that operation.")]),
        dict(role="user", content="Compute 17 + 25. Return only the number.")])
    no_user = dict(base, messages=[dict(role="developer", name="calculator",
        content="Compute 17 + 25 and reply with only the number.")])
    tools = copy.deepcopy(TOOLS[:1])
    tools[0]["function"]["strict"] = True
    tools[0]["function"]["parameters"]["properties"]["path"] = dict(type="string", enum=[DOCUMENT])
    tool = dict(base, max_completion_tokens=256, stream=True,
        stream_options=dict(include_usage=True), messages=[
            dict(role="system", content="Use the project records tool for source-backed answers."),
            dict(role="developer", content="Read only the requested project document."),
            dict(role="user", name="Researcher", content="Read the first 16 lines of docs/MODEL.md with read_project_record. Then return the exact title and citation from the tool.")],
        tools=tools, tool_choice=dict(type="function", function=dict(name="read_project_record")),
        parallel_tool_calls=False)
    return dict(ordered=ordered, named=named, refusal=refusal, no_user=no_user, tool=tool)


def require_answer(value, expected):
    require(value.finish_reason == "stop" and
            value.message.get("content", "").strip() == expected and
            not value.message.get("tool_calls"), "message semantics quality")


def prepare(args):
    out = args.output.resolve()
    require(out.is_relative_to(ROOT / ".q4t-work"), "output must be in workspace artifacts")
    require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
            "commit reviewed source before freezing")
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    binary = args.binary.resolve(strict=True)
    model = Path.home() / "models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream"
    names = subprocess.check_output(["git", "ls-files", "src", "include", "cmake",
        "CMakeLists.txt", "tools/serve_chat_input", "tools/serve_grammar_reap",
        "tools/serve_protocol", "tools/examples", "tools/evalscope", "tools/serve_recovery",
        "tools/serve_service", "tools/serve_gateway", "tools/release", DOCUMENT],
        cwd=ROOT, text=True).splitlines()
    protected = [ROOT / name for name in ("build/q4t", "build/q4t.release.json",
        "build/service.release.json", "build/service/manifest.json")]
    plan = dict(version=1, limits=LIMITS, output=str(out), binary=str(binary),
        binary_sha256=sha(binary), model=str(model), model_assets=assets(model),
        port=args.port, operations_port=args.port+1, requests=requests(),
        sources={name: sha(ROOT/name) for name in names},
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        protected={str(p): sha(p) for p in protected}, sdk=sdk_identity(),
        planned=dict(starts=1, chats=6), automatic_retries=False,
        acceptance="Input semantics and actual SDK/tool closure; not performance or full model quality")
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
        require(plan["limits"] == LIMITS and plan["requests"] == requests(), "plan drift")
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

    def business(self):
        caps = json.loads((self.out/"capabilities.json").read_text())
        require(caps["template"]["message_profile"] == "qwen3.8-openai-v3", "profile mismatch")
        self.client.protocol_sha256 = caps["protocol_source_sha256"]
        self.budget("controls")
        save(self.out/"sdk-capabilities.json", self.client.capabilities())
        for label, expected in (("ordered", "42"), ("named", "314159"),
                                ("refusal", "42"), ("no_user", "42")):
            value = self.sdk_request(label, self.plan["requests"][label])
            require_answer(value, expected)
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
        follow["messages"][-1]["name"] = "read_project_record"
        follow["messages"].append(dict(role="developer", content=
            "Return the first line title and the exact citation supplied by the tool. No more tool calls."))
        final = self.sdk_request("tool-final", follow)
        require(final.finish_reason == "stop" and not final.message.get("tool_calls") and
            "Qwen3.8-Flash-Next" in final.message["content"] and
            result["source"]["citation"] in final.message["content"], "real document closure")
        metrics = self.drained("final")
        require(metrics["q4t_requests_total"] == 6 and
                metrics["q4t_requests_success_total"] == 6 and
                metrics["q4t_requests_error_total"] == 0, "unexpected replay or error")

    def run(self):
        result = dict(status="FAILED")
        try:
            self.start()
            self.business()
            result["status"] = "PASS_CHAT_INPUT_BUSINESS"
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
