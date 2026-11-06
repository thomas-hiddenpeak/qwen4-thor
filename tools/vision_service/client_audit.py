"""Independent offline raw-wire, provenance and lifecycle audit of this stage."""
import argparse
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess

from PIL import Image
from tools.vision_service import client_regression as r, closeout_audit as prior

s, c = r.s, r.c


def seal():
    plan=r.old.read(r.FOLDER/"plan.json")
    paths={Path(p) for p in plan["files"]}
    paths.update(p for directory in (r.FOLDER,r.BASE/"cpu-01",r.BASE/"cpu-02")
                 for p in directory.rglob("*") if p.is_file())
    cas=r.BASE/"originals";cas.mkdir(exist_ok=True)
    files={}
    for path in sorted(paths):
        digest=s.sha(path)
        if str(path) in plan["files"]:s.require(digest==plan["files"][str(path)],"source drift "+str(path))
        target=cas/digest
        if not target.exists():shutil.copyfile(path,target)
        s.require(s.sha(target)==digest,"CAS changed")
        files[str(path.resolve())]=digest
    s.save(r.BASE/"originals.json",dict(files=files))
    return dict(bindings=len(files))


class Evidence:
    def __init__(self):
        self.folder=r.FOLDER
        self.files=r.old.read(r.BASE/"originals.json")["files"]
        for digest in set(self.files.values()):s.require(s.sha(r.BASE/"originals"/digest)==digest,"CAS corrupt")
        self.plan=self.read("plan.json")
        for path,digest in self.plan["files"].items():s.require(self.files[path]==digest,"plan file drift")
        tree=subprocess.check_output(["git","ls-tree","-r","-z",self.plan["source_commit"]])
        blobs={}
        for item in tree.split(b"\0"):
            if item:
                header,name=item.split(b"\t",1);blobs[name.decode()]=header.split()[2].decode()
        self.git_sources=0
        for name,digest in self.plan["files"].items():
            path=Path(name)
            if not path.is_relative_to(s.ROOT):continue
            relative=str(path.relative_to(s.ROOT))
            if relative not in blobs:continue
            data=self.bytes(name)
            blob=hashlib.sha1(b"blob "+str(len(data)).encode()+b"\0"+data).hexdigest()
            s.require(blob==blobs[relative],"Git source drift: "+relative)
            self.git_sources+=1

    def bytes(self,name):
        path=Path(name)
        if not path.is_absolute():path=self.folder/path
        return (r.BASE/"originals"/self.files[str(path)]).read_bytes()

    def read(self,name):return json.loads(self.bytes(name))

    def completion(self,label):
        body=self.read("execution/"+label+"-request.json")["body"]
        transport=self.read("execution/"+label+"-transport.json")
        s.require(transport["status"]==200 and not transport["automatic_retry"],"transport failed")
        value=(c.read_sse if body.get("stream") else c.read_json)(io.BytesIO(self.bytes("execution/"+label+"-wire.bin")))
        s.require(value.id==body["request_id"] and value.model==body["model"],"wire identity mismatch")
        s.require(asdict(value)==self.read("execution/"+label+"-completion.json"),"derived completion changed")
        pre=json.loads(self.bytes("execution/"+label+"-preflight-wire.bin"))
        s.require(value.usage["prompt_tokens"]==pre["prompt_tokens"],"usage mismatch")
        return body,value


def final_truth(actual, source, record):
    expected=dict(counts=record["totals"],matches_image=True,citation=source["citation"])
    s.require(actual==expected,"tool answer differs from actual record/citation")


