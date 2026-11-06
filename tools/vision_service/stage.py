"""Frozen visual service acceptance, one complete phase per GPU lease.

No retries, persistent service or automatic installation. Raw requests,
responses, failures, process identities and final observations are retained.
"""
import argparse
import copy
from dataclasses import asdict
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tools/evalscope"), str(ROOT / "tools/examples")]
from tools.examples import chat_client as client
from tools.examples.vision_input import image_part, sample_video
from tools.mtp_suffix import run_stage as guard_tools
from tools.serve_cuda_quota_integration.verify import Children
from tools.serve_backpressure.verify import metrics as parse_metrics
from tools.examples.project_records import ProjectRecords, TOOLS, VALIDATORS
from jsonschema import Draft202012Validator
import nvml_process_query

BASE = ROOT / ".q4t-work/vision-production-20261106"
MODEL = Path("/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream")
PORT = 18146
LIMITS = dict(starts=1, generations=30, controls=100, native=2, seconds=1800)


def sha(path):
    with Path(path).open("rb") as f: return hashlib.file_digest(f, "sha256").hexdigest()


def save(path, value):
    with Path(path).open("x") as f: json.dump(value, f, ensure_ascii=False, indent=2)


def require(value, message):
    if not value: raise RuntimeError(message)


def wait_owned(children, errors):
    """Record bad exits without skipping fresh guard/reap/GPU barriers."""
    for process,folder in children.children:
        if not (folder/"waitable.json").exists():
            try:children.wait(process,folder,30)
            except BaseException:
                # A saved waitable receipt means it exited. A timeout or
                # unknown state must still abort the normal reap path.
                if not (folder/"waitable.json").exists():raise
                errors.append(traceback.format_exc())


def request(text, parts=(), stream=False, **extra):
    body = client.make_request([dict(role="user", content=[*parts,
        dict(type="text", text=text)])], [], stream=stream, max_tokens=256,
        timeout_ms=120000)
    body.update(extra)
    return body


def structured(properties):
    return dict(type="json_schema", json_schema=dict(name="visual_observation", strict=True,
        schema=dict(type="object", properties=properties, required=list(properties),
                    additionalProperties=False)))


def cases():
    fixtures = BASE / "fixtures"
    natural = image_part(fixtures / "natural-scene.png")
    document = image_part(fixtures / "document.png")
    chart = image_part(fixtures / "chart.png")
    motion = sample_video(fixtures / "motion.mov", frames=8)
    reverse = dict(motion, video_frames=list(reversed(motion["video_frames"])))
    odd = dict(type="video_frames", video_frames=motion["video_frames"][:3],
               video_timestamps=motion["video_timestamps"][:3])
    table_format = structured({k:dict(type="integer") for k in ("Vision","Text","Tools")})
    direction = structured(dict(direction=dict(type="string", enum=["left_to_right",
        "right_to_left", "stationary"])))
    result = []
    def add(label, body, expected, check="exact"):
        result.append(dict(label=label, request=body, expected=expected, check=check))
    add("natural-json", request("What color is the mug? Reply with one English word only.",
        [natural]), "red")
    add("natural-sse", request("What color is the mug? Reply with one English word only.",
        [natural], stream=True), "red")
    add("natural-spatial", request("What object is on the far right? Reply with its English name only.",
        [natural]), "banana")
    for name, part in (("png", document), ("jpeg", image_part(fixtures/"document.jpg"))):
        add("document-"+name, request("Read the Passed column. Return the integer counts for Vision, Text, Tools as JSON.",
            [part], response_format=table_format), dict(Vision=12, Text=24, Tools=8), "json")
    add("chart", request("Which bar is tallest? Reply only with A, B, or C.", [chart], stream=True), "b")
    add("multiple-images", request("The first image is a table and the second a chart. Return the Vision count from the table and the value of bar B in the chart as JSON keys vision and b.",
        [document,chart], response_format=structured(dict(vision=dict(type="integer"), b=dict(type="integer")))),
        dict(vision=12,b=9), "json")
    for name, part, answer in (("forward",motion,"left_to_right"), ("reverse",reverse,"right_to_left"),
                                ("odd",odd,"left_to_right")):
        add("motion-"+name, request("Which direction does the red circle move over the video? Return JSON direction.",
            [part], stream=True, response_format=direction), dict(direction=answer), "json")
    add("image-json-object", request("Return a JSON object with the mug color under key color.",
        [natural], response_format=dict(type="json_object")), dict(color="red"), "json")
    add("image-tool-none", request("What color is the mug? Reply with one English word only.",
        [natural], tool_choice="none"), "red")
    for i in range(2):
        add(f"text-protection-{i}", request("Compute 17 + 25. Reply with the number only.",
            stream=bool(i)), "42")
    return result


