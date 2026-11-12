"""A04 legacy functions and historical migration business acceptance under one GPU lease."""
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

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
DOCUMENT = "docs/MODEL.md"


def requests():
    base=dict(model="qwen3.8-flash-next",enable_thinking=False,temperature=0,
        max_completion_tokens=256,stream=True,stream_options=dict(include_usage=True))
    function=copy.deepcopy(TOOLS[0]["function"])
    function.pop("strict",None)
    function["parameters"]["properties"]["path"]=dict(type="string",enum=[DOCUMENT])
    tool=dict(base,functions=[function],function_call=dict(name="read_project_record"),
        messages=[dict(role="user",content="Read the first 16 lines of docs/MODEL.md with read_project_record. Then return the exact title and citation from the tool.")])
    automatic=copy.deepcopy(tool);automatic["stream"]=False;automatic.pop("stream_options")
    automatic["function_call"]="auto"
    note=dict(base,functions=[dict(name="write_note",description="Propose the complete note in the text argument.",
        parameters=dict(type="object",properties=dict(text=dict(type="string")),required=["text"],additionalProperties=False))],
        function_call=dict(name="write_note"),messages=[dict(role="user",content="Call write_note with a detailed essay of at least 1500 words about the history of mathematics. Put the complete essay in text, not a summary.")])
    return dict(tool=tool,automatic=automatic,
        truncated=dict(note,max_completion_tokens=64),
        cancel=dict(note,max_completion_tokens=1024,request_id="legacy-tools-cancel",cancel_token="c"*64),
        greedy=dict(base,messages=[dict(role="user",content="Compute 17 + 25. Reply only with the decimal number.")]))


def frames(raw):
    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b"data: {")]


def require_fragments(raw):
    parts=[];name=None;finished=False
    for event in frames(raw):
        for choice in event.get("choices",[]):
            if choice.get("finish_reason") is not None:
                require(choice["finish_reason"]=="function_call","wrong legacy finish")
                finished=True
            delta=choice.get("delta",{})
            require("tool_calls" not in delta,"modern call leaked onto legacy stream")
            function=delta.get("function_call")
            if function is None:continue
            require(not finished,"fragment after terminal")
            require(set(function)<={"name","arguments"},"nonlegacy function field")
            if name is None:
                name=function.get("name");require(name,"missing function name")
            else:require("name" not in function,"name repeated")
            if "arguments" in function:parts.append(function["arguments"])
    require(finished and len(parts)>=3 and parts[0]=="{" and parts[-1]=="}","not incremental legacy arguments")
    require(isinstance(json.loads("".join(parts)),dict),"invalid assembled arguments")
    return parts