def audit():
    e=Evidence()
    life=prior.lifecycle(e)
    s.require(life["guard"]=="COMPLETED" and life["status"]==r.Run.success_status,"phase failed")
    counts=life["counts"]
    s.require(counts["starts"]==1 and counts["generations"]==14 and counts["native"]==0
              and 17 <= counts["controls"] <= 30,"unexpected attempt count")
    observations=[]
    for row in e.read("requests.json"):
        body,value=e.completion(row["label"])
        s.require(body==row["request"],"frozen request changed")
        actual=json.loads(value.message["content"])
        s.Draft202012Validator(body["response_format"]["json_schema"]["schema"]).validate(actual)
        s.require(value.finish_reason=="stop" and actual==row["expected"],"wrong answer: "+row["label"])
        if row["kind"]=="synthetic_video_protection":
            direction,_=prior.direction_truth(body)
            s.require(actual["direction"]==("left_to_right" if direction=="right" else "right_to_left"),"video truth differs")
        observations.append(dict(label=row["label"],language=row["language"],actual=actual,usage=value.usage,
            seconds=e.read("execution/"+row["label"]+"-transport.json")["seconds"]))
    provenance=e.read("fixtures/provenance.json")
    photo=provenance["cabin"]
    s.require(hashlib.sha256(e.bytes(photo["source"])).hexdigest()==photo["sha256"],"photo source")
    with Image.open(io.BytesIO(e.bytes(photo["source"]))) as im:
        expected=im.convert("RGB").resize((960,720),Image.Resampling.LANCZOS)
        with Image.open(io.BytesIO(e.bytes("fixtures/cabin.png"))) as actual:
            s.require(actual.size==expected.size and actual.tobytes()==expected.tobytes(),"photo preprocessing drift")
    s.require(e.bytes(provenance["earth"]["source"])==e.bytes("fixtures/earth.jpg"),"natural photo changed")
    for source,digest in provenance["records"]["inputs"].items():
        s.require(hashlib.sha256(e.bytes(source)).hexdigest()==digest,"real record render changed")
    body,tool=e.completion("record-tool-call")
    calls=c.prepare_tool_calls(tool,s.VALIDATORS)
    s.require(len(calls)==1 and asdict(calls[0])==e.read("execution/tool-execution-started.json"),"tool execution identity")
    result=e.read("execution/tool-result.json");source=result["source"]
    record=e.bytes(s.ROOT/r.old.RECORD)
    s.require(source["sha256"]==hashlib.sha256(record).hexdigest(),"tool source hash")
    s.require(source["text"]=="\n".join(f"{i}: {line}" for i,line in enumerate(record.decode().splitlines()[:12],1)),"actual tool lines")
    s.require(source["citation"]==f"[{r.old.RECORD}:L1-L12]","tool citation")
    history=c.append_tool_results(body["messages"],tool,{calls[0].call_id:json.dumps(result,ensure_ascii=False)})
    error=e.read("execution/record-final-budget-1-transport.json")
    wire=json.loads(e.bytes("execution/record-final-budget-1-wire.bin"))
    s.require(error["status"]==502 and wire["error"]["code"]=="structured_output_incomplete","missing budget failure")
    low=e.read("execution/record-final-budget-1-request.json")["body"]
    s.require(low["max_tokens"]==1,"negative budget changed")
    finals=[]
    for language in ("python","javascript"):
        final,value=e.completion("record-final-"+language)
        s.require(final["max_tokens"]==768 and value.finish_reason=="stop","final budget/terminal")
        s.require(final["messages"][1:]==history[1:]==low["messages"][1:],"tool history was rewritten or replayed")
        s.require(final["messages"]==low["messages"],"continuation prompt changed")
        actual=json.loads(value.message["content"])
        s.Draft202012Validator(e.read("final-schema.json")).validate(actual)
        final_truth(actual,source,json.loads(record))
        finals.append(dict(language=language,actual=actual,usage=value.usage))
    starts=[p for p in e.files if p.endswith("tool-execution-started.json")]
    s.require(len(starts)==1,"tool execution count")
    runtime=r.old.read(s.ROOT/"build/q4t.release.json")
    manifest=r.old.read(runtime["tested_source_manifest"])
    s.require(all(s.sha(s.ROOT/p)==h for p,h in manifest.items()),"runtime source changed")
    for p in (s.ROOT/"build/q4t",s.ROOT/"build/q4t.release.json"):
        s.require(s.sha(p)==e.plan["files"][str(p)],"default pair changed")
    return dict(status="PASS_SCOPED_CLIENT_AND_REAL_SCENE",source_commit=e.plan["source_commit"],
        phase=life,cases=observations,finals=finals,tool_executions=1,low_budget_failed_closed=True,
        original_files=len(e.files),git_sources=e.git_sources,runtime_sources_unchanged=len(manifest),
        global_media_acceptance=False,hardware_decode_expanded=False,vision_concurrency_expanded=False,
        limits="Two photographs and one actual record; no general visual accuracy or long-run claim.")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("seal","audit"))
    action=parser.parse_args().action
    value=seal() if action=="seal" else audit()
    if action=="audit":s.save(r.BASE/"audit.json",value)
    print(json.dumps(value))