def prepare(phase="phase-01", reuse=None, cpu_video=False, tower_repair=False):
    folder = BASE / phase
    folder.mkdir(exist_ok=False)
    rows = json.loads((BASE/reuse/"requests.json").read_text()) if reuse else cases()
    save(folder / "requests.json", rows)
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    paths = [ROOT/name for name in tracked if name.startswith(("src/", "include/", "cmake/",
        "tools/vision_service/", "tools/examples/", "tools/evalscope/")) or name == "CMakeLists.txt"]
    paths += [BASE/"engine/q4t", BASE/"engine/q4t_vision_tower_contract",
              ROOT/"build/q4t", ROOT/"build/q4t.release.json", folder/"requests.json",
              ROOT/"tools/vision_ref.json", MODEL/"config.json", MODEL/"preprocessor_config.json",
              MODEL/"video_preprocessor_config.json", MODEL/"tokenizer.json"]
    paths += list((BASE/"fixtures").iterdir())
    paths += list((ROOT/"docs").glob("serve-*.json"))
    reused=None
    if reuse:
        prior=BASE/reuse
        result=json.loads((prior/"execution/result.json").read_text())
        exit=json.loads((prior/"lease/exit.json").read_text())
        require(result["counts"]==dict(starts=0,generations=0,controls=0,native=2) and
            not result["cleanup_errors"] and not result["monitor_failures"] and
            exit["status"]=="COMMAND_FAILED","unexpected prior phase")
        require(json.loads((prior/"execution/tower/waitable.json").read_text())["returncode"]==0,
                "numerical prerequisite not passed")
        old=json.loads((prior/"plan.json").read_text())
        if not tower_repair:
            for name in (str(BASE/"engine/q4t_vision_tower_contract"),str(ROOT/"tools/vision_ref.json")):
                require(sha(name)==old["files"][name],"numerical identity changed")
        reused=dict(prior= str(prior), video=str(prior/"execution/hardware/motion-h264.mp4"),
                    numerical_status="PASS_UNCHANGED_FULL_TOWER", prior_phase_status="FAILED_CODEC_METADATA")
        paths += [prior/"execution/tower/waitable.json",prior/"execution/tower/stdout.log",
                  prior/"execution/result.json",prior/"lease/exit.json",Path(reused["video"])]
    if cpu_video:
        require(reused is not None,"CPU video continuation needs preserved encoding")
        save(folder/"encoded-video-part.json",sample_video(reused["video"],frames=8,backend="cpu"))
        paths.append(folder/"encoded-video-part.json")
        prior_failed=BASE/"phase-02/execution/result.json"
        result=json.loads(prior_failed.read_text())
        require(result["counts"]==dict(starts=0,generations=0,controls=0,native=1) and
            not result["cleanup_errors"] and not result["monitor_failures"],"second phase not closed")
        paths.append(prior_failed)
    limits=dict(LIMITS,native=0 if cpu_video else 1) if reuse else LIMITS
    if tower_repair:
        require(cpu_video and reuse=="phase-01","repair requires preserved CPU video")
        limits=dict(LIMITS,native=2)
        reused["numerical_status"]="INVALIDATED_SHARED_REFERENCE_DEFECT"
        paths += list((BASE/"cpu/hf-tower-01").glob("*.json"))
    plan = dict(profile="q4t-vision-stage-v1", limits=limits, port=PORT,
        cpu_video=cpu_video,
        tower_repair=tower_repair,
        reused=reused,
        binary=str(BASE/"engine/q4t"), model=str(MODEL), source_commit=subprocess.check_output(
            ["git","rev-parse","HEAD"], cwd=ROOT,text=True).strip(),
        files={str(p):sha(p) for p in paths if p.is_file()},
        matrix=dict(ordinary=len(rows), real_tool_requests=2, negatives=5,
                    cancellation=1, recovery=1, hardware_video=1),
        text_baseline_and_governance="separate frozen phase within cumulative budget",
        performance_acceptance=False, long_stability=False)
    save(folder/"plan.json", plan)
    return plan["matrix"]


