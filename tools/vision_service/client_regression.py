"""Frozen small real-scene client regression, one whole-phase GPU lease.

The two photographs have predeclared visual truth. Record graphics are real
data renders, not scans; two old synthetic videos are explicitly protection.
No hardware decode, concurrent generation, automatic retry or deployment.
"""
import argparse
import copy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import time

from PIL import Image
from tools.vision_service import stage as s, closeout as old
from tools.examples import chat_client as c
from tools.serve_protocol.js_client import JsSdkClient

BASE = s.ROOT / ".q4t-work/vision-client-20261107"
FOLDER = BASE / "phase-01"
LIMITS = dict(starts=1, generations=16, controls=30, native=0, seconds=600)
TOTAL = dict(starts=2, generations=24, controls=60, native=0, seconds=1200)
SDK = s.ROOT / ".q4t-work/serve-protocol-20261103/sdk"
CABIN = s.ROOT / "reference/qwen35-thor/tests/assets/飞机上RM01运行照片.jpg"
EARTH = s.ROOT / "reference/ds4/tests/vision-fixtures/glm53/earth.jpg"


def schema(properties):
    return dict(type="object", properties=properties, required=list(properties),
                additionalProperties=False)


def js_request(body, output_schema, name="answer"):
    env = {k:v for k,v in os.environ.items() if k not in
           ("NODE_OPTIONS", "NODE_PATH", "LD_PRELOAD")}
    result = subprocess.run(["node", str(Path(__file__).with_name("schema_request.mjs"))],
        input=json.dumps(dict(body=body, schema=output_schema, name=name)),
        capture_output=True, text=True, timeout=15, env=env)
    result.check_returncode()
    s.require(not result.stderr, "JS request diagnostics")
    return json.loads(result.stdout)


def budget(plans):
    s.require(all(type(p["limits"][k]) is int and p["limits"][k] >= 0
                  for p in plans for k in TOTAL), "invalid phase budget")
    totals = {k:sum(p["limits"][k] for p in plans) for k in TOTAL}
    s.require(all(0 <= totals[k] <= TOTAL[k] for k in TOTAL), "total stage budget")
    return totals