def prepare(args):
    out = args.output.resolve()
    require(out.is_relative_to(ROOT / ".q4t-work"), "output must be in workspace artifacts")
    require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
            "commit reviewed source before freezing")
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    binary = args.binary.resolve(strict=True)
    model = Path.home() / "models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream"
    names = subprocess.check_output(["git", "ls-files", "src", "include", "cmake",
        "CMakeLists.txt", "tools/serve_tool_stream", "tools/serve_legacy_tools", "tools/serve_sampling", "tools/serve_chat_input", "tools/serve_grammar_reap",
        "tools/serve_protocol", "tools/examples", "tools/evalscope", "tools/serve_recovery",
        "tools/serve_service", "tools/serve_gateway", "tools/release", DOCUMENT,"docs/ARCHITECTURE.md"],
        cwd=ROOT, text=True).splitlines()
    protected = [ROOT / name for name in ("build/q4t", "build/q4t.release.json",
        "build/service.release.json", "build/service/manifest.json")]
    plan = dict(version=1, limits=LIMITS, output=str(out), binary=str(binary),
        binary_sha256=sha(binary), model=str(model), model_assets=assets(model),
        port=args.port, operations_port=args.port+1,
        requests=requests(),
        sources={name: sha(ROOT/name) for name in names},
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        protected={str(p): sha(p) for p in protected}, sdk=sdk_identity(),
        planned=dict(starts=1, chats=7), automatic_retries=False,
        acceptance="Legacy functions JSON/SSE, actual role=function result closure, modern migration, truncation/cancellation and cleanup; not performance or full model quality")
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

    def cancel_tool(self):
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
                                    if choice.get("delta", {}).get("function_call", {}).get("arguments"):
                                        visible.set()
                    self.client.complete(body, sink=sink)
                observed["unexpected_success"] = True
            except JsSdkError as error:
                observed["error"] = error.details
            except BaseException:
                observed["unexpected_error"] = traceback.format_exc()
        self.fault_thread = threading.Thread(target=invoke, name="legacy-tools-cancel-business")
        self.fault_thread.start()
        require(visible.wait(60), "no incremental arguments before cancellation")
        self.budget("controls")
        status, raw = self.http("/v1/requests/cancel", {key:body[key] for key in
            ("request_id", "cancel_token")}, label="cancel-control")
        require(status == 202, "explicit cancellation rejected")
        self.fault_thread.join(30)
        require(not self.fault_thread.is_alive(), "cancelled request did not finish")
        save(self.out/"cancel.result.json", observed)
        require(observed.get("error", {}).get("code") == "explicit_cancel" and
                not observed.get("unexpected_success") and not observed.get("unexpected_error"),
                "cancelled tool was not a final error")
        raw = wire.read_bytes()
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b"data: {")]
        require(raw.count(b"data: [DONE]") == 1 and events[-1]["error"]["code"] == "explicit_cancel",
                "cancel terminal incomplete")
        require(not any(e.get("usage") for e in events) and not any(c.get("finish_reason")
            for e in events for c in e.get("choices", [])), "cancelled request claimed success")
        self.same_model()
        self.drained("cancel-drain")

    def business(self):
        caps=json.loads((self.out/"capabilities.json").read_text())
        require(caps["chat"]["stream_tool_arguments"]=="incremental" and
            caps["chat"]["legacy_function_call"] and caps["chat"]["legacy_tools"]["parallel"] is False,"tool capability missing")
        self.client.protocol_sha256=caps["protocol_source_sha256"]
        body=self.plan["requests"]["tool"]
        answer=self.sdk_request("tool",body)
        require_fragments((self.out/"tool.wire").read_bytes())
        calls=prepare_tool_calls(answer,VALIDATORS)
        require(len(calls)==1 and calls[0].arguments==dict(path=DOCUMENT,start_line=1,line_count=16),"read arguments differ")
        result=ProjectRecords(ROOT).execute(calls[0])
        require(result["ok"] and result["source"]["sha256"]==self.plan["sources"][DOCUMENT],"real document identity")
        save(self.out/"tool-execution.json",dict(executions=1,result=result))
        follow=copy.deepcopy(body);follow["function_call"]="none"
        follow["messages"]=append_tool_results(body["messages"],answer,{calls[0].call_id:json.dumps(result,ensure_ascii=False)})
        follow["messages"].append(dict(role="developer",content="Return the first line title and exact citation supplied by the tool. No more tool calls."))
        final=self.sdk_request("tool-final",follow)
        require(final.finish_reason=="stop" and not final.message.get("tool_calls") and not final.message.get("function_call") and
            "Qwen3.8-Flash-Next" in final.message["content"] and result["source"]["citation"] in final.message["content"],"real document closure")
        automatic=self.sdk_request("auto-json",self.plan["requests"]["automatic"])
        plans=prepare_tool_calls(automatic,VALIDATORS)
        require(len(plans)==1 and plans[0].arguments==calls[0].arguments,"legacy auto/JSON selection")
        migration=copy.deepcopy(follow)
        migration.pop("functions");migration.pop("function_call")
        migration["tools"]=[dict(type="function",function=copy.deepcopy(body["functions"][0]))]
        migration["tool_choice"]=dict(type="function",function=dict(name="read_project_record"))
        migration["parallel_tool_calls"]=False
        migration["messages"].pop()
        migration["messages"].append(dict(role="user",content="Read the same first 16 lines of docs/MODEL.md again using read_project_record."))
        modern=self.sdk_request("modern-history-migration",migration)
        require(modern.finish_reason=="tool_calls" and not modern.message.get("function_call"),"legacy history changed current output dialect")
        plans=prepare_tool_calls(modern,VALIDATORS)
        require(len(plans)==1 and plans[0].arguments==calls[0].arguments,"modern history argument mismatch")
        migrated=ProjectRecords(ROOT).execute(plans[0])
        require(migrated["ok"] and migrated["source"]["sha256"]==result["source"]["sha256"],"migration document identity")
        save(self.out/"migration-execution.json",dict(executions=1,result=migrated))
        self.truncated_tool()
        self.cancel_tool()
        last=self.sdk_request("greedy-after-cancel",self.plan["requests"]["greedy"])
        require(last.finish_reason=="stop" and last.message["content"].strip()=="42","normal MTP continuation")
        metrics=self.drained("final")
        require(metrics["q4t_requests_total"]==7 and metrics["q4t_requests_success_total"]==5 and
            metrics["q4t_requests_aborted_total"]==1 and metrics["q4t_requests_error_total"]==1,"terminal accounting")

    def truncated_tool(self):
        body=self.plan["requests"]["truncated"];self.budget("chats")
        save(self.out/"truncated.request.json",body)
        with (self.out/"truncated.wire").open("xb") as wire:
            try:
                self.client.complete(body,sink=wire.write)
            except JsSdkError as error:
                save(self.out/"truncated.result.json",dict(error=error.details,tool_executions=0))
                require(error.details.get("code")=="structured_output_incomplete","unexpected truncation error")
            else:raise RuntimeError("truncated tool claimed success")
        raw=(self.out/"truncated.wire").read_bytes();events=frames(raw)
        require(any(c.get("delta",{}).get("function_call") for e in events for c in e.get("choices",[])),"no provisional tool before length limit")
        require(raw.count(b"data: [DONE]")==1 and not any(c.get("finish_reason") for e in events for c in e.get("choices",[])),"truncation success terminal")
        self.same_model();self.drained("truncated-drain")

    def run(self):
        result = dict(status="FAILED")
        try:
            self.start()
            self.business()
            result["status"] = "PASS_LEGACY_TOOLS_BUSINESS"
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