class Run:
    success_status = "PASS_BOUNDED_VISUAL_BUSINESS"
    limits = LIMITS
    def __init__(self, folder):
        self.folder = folder
        self.plan = json.loads((folder/"plan.json").read_text())
        expected=dict(self.limits,native=0 if self.plan.get("cpu_video") else 1) if self.plan.get("reused") else self.limits
        if self.plan.get("tower_repair"):expected=dict(self.limits,native=2)
        require(self.plan["limits"]==expected and self.plan["port"]==PORT,
                "frozen phase limits/port changed")
        self.output = folder/"execution"
        self.output.mkdir()
        self.counts = dict(starts=0,generations=0,controls=0,native=0)
        self.started = time.monotonic()
        self.children = Children(folder/"lease")
        self.model = None
        self.observations = []
        self.lock = threading.Lock()
        self.failures, self.heartbeat = [], {"last_sample":time.monotonic()}
        self.stop_monitor = threading.Event()
        self.monitor = threading.Thread(target=guard_tools.sample_memory, args=(
            self.output/"resources.jsonl", self.stop_monitor, self.failures,self.heartbeat), daemon=True)

    def budget(self, kind):
        with self.lock:
            require(time.monotonic()-self.started < self.plan["limits"]["seconds"]-45, "phase deadline")
            require(self.counts[kind] < self.plan["limits"][kind], "phase budget: "+kind)
            self.counts[kind] += 1
            save(self.output/f"attempt-{kind}-{self.counts[kind]:03d}.json", dict(self.counts))
        guard_tools.require_monitor(self.monitor,self.failures,self.heartbeat,10)

    def http(self, label, path, body=None, expected=200):
        self.budget("generations" if path == "/v1/chat/completions" else "controls")
        remaining = self.plan["limits"]["seconds"] - (time.monotonic()-self.started) - 45
        connection = http.client.HTTPConnection("127.0.0.1", self.plan["port"], timeout=min(130,remaining))
        raw_body = json.dumps(body,ensure_ascii=False).encode() if body is not None else None
        save(self.output/(label+"-request.json"), dict(path=path,body=body))
        begin = time.monotonic()
        try:
            connection.request("POST" if body is not None else "GET", path, raw_body,
                {"Content-Type":"application/json"} if body is not None else {})
            response = connection.getresponse()
            raw = response.read(32*1024*1024+1)
            (self.output/(label+"-wire.bin")).write_bytes(raw)
            save(self.output/(label+"-transport.json"), dict(status=response.status,
                headers=dict(response.getheaders()), seconds=time.monotonic()-begin))
            require(len(raw) <= 32*1024*1024, "response limit")
            require(response.status == expected, f"{label}: HTTP {response.status} != {expected}")
            return raw
        finally: connection.close()

    def complete(self, label, body):
        raw = self.http(label,"/v1/chat/completions",body)
        value = (client.read_sse if body.get("stream") else client.read_json)(io.BytesIO(raw))
        require(value.id == body["request_id"] and value.model == body["model"], "response identity")
        save(self.output/(label+"-completion.json"), asdict(value))
        return value

    def observe(self, label, passed, **details):
        row = dict(label=label,passed=bool(passed),**details)
        self.observations.append(row)
        save(self.output/(label+"-observation.json"),row)

    def native(self, label, command, timeout):
        self.budget("native")
        folder = self.output/label
        process = self.children.launch(folder,command)
        self.children.wait(process,folder,timeout)

    def idle(self,label):
        names=("q4t_active_chats","q4t_queued_requests","q4t_structured_active",
               "q4t_structured_preparing","q4t_structured_workers_active")
        deadline=time.monotonic()+5
        while True:
            raw = self.http(label+"-"+str(self.counts["controls"]),"/metrics")
            values = parse_metrics(raw.decode())
            if all(values[n]==0 for n in names):break
            require(time.monotonic()<deadline,"active request retained")
            time.sleep(.1)
        require(values["q4t_gpu_healthy"]==1 and values["q4t_seq_slots_free"]==1,"GPU/slot unhealthy")
        require(values["q4t_structured_workers_started_total"] ==
                values["q4t_structured_workers_reaped_total"],"grammar child retained")

    def launch(self):
        self.budget("starts")
        argv=[self.plan["binary"],"serve","--model-dir",self.plan["model"],"--host","127.0.0.1",
            "--port",str(self.plan["port"]),"--allow-media","--no-mtp","--max-seq","1",
            "--max-prefill","8192","--max-len","16384","--max-inflight-requests","8",
            "--max-queued-requests","4"]
        env={k:v for k,v in os.environ.items() if not k.startswith("Q4T_") and k not in
             ("LD_PRELOAD","LD_LIBRARY_PATH")}
        self.model = self.children.launch(self.output/"model",argv,env)
        deadline=time.monotonic()+180
        while "serving on port" not in (self.output/"model/stderr.log").read_text():
            require(self.children.running(self.model) and time.monotonic()<deadline,"startup failed")
            time.sleep(.1)
        for label,path in (("caps","/v1/capabilities"),("config","/v1/config")):
            value=json.loads(self.http(label,path))
            if label=="caps":require(value["runtime"]["media_enabled"] and
                not value["runtime"]["mtp_enabled"],"media profile not active")

    def business(self):
        rows=json.loads((self.folder/"requests.json").read_text())
        for row in rows:
            label,body=row["label"],row["request"]
            pre=json.loads(self.http(label+"-preflight","/v1/chat/preflight",dict(request=body)))
            Draft202012Validator(json.loads((ROOT/"docs/serve-preflight-v1.schema.json").read_text())).validate(pre)
            value=self.complete(label,body)
            require(value.usage["prompt_tokens"]==pre["prompt_tokens"],"preflight/generation tokens disagree")
            content=value.message.get("content","")
            actual=json.loads(content) if row["check"]=="json" else content.strip().strip(".。").lower()
            self.observe(label,actual==row["expected"] and value.finish_reason=="stop",actual=actual,expected=row["expected"])
        self.tools()
        self.negatives()
        self.cancel()
        part_path=self.folder/"encoded-video-part.json" if self.plan.get("cpu_video") else self.output/"hardware/part.json"
        part=json.loads(part_path.read_text())
        body=request("Which direction does the red circle move? Return JSON direction.",[part],
            response_format=structured(dict(direction=dict(type="string",enum=["left_to_right","right_to_left","stationary"]))))
        result=self.complete("encoded-video",body)
        self.observe("encoded-video",json.loads(result.message["content"])==dict(direction="left_to_right"),
            decode_backend="cpu" if self.plan.get("cpu_video") else "jetson")
        self.idle("final-idle")

    def tools(self):
        tools=copy.deepcopy(TOOLS[:1]);tools[0]["function"]["strict"]=True
        body=request("Read the first 16 lines of docs/STATUS.md using read_project_record. Then report the Vision Passed count visible in this image and the current project stage. Cite the actual document.",
            [image_part(BASE/"fixtures/document.png")],stream=True,tools=tools,
            tool_choice=dict(type="function",function=dict(name="read_project_record")),parallel_tool_calls=False)
        value=self.complete("visual-tool-call",body)
        calls=client.prepare_tool_calls(value,VALIDATORS)
        require(len(calls)==1 and calls[0].arguments["path"]=="docs/STATUS.md","unexpected real tool call")
        tool_result=ProjectRecords(ROOT).execute(calls[0]);require(tool_result["ok"],"real project source unavailable")
        save(self.output/"real-tool-result.json",tool_result)
        history=client.append_tool_results(body["messages"],value,{calls[0].call_id:json.dumps(tool_result,ensure_ascii=False)})
        following=client.make_request(history,tools,stream=False,max_tokens=256,timeout_ms=120000)
        following["tool_choice"]="none"
        result=self.complete("visual-tool-final",following)
        content=result.message.get("content","")
        self.observe("visual-tool-loop",result.finish_reason=="stop" and "12" in content and
            "docs/STATUS.md" in content,content=content,tool_source=tool_result)

    def negatives(self):
        part=image_part(BASE/"fixtures/document.png")
        variants=[dict(type="image_url",image_url="https://example.invalid/image.png"),
            dict(type="image_url",image_url="data:image/png;base64,AAAA"),
            dict(type="video_frames",video_frames=[part["image_url"]]*2,video_timestamps=[1,0]),
            dict(type="video_frames",video_frames=[part["image_url"]]),
            dict(type="image_url",image_url=dict(part["image_url"],detail="high"))]
        for i,variant in enumerate(variants):
            error=json.loads(self.http(f"invalid-{i}","/v1/chat/completions",request("Describe.",[variant]),expected=400))
            self.observe(f"invalid-{i}",bool(error.get("error",{}).get("code")),error=error)

    def cancel(self):
        body=request("List every visible detail at great length.",[image_part(BASE/"fixtures/natural-scene.png")],
            stream=True,max_tokens=2048)
        # Begin a real visual request, cancel after the request is registered.
        error=[]
        def run():
            try: self.http("cancelled-visual","/v1/chat/completions",body)
            except BaseException as exc:error.append(repr(exc))
        thread=threading.Thread(target=run,daemon=True);thread.start();time.sleep(.5)
        raw=self.http("cancel-control","/v1/requests/cancel",
            {key:body[key] for key in ("request_id","cancel_token")},expected=202)
        thread.join(140);require(not thread.is_alive(),"cancel did not terminate")
        wire=(self.output/"cancelled-visual-wire.bin").read_bytes()
        transport=json.loads((self.output/"cancelled-visual-transport.json").read_text())
        if transport["status"]==200:
            cancelled=False
            try:client.read_sse(io.BytesIO(wire))
            except client.RemoteError as exc:cancelled=exc.code=="explicit_cancel" and b"[DONE]" in wire
        else:
            cancelled=transport["status"]==409 and json.loads(wire)["error"]["code"]=="explicit_cancel"
        self.observe("cancelled-visual",cancelled,request_error=error)
        self.idle("cancel-idle")
        result=self.complete("recovery",request("What color is the mug? One English word only.",
            [image_part(BASE/"fixtures/natural-scene.png")]))
        self.observe("recovery",result.message["content"].strip().lower().strip(".")=="red")

    def run(self):
        guard_tools.require_guard()
        guard_tools.verify_files(dict(self.plan,model_file_metadata={}))
        self.monitor.start()
        verdict="FAILED"
        failure=None
        cleanup=[]
        def expired(*_): raise TimeoutError("frozen phase deadline; cleanup only")
        signal.signal(signal.SIGALRM,expired)
        signal.setitimer(signal.ITIMER_REAL,self.plan["limits"]["seconds"]-45)
        try:
            reuse=self.plan.get("reused")
            if self.plan.get("tower_repair"):
                for label in ("small","rectangular"):
                    self.native("hf-"+label,[str(BASE/"engine/q4t_vision_tower_contract"),
                        str(BASE/"cpu/hf-tower-01"/(label+".json")),
                        str(self.output/("hf-"+label+"-output"))],90)
            elif not reuse:
                self.native("tower",[str(BASE/"engine/q4t_vision_tower_contract"),str(ROOT/"tools/vision_ref.json"),
                    str(self.output/"tower-output")],120)
            codec=[sys.executable,"-B",str(ROOT/"tools/vision_service/codec_acceptance.py"),
                "--fixtures",str(BASE/"fixtures"),"--output",str(self.output/"hardware")]
            if reuse:codec += ["--existing-video",reuse["video"]]
            if not self.plan.get("cpu_video"):self.native("codec",codec,90)
            self.launch();self.business()
            require(all(r["passed"] for r in self.observations),"business/quality failures; inspect observations")
            verdict=self.success_status
        except BaseException:
            failure=traceback.format_exc()
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            try:
                if self.model and self.children.running(self.model):
                    self.children.signal(self.model,signal.SIGTERM)
                wait_owned(self.children,cleanup)
                # Fresh guard sample after all owned processes are waitable.
                self.children.reap(self.output)
                # Two completed cycles ensure a full cycle began after shutdown.
                for _ in range(2):
                    boundary=self.heartbeat["last_sample"]
                    while self.heartbeat["last_sample"] <= boundary:
                        guard_tools.require_monitor(self.monitor,self.failures,self.heartbeat,10)
                        time.sleep(.05)
                gpu=nvml_process_query.query(timeout=5)
                save(self.output/"gpu-final.json",gpu)
                require(gpu["status"]=="OK" and not gpu["pids"],"ENV_ABORT: final GPU query")
            except BaseException:
                cleanup.append(traceback.format_exc())
            finally:
                self.stop_monitor.set();self.monitor.join(5)
                self.children.cleanup()
            if self.monitor.is_alive() or self.failures:cleanup.append("ENV_ABORT: final monitor")
            try: guard_tools.verify_files(dict(self.plan,model_file_metadata={}))
            except BaseException: cleanup.append(traceback.format_exc())
            if failure or cleanup:verdict="FAILED"
            save(self.output/"result.json",dict(status=verdict,counts=self.counts,
                seconds=time.monotonic()-self.started,observations=self.observations,
                monitor_failures=self.failures,default_unchanged=True,
                failure=failure,cleanup_errors=cleanup))
        require(verdict==self.success_status,failure or str(cleanup))
        return verdict


if __name__ == "__main__":
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("prepare","run"))
    parser.add_argument("--phase",default="phase-01",choices=("phase-01","phase-02","phase-03","phase-04"))
    parser.add_argument("--reuse",choices=("phase-01",))
    parser.add_argument("--cpu-video",action="store_true")
    parser.add_argument("--tower-repair",action="store_true")
    args=parser.parse_args()
    print(json.dumps(prepare(args.phase,args.reuse,args.cpu_video,args.tower_repair) if args.action=="prepare" else Run(BASE/args.phase).run()))