def prepare():
    s.require(s.sha(s.ROOT/"build/q4t") == old.DEFAULT, "default changed")
    FOLDER.mkdir()
    fixtures = FOLDER/"fixtures"
    fixtures.mkdir()
    # Deterministic test input preprocessing, full frame; original stays read-only.
    with Image.open(CABIN) as image:
        s.require(image.size == (7749, 5812), "original photo geometry changed")
        image.convert("RGB").resize((960, 720), Image.Resampling.LANCZOS).save(fixtures/"cabin.png")
    (fixtures/"earth.jpg").write_bytes(EARTH.read_bytes())
    chart = old.FOLDER/"fixtures/record-chart.png"
    table = old.FOLDER/"fixtures/record-table.png"
    for path in (chart, table): (fixtures/path.name).write_bytes(path.read_bytes())
    provenance = dict(cabin=dict(source=str(CABIN), sha256=s.sha(CABIN),
        transform="Pillow RGB full-frame Lanczos resize 960x720; no crop", output=s.sha(fixtures/"cabin.png")),
        earth=dict(source=str(EARTH), sha256=s.sha(EARTH), transform="byte copy",
            attribution="reference/ds4 fixture README: NASA Apollo 17 AS17-148-22727"),
        records=dict(source=old.RECORD, sha256=s.sha(s.ROOT/old.RECORD),
            kind="programmatic render of real committed record, not a scan",
            inputs={str(p):s.sha(p) for p in (chart,table,old.FOLDER/"fixtures/truth.json")}))
    s.save(fixtures/"provenance.json", provenance)
    cabin, earth = (s.image_part(fixtures/n) for n in ("cabin.png", "earth.jpg"))
    rows = []
    def add(label, language, question, parts, output_schema, expected, stream=False):
        body = c.make_request([dict(role="user", content=[*parts,
            dict(type="text", text=question)])], [], stream=stream, max_tokens=256)
        py = c.with_json_schema(body, output_schema)
        js = js_request(body, output_schema)
        for key in ("messages", "response_format", "max_tokens"):
            s.require(py[key] == js[key], "Python/JS semantic request differs: "+key)
        rows.append(dict(label=label, language=language, request=py if language=="python" else js,
                         expected=expected, kind="exact_json"))
    cabin_schema = schema(dict(scene=dict(type="string", enum=["aircraft_cabin","office","outdoors"],
        description="Visible setting only; aircraft_cabin means inside an airplane passenger cabin."),
        visible_windows=dict(type="integer", description="Number of visible airplane windows, including partly occluded windows."),
        device_lights_on=dict(type="boolean", description="Whether the tall rectangular device has visible illuminated lights."),
        people_visible=dict(type="boolean", description="Whether any person is visible in the image.")))
    earth_schema = schema(dict(object=dict(type="string", enum=["earth","moon","mars"]),
        clouds_visible=dict(type="boolean"), street_sign_visible=dict(type="boolean",
            description="True only if a street sign is actually visible; do not invent one.")))
    for language, stream in (("python",False),("javascript",True)):
        add("cabin-"+language,language,"Describe only the visible setting, windows, device lights and people.",
            [cabin],cabin_schema,dict(scene="aircraft_cabin",visible_windows=2,device_lights_on=True,people_visible=False),stream)
        add("earth-"+language,language,"Identify the pictured object and whether clouds and a street sign are visible.",
            [earth],earth_schema,dict(object="earth",clouds_visible=True,street_sign_visible=False),not stream)
    add("multiple-real-photos","python","Classify the first image's setting and the object in the second image.",
        [cabin,earth],schema(dict(first_scene=dict(type="string",enum=["aircraft_cabin","office","outdoors"]),
        second_object=dict(type="string",enum=["earth","moon","mars"]))),
        dict(first_scene="aircraft_cabin",second_object="earth"),True)
    add("real-record-chart","javascript","Read the phase-05b bar value and the label of the tallest bar.",
        [s.image_part(fixtures/"record-chart.png")],schema(dict(phase05b=dict(type="integer"),tallest=dict(type="string"))),
        dict(phase05b=13,tallest="phase-04"))
    original = old.read(s.BASE/"phase-04/requests.json")
    for label, language in (("motion-forward","python"),("motion-reverse","javascript")):
        source = next(r for r in original if r["label"]==label)
        parts = [p for p in source["request"]["messages"][-1]["content"] if p["type"] != "text"]
        add(label,language,"Which direction does the red circle move over the video? Return JSON direction.",
            parts,old.read(s.ROOT/"tools/examples/vision_direction.schema.json"),source["expected"],True)
        rows[-1]["kind"] = "synthetic_video_protection"
    for language, stream in (("python",False),("javascript",True)):
        body = c.make_request([dict(role="user",content="Compute 17 + 25. Return the result.")], [],
            stream=stream,max_tokens=64,json_schema=schema(dict(result=dict(type="integer"))))
        if language=="javascript":
            base=copy.deepcopy(body);base.pop("response_format")
            base["messages"][0]["content"]="Compute 17 + 25. Return the result."
            body=js_request(base,schema(dict(result=dict(type="integer"))))
        rows.append(dict(label="text-"+language,language=language,request=body,
                         expected=dict(result=42),kind="text_protection"))
    s.save(FOLDER/"requests.json",rows)
    tools = copy.deepcopy(s.TOOLS[:1]);tools[0]["function"]["strict"]=True
    question = (f"Read the first 12 lines of {old.RECORD} using read_project_record. "
        "Then report all four attempt counts, compare them to the image, and quote the exact source citation.")
    tool = c.make_request([dict(role="user",content=[s.image_part(fixtures/"record-table.png"),
        dict(type="text",text=question)])],tools,stream=True,max_tokens=256)
    tool.update(tool_choice=dict(type="function",function=dict(name="read_project_record")),parallel_tool_calls=False)
    s.save(FOLDER/"tool-request.json",tool)
    s.save(FOLDER/"final-schema.json",schema(dict(counts=schema({k:dict(type="integer") for k in
        ("starts","generations","controls","native")}),matches_image=dict(type="boolean"),
        citation=dict(type="string",description="Copy the exact citation returned by the actual tool."))))
    probe = s.BASE/"engine/q4t_preflight_probe"
    requests = [r["request"] for r in rows]+[tool]
    result=subprocess.run([str(probe),str(s.MODEL/"tokenizer.json")],input="".join(json.dumps(dict(
        allow_media=True,max_len=16384,max_prefill=8192,body=dict(request=b)))+"\n" for b in requests),
        text=True,capture_output=True,timeout=90)
    (FOLDER/"preflight.stdout").write_text(result.stdout);(FOLDER/"preflight.stderr").write_text(result.stderr)
    result.check_returncode()
    values=[json.loads(line) for line in result.stdout.splitlines()]
    s.require(len(values)==len(requests) and all("error" not in v for v in values),"CPU preflight failed")
    paths = [s.ROOT/n for n in subprocess.check_output(["git","ls-files"],text=True).splitlines()
        if n.startswith(("src/","include/","cmake/","tools/","docs/serve-")) or n=="CMakeLists.txt"]
    paths += list(FOLDER.glob("*.json"))+list(fixtures.iterdir())+[CABIN,EARTH,chart,table,
        old.FOLDER/"fixtures/truth.json",s.ROOT/old.RECORD,
        s.ROOT/"docs/VISION_CLIENT_PLAN_2026-11-07.md",probe,
        s.ROOT/"build/q4t",s.ROOT/"build/q4t.release.json"]
    paths += [s.MODEL/n for n in ("config.json","preprocessor_config.json","video_preprocessor_config.json","tokenizer.json")]
    # Explicitly bind the already-installed pinned SDK, no dependency refresh.
    paths += [p for p in (SDK/"node_modules/openai").rglob("*") if p.is_file()]
    plan=dict(profile="visual-client-regression-v1",limits=LIMITS,total=TOTAL,port=s.PORT,
        binary=str(s.ROOT/"build/q4t"),model=str(s.MODEL),cpu_video=True,tower_repair=False,
        reused=dict(video=str(s.BASE/"phase-01/execution/hardware/motion-h264.mp4")),
        source_commit=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
        files={str(p):s.sha(p) for p in paths})
    budget([old.read(p) for p in BASE.glob("*/plan.json")]+[plan])
    s.save(FOLDER/"plan.json",plan)
    return dict(fixed_requests=len(rows),tool_requests=4,limits=LIMITS)


