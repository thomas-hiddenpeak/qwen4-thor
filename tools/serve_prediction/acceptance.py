"""A03 predicted output target-verification business acceptance under one GPU lease."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import threading

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_grammar_reap import acceptance as lifecycle
from tools.serve_protocol.js_client import JsSdkClient, JsSdkError
from tools.serve_protocol.stage import sdk_identity
from tools.serve_recovery.bundle import assets, check_assets
from tools.serve_service.package import require, save, sha

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
DOCUMENT = "docs/MODEL.md"


def requests(bias_token):
    base = dict(model="qwen3.8-flash-next", enable_thinking=False,
        temperature=0, max_tokens=64, stream=False,
        messages=[dict(role="user", content="Repeat exactly this text and nothing else, without quotes: Alpha beta gamma delta.")])
    exact = dict(base, prediction=dict(type="content", content="Alpha beta gamma delta."))
    mismatch = dict(base, prediction=dict(type="content", content="wrong wrong wrong"),
        messages=[dict(role="user", content="Compute 17 + 25. Reply only with the decimal number.")])
    strict = dict(base, prediction=dict(type="content", content=[dict(type="text", text='{"answer":'), dict(type="text", text='"42"}')]),
        response_format=dict(type="json_schema", json_schema=dict(name="sum", strict=True,
            schema=dict(type="object", properties=dict(answer=dict(type="string", enum=["42"])),
                required=["answer"], additionalProperties=False))),
        messages=mismatch["messages"], stream=True, stream_options=dict(include_usage=True))
    bias = dict(mismatch, temperature=0.8, seed=29, max_tokens=8,
        logit_bias={str(bias_token):100}, prediction=dict(type="content", content="wrong"))
    cancel = dict(base, temperature=0.8, seed=23, max_tokens=1024,
        prediction=dict(type="content", content="The history of mathematics is a long and interesting story."),
        request_id="prediction-cancel", cancel_token="c"*64, stream=True,
        stream_options=dict(include_usage=True), messages=[dict(role="user", content=
            "Write a long essay of at least 1000 words about the history of mathematics.")])
    return dict(exact=exact, exact_sse=dict(exact, stream=True, stream_options=dict(include_usage=True)),
        mismatch=mismatch, strict=strict, bias=bias, cancel=cancel,
        greedy=dict(base, messages=mismatch["messages"]))


def prediction_usage(value, *, accepted=None, rejected=None):
    details = value.usage["completion_tokens_details"]
    a, r = details["accepted_prediction_tokens"], details["rejected_prediction_tokens"]
    require(type(a) is int and type(r) is int and min(a,r)>=0 and
        a+r<=value.usage["completion_tokens"], "prediction token accounting")
    if accepted is not None: require(a==accepted, "unexpected accepted prediction count")
    if rejected is not None: require(r==rejected, "unexpected rejected prediction count")
    return a,r


def prepare(args):
    out = args.output.resolve()
    require(out.is_relative_to(ROOT / ".q4t-work"), "output must be in workspace artifacts")
    require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
            "commit reviewed source before freezing")
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    binary = args.binary.resolve(strict=True)
    model = Path.home() / "models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream"
    names = subprocess.check_output(["git", "ls-files", "src", "include", "cmake",
        "CMakeLists.txt", "tools/serve_prediction", "tools/serve_sampling", "tools/serve_chat_input", "tools/serve_grammar_reap",
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
        planned=dict(starts=1, chats=7), automatic_retries=False,
        acceptance="Target-verified prediction, rollback, usage, constrained sampling, cancellation and cleanup; not performance or full model quality")
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
        if body.get("prediction") is not None:
            a,r=prediction_usage(value)
            require(a+r==json.loads(raw)["output"]["prediction_tokens"], "prediction count differs from actual tokenization")
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
        self.fault_thread = threading.Thread(target=invoke, name="prediction-cancel-business")
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
        require(caps["chat"]["prediction"]["profile"]=="q4t-prediction-v1", "prediction profile")
        self.client.protocol_sha256=caps["protocol_source_sha256"]
        first=self.sdk_request("exact-json", self.plan["requests"]["exact"])
        second=self.sdk_request("exact-sse", self.plan["requests"]["exact_sse"])
        require(first.message==second.message and first.usage==second.usage, "prediction transport differs")
        require(first.message["content"]=="Alpha beta gamma delta." and first.finish_reason=="stop", "exact prediction quality")
        a,r=prediction_usage(first,rejected=0)
        require(a>0, "prediction was never consumed")
        wrong=self.sdk_request("mismatch", self.plan["requests"]["mismatch"])
        require(wrong.finish_reason=="stop" and wrong.message["content"].strip()=="42", "prediction forced incorrect text or rollback damaged state")
        require(prediction_usage(wrong,accepted=0)[1]>0, "unused prediction was not charged")
        strict=self.sdk_request("strict", self.plan["requests"]["strict"])
        require(strict.finish_reason=="stop" and json.loads(strict.message["content"])=={"answer":"42"}, "prediction bypassed grammar")
        require(prediction_usage(strict)[0]>0, "strict prediction not consumed")
        bias=self.sdk_request("bias", self.plan["requests"]["bias"])
        require(bias.finish_reason=="length" and bias.message["content"] and
                set(bias.message["content"])=={"7"}, "target sampler/bias did not win")
        require(prediction_usage(bias,accepted=0)[1]>0 and bias.usage["completion_tokens"]==8,
                "rejected prediction escaped output limit")
        self.cancel_multi()
        last=self.sdk_request("greedy-after-cancel",self.plan["requests"]["greedy"])
        require(last.finish_reason=="stop" and last.message["content"].strip()=="42", "normal MTP continuation")
        metrics=self.drained("final")
        require(metrics["q4t_requests_total"]==7 and metrics["q4t_requests_success_total"]==6 and
                metrics["q4t_requests_aborted_total"]==1 and metrics["q4t_requests_error_total"]==0,
                "prediction request accounting")
        log=(self.out/"model.log").read_text()
        import re
        records=re.findall(r'\[prediction\].*?verify_pairs=(\d+) pair_matches=(\d+)',log)
        require(records and any(int(m)>0 for p,m in records) and
                any(int(p)>int(m) for p,m in records), "no actual target verify/partial rollback evidence")

    def run(self):
        result = dict(status="FAILED")
        try:
            self.start()
            self.business()
            result["status"] = "PASS_PREDICTION_BUSINESS"
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