class Run(s.Run):
    limits = LIMITS
    success_status = "PASS_SCOPED_REAL_SCENE_CLIENT_REGRESSION"

    def client_complete(self,label,body,language="python",expected_error=None):
        pre=json.loads(self.http(label+"-preflight","/v1/chat/preflight",dict(request=body)))
        self.budget("generations")
        s.save(self.output/(label+"-request.json"),dict(path="/v1/chat/completions",body=body,language=language))
        api=(c.ChatClient(f"http://127.0.0.1:{s.PORT}") if language=="python" else
             JsSdkClient(f"http://127.0.0.1:{s.PORT}",SDK,timeout=130))
        begin=time.monotonic()
        try:
            with (self.output/(label+"-wire.bin")).open("xb") as wire:
                value=api.complete(body,sink=wire.write)
            s.require(expected_error is None,"expected error was accepted")
            s.require(value.usage["prompt_tokens"]==pre["prompt_tokens"],"expanded usage mismatch")
            s.save(self.output/(label+"-completion.json"),asdict(value))
            s.save(self.output/(label+"-transport.json"),dict(status=200,seconds=time.monotonic()-begin,
                language=language,automatic_retry=False))
            if language=="javascript":s.save(self.output/(label+"-sdk.json"),api.last_result)
            return value
        except c.RemoteError as error:
            # _post rejects non-2xx before invoking sink; retain the actual error body.
            path=self.output/(label+"-wire.bin")
            if path.stat().st_size==0:
                path.write_bytes(error.body if isinstance(error.body,bytes) else
                    (error.body.encode() if isinstance(error.body,str) else json.dumps(error.body).encode()))
            s.save(self.output/(label+"-transport.json"),dict(status=error.status,code=error.code,
                seconds=time.monotonic()-begin,language=language,automatic_retry=False))
            s.require(error.code==expected_error and error.status==502,"unexpected remote failure")
            self.observe(label,True,expected_failure=expected_error,automatic_retry=False)
            return None

    def business(self):
        budget([old.read(p) for p in BASE.glob("*/plan.json")])
        for row in old.read(self.folder/"requests.json"):
            value=self.client_complete(row["label"],row["request"],row["language"])
            actual=json.loads(value.message["content"])
            s.Draft202012Validator(row["request"]["response_format"]["json_schema"]["schema"]).validate(actual)
            self.observe(row["label"],value.finish_reason=="stop" and actual==row["expected"],
                expected=row["expected"],actual=actual,usage=value.usage,kind=row["kind"])
        body=old.read(self.folder/"tool-request.json")
        first=self.client_complete("record-tool-call",body)
        calls=c.prepare_tool_calls(first,s.VALIDATORS)
        s.require(len(calls)==1 and calls[0].arguments==dict(path=old.RECORD,start_line=1,line_count=12),
                  "unexpected actual tool invocation")
        s.save(self.output/"tool-execution-started.json",asdict(calls[0]))
        result=s.ProjectRecords(s.ROOT).execute(calls[0])
        s.require(result["ok"],"actual tool failed")
        s.save(self.output/"tool-result.json",result)
        history=c.append_tool_results(body["messages"],first,{calls[0].call_id:json.dumps(result,ensure_ascii=False)})
        output_schema=old.read(self.folder/"final-schema.json")
        following=c.make_request(history,body["tools"],stream=False,max_tokens=1)
        following["tool_choice"]="none"
        low=c.with_json_schema(following,output_schema)
        self.client_complete("record-final-budget-1",low,expected_error="structured_output_incomplete")
        expected=dict(counts=old.read(s.ROOT/old.RECORD)["totals"],matches_image=True,
                      citation=result["source"]["citation"])
        # Explicit application continuation with identical real tool history.
        # Each transport has a fresh identity; no tool is replayed.
        for language,stream in (("python",True),("javascript",False)):
            following=c.make_request(history,body["tools"],stream=stream,max_tokens=768)
            following["tool_choice"]="none"
            final=(c.with_json_schema(following,output_schema) if language=="python" else
                   js_request(following,output_schema))
            value=self.client_complete("record-final-"+language,final,language)
            actual=json.loads(value.message["content"])
            s.Draft202012Validator(output_schema).validate(actual)
            self.observe("record-final-"+language,value.finish_reason=="stop" and actual==expected,
                actual=actual,expected=expected,usage=value.usage,tool_executions=1)
        self.idle("final-idle")


if __name__=="__main__":
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("prepare","run"))
    print(json.dumps(prepare() if parser.parse_args().action=="prepare" else Run(FOLDER).run()))
