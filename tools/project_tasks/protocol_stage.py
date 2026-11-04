"""One shared lease, two sequential protocols, saved-tool recovery over TLS."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True; sys.path.insert(0,str(ROOT))
from tools.project_tasks import protocol_cpu as cpu, protocol_fix_cpu as fix
from tools.project_tasks import engine, store, transport, retention, migration_fixtures as history
from tools.project_tasks.live_worker import QUESTION
from tools.serve_error_params.stage import Children, h
from tools.serve_clients import short as s
from tools.serve_service import package as pkg

BASE=cpu.BASE; ATTEMPT=BASE/"retry-01"
PLAN=ATTEMPT/"plan.json"; OUT=ATTEMPT/"execution"
HISTORICAL=ATTEMPT/"historical-runtime"; TASKS=HISTORICAL/".q4t-work/tasks"
PORT,TLS_PORT=18130,18450
LIMITS=dict(starts=2,generation_attempts=4,controls=80,seconds=300)
GPU_WINDOW=270
HERE="tools/project_tasks/"
DRIVER_SOURCES=sorted(set((*cpu.SOURCES,HERE+"protocol_fix_cpu.py",HERE+"protocol_stage.py",
    HERE+"protocol_driver_cpu.py",HERE+"protocol_report.py",HERE+"migration_worker.py",
    "tools/serve_error_params/stage.py","tools/serve_clients/short.py",
    "tools/serve_cuda_quota_integration/verify.py","tools/verify/startup_delivery/deploy.py",
    "docs/SERVE_TASK_PROTOCOL_REPAIR_2026-11-04.md")))


def sources():
    paths=set(subprocess.check_output(["git","ls-files","tools","cmake"],cwd=ROOT,text=True).splitlines())
    paths.update(DRIVER_SOURCES); paths.update(("docs/MODEL.md","docs/serve-config-v1.schema.json"))
    return {name:pkg.sha(ROOT/name) for name in sorted(paths)}


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists() and not HISTORICAL.exists(),"preserve prior attempt")
    pkg.require(not subprocess.check_output(["git","status","--porcelain"],cwd=ROOT),"commit reviewed stage first")
    fix.gate()
    for folder,count in (("cpu-history-fix",1),("cpu-driver-v2",5)):
        value=pkg.read(BASE/folder/"result.json")
        pkg.require(value["passed"] and value["tests_run"]==count,"CPU gate")
        for name,digest in value["sources"].items(): pkg.require(pkg.sha(ROOT/name)==digest,"CPU source changed: "+name)
    ATTEMPT.mkdir(mode=0o700)
    historical=history.extract_runtime(6,HISTORICAL); (HISTORICAL/".q4t-work").mkdir(mode=0o700)
    # The historical reader accepts profile and CA paths only inside its own
    # build/.q4t-work root; use that same private boundary for both versions.
    private=HISTORICAL/".q4t-work/private"; private.mkdir(mode=0o700)
    cert,key=s.certificate(private)
    front=s.example(TLS_PORT,PORT); front.update(per_client_inflight=4,global_inflight=8,per_client_rps=100,burst=128)
    policy=dict(version=1,gateway_token=s.secrets.token_hex(32),clients=[
        dict(id="alpha",max_inflight=1,max_control_inflight=2,max_prompt_tokens=12000,max_output_tokens=512,max_body_bytes=131072),
        dict(id="beta",max_inflight=1,max_control_inflight=2,max_prompt_tokens=9000,max_output_tokens=64,max_body_bytes=65536)])
    pkg.save(private/"gateway.json",front); pkg.save(private/"clients.json",policy)
    gateway=s.g.prepare(private/"gateway.json",pkg.read(s.VENDOR)["binary"],cert,key,private/"gateway",private/"clients.json")
    previous=ROOT/".q4t-work/serve-error-params-20261104/package/previous"
    profiles={}; bindings={}; versions=[]
    for label,directory in (("old",previous),("new",ROOT/"build")):
        sidecar=pkg.read(directory/"q4t.release.json"); protocol=sidecar["chat_protocol"]
        binary=directory/"q4t"
        versions.append(dict(label=label,binary=str(binary),binary_sha256=pkg.sha(binary),
            sidecar=str(directory/"q4t.release.json"),sidecar_sha256=pkg.sha(directory/"q4t.release.json"),protocol=protocol["protocol_sha256"]))
        for client in front["clients"]:
            name=client["id"]+"-"+label; path=private/(name+".json")
            pkg.save(path,dict(client_id=client["id"],api_key=client["api_key"],connect_host="127.0.0.1",
                base_url=f'https://{front["server_name"]}:{TLS_PORT}/v1',ca_file=str(cert),model="qwen3.8-flash-next",
                protocol_sha256=protocol["protocol_sha256"],template_sha256=protocol["template"]["reference_sha256"]))
            _,bindings[name]=transport.profile(path); profiles[name]=dict(path=str(path),sha256=pkg.sha(path))
    from tools.project_tasks import protocol
    review=protocol.reviewed(bindings["alpha-old"],bindings["alpha-new"])
    previous_plan=pkg.read(ROOT/".q4t-work/serve-error-params-20261104/package/plan.json")
    release=pkg.read(ROOT/"build/q4t.release.json")
    runtime=pkg.read(release["tested_source_manifest"])
    plan=dict(root=str(ROOT),limits=LIMITS,gpu_window_seconds=GPU_WINDOW,port=PORT,tls_port=TLS_PORT,
        versions=versions,gateway=gateway,frontend=str(private/"gateway.json"),policy=str(private/"clients.json"),
        profiles=profiles,bindings=bindings,protocol_review=review,historical=historical,
        model_dir=previous_plan["model_dir"],model_inputs=previous_plan["model_inputs"],runtime=runtime,
        preserved=previous_plan["preserved"],
        local={str(p):pkg.sha(p) for p in (private/"gateway.json",private/"clients.json",cert)},
        source_files=sources(),source_commit=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
        receipts={name:pkg.sha(BASE/name/"result.json") for name in ("cpu-01","cpu-history-fix","cpu-driver-v2")},
        historical_archive=history.release(6)["evidence"],historical_archive_sha256=history.release(6)["archive_sha256"],
        performance_acceptance=False,long_run=False)
    original=[BASE/"plan.json",BASE/"run.stdout",BASE/"run.stderr"]
    for folder in ("execution","failed-postflight"):
        original.extend(path for path in (BASE/folder).rglob("*") if path.is_file())
    plan["prior_attempt"]={str(path.relative_to(BASE)):pkg.sha(path) for path in sorted(original)}
    plan["day_plans"]=[name for name in plan["preserved"] if name.endswith("/day/plan.json")]
    pkg.require(len(plan["day_plans"])==3,"three retained user day plans")
    pkg.save(PLAN,plan); check(); return dict(prepared=True,models_started=0,listeners_started=0)


def check():
    plan=pkg.read(PLAN)
    pkg.require(plan["root"]==str(ROOT) and plan["limits"]==LIMITS and plan["gpu_window_seconds"]==GPU_WINDOW and
        (plan["port"],plan["tls_port"])==(PORT,TLS_PORT),"stage scope")
    for name,digest in {**plan["source_files"],**plan["runtime"]}.items(): pkg.require(pkg.sha(ROOT/name)==digest,"frozen source changed: "+name)
    for name,digest in {**plan["preserved"],**plan["local"]}.items(): pkg.require(pkg.sha(name)==digest,"fixed artifact changed")
    for name in plan["day_plans"]: pkg.require(not (Path(name).parent/"execution").exists(),"user day executed")
    for name,digest in plan["receipts"].items(): pkg.require(pkg.sha(BASE/name/"result.json")==digest,"CPU receipt changed")
    for name,digest in plan["prior_attempt"].items(): pkg.require(pkg.sha(BASE/name)==digest,"prior failed attempt changed")
    for name,digest in plan["model_inputs"].items(): pkg.require(pkg.sha(Path(plan["model_dir"])/name)==digest,"model input changed")
    for row in plan["versions"]:
        pkg.require(pkg.sha(row["binary"])==row["binary_sha256"] and pkg.sha(row["sidecar"])==row["sidecar_sha256"],"binary/sidecar changed")
    pkg.require(pkg.sha(ROOT/plan["historical_archive"])==plan["historical_archive_sha256"],"historical archive changed")
    for name,digest in plan["historical"]["sources"].items(): pkg.require(pkg.sha(HISTORICAL/name)==digest,"historical runtime changed")
    for name,row in plan["profiles"].items():
        pkg.require(pkg.sha(row["path"])==row["sha256"] and transport.profile(row["path"])[1]==plan["bindings"][name],"profile changed")
    s.g.check(plan["gateway"]["package"],plan["gateway"]["manifest_sha256"])
    return plan


class Run:
    def __init__(self,plan):
        self.plan=plan; self.started=time.monotonic(); self.services=[]
        self.counts=dict(starts=0,generation_attempts=0,controls=0); self.finished=False

    def budget(self,kind):
        pkg.require(time.monotonic()-self.started<LIMITS["seconds"] and self.counts[kind]<LIMITS[kind],"stage budget: "+kind)
        self.counts[kind]+=1
        pkg.save(OUT/f'attempt-{kind}-{self.counts[kind]:03d}.json',dict(self.counts))

    def launch(self,index):
        h.require_guard(OUT)
        selected=self.plan["versions"][index]; phase=OUT/selected["label"]; phase.mkdir(mode=0o700)
        s.OUT=phase; s.PORT=PORT; s.TLS_PORT=TLS_PORT; s.LIMITS=LIMITS
        service=s.Run(dict(self.plan,binary=selected["binary"],protocol=selected["protocol"]))
        service.children=Children(OUT/"lease"); service.budget=self.budget
        self.services.append(service); self.service=service
        service.launch(True)
        return service

    def known(self,client,tokens):
        self.budget("generation_attempts"); folder=OUT/(client+"-old")
        argv=[sys.executable,"-B",str(ROOT/HERE/"migration_worker.py"),str(HISTORICAL),str(TASKS),
            self.plan["profiles"][client+"-old"]["path"],str(ROOT),str(folder),str(tokens),QUESTION]
        process=self.service.children.launch(folder,argv)
        deadline=time.monotonic()+75
        while not (folder/"boundary.json").exists():
            pkg.require(self.service.children.running(process) and time.monotonic()<deadline,"historical saved tool boundary")
            time.sleep(.05)
        boundary=pkg.read(folder/"boundary.json")
        pkg.require(boundary["pid"]==process.pid and boundary["kind"]=="tool_result","actual application boundary")
        self.service.children.signal(process,signal.SIGKILL)
        self.service.children.wait(process,folder,10,expected=-signal.SIGKILL)
        with store.open_task(TASKS,store.task_name(client,"migration-query")) as ledger:
            state=engine.replay(ledger)
            pkg.require(state["requests"]==state["tool_results"]==1,"known result boundary")
            pkg.save(OUT/(client+"-known-ledger.json"),dict(meta=ledger.meta,events=ledger.events(),database_sha256=pkg.sha(ledger.path/"ledger.db")))

    def cli(self,client,version,label,action,*extra,expected=0,generation=False):
        self.budget("generation_attempts" if generation else "controls")
        argv=[sys.executable,"-B",str(ROOT/HERE/"runner.py"),action,"--root",str(TASKS),"--profile",
            self.plan["profiles"][client+"-"+version]["path"],"--key","migration-query",*map(str,extra)]
        started=time.monotonic()
        value=subprocess.run(argv,cwd=ROOT,capture_output=True,text=True,timeout=75)
        pkg.save(OUT/(client+"-"+label+".json"),dict(argv=argv,returncode=value.returncode,stdout=value.stdout,stderr=value.stderr,seconds=time.monotonic()-started))
        pkg.require(value.returncode==expected,"application command: "+label)
        return json.loads(value.stderr if expected==1 else value.stdout)

    def resume(self,client,tokens):
        denied=self.cli(client,"new","unmigrated","resume",expected=1)
        pkg.require(denied["code"]=="binding_changed","unmigrated protocol refusal")
        path=ATTEMPT/"plans"/(client+"-protocol.json")
        prepared=self.cli(client,"new","plan-protocol","plan-protocol","--plan",path,"--reason","explicit reviewed SRV-01c upgrade")
        wrong=self.cli(client,"new","wrong-operation","apply-trust","--plan",path,"--plan-sha256",prepared["plan_sha256"],expected=1)
        pkg.require(wrong["code"]=="plan_changed","mixed operation refusal")
        moved=self.cli(client,"new","applied","apply-protocol","--plan",path,"--plan-sha256",prepared["plan_sha256"])
        pkg.require(moved["no_execution"] and moved["requests"]==moved["saved_tool_results"]==1,"metadata-only upgrade")
        duplicate=self.cli(client,"new","duplicate-apply","apply-protocol","--plan",path,"--plan-sha256",prepared["plan_sha256"])
        pkg.require(duplicate["already_applied"] and duplicate["revision"]==moved["revision"],"duplicate apply mutated task")
        final=self.cli(client,"new","completed","resume",generation=True)
        pkg.require(final["state"]=="COMPLETED" and final["requests"]==2 and final["tool_attempts"]==final["saved_tool_results"]==1 and
            "Qwen4ExpForConditionalGeneration" in final["answer"] and "[docs/MODEL.md:L1-L24]" in final["answer"],"actual answer/citation")
        duplicate=self.cli(client,"new","duplicate-submit","submit","--question",QUESTION,"--max-requests",2,"--max-tools",1,"--max-tokens",tokens)
        pkg.require(duplicate==dict(final,created=False),"duplicate submit changed task")
        with store.open_task(TASKS,store.task_name(client,"migration-query")) as ledger:
            pkg.save(OUT/(client+"-ledger.json"),dict(meta=ledger.meta,events=ledger.events(),database_sha256=pkg.sha(ledger.path/"ledger.db")))
            shutil.copytree(ledger.path,OUT/(client+"-completed-task"))

    def archived(self,client,tokens,prune):
        for kind in (("archive","prune") if prune else ("archive",)):
            path=ATTEMPT/"plans"/(client+"-"+kind+".json")
            plan=self.cli(client,"new","plan-"+kind,"plan-"+kind,"--plan",path,"--reason","preserve receipt and verify permanent key")
            self.cli(client,"new","apply-"+kind,"apply-"+kind,"--plan",path,"--plan-sha256",plan["plan_sha256"])
        denied=self.cli(client,"old","archive-reverse","plan-protocol","--plan",ATTEMPT/"plans"/(client+"-reverse.json"),"--reason","unregistered reverse negative",expected=1)
        pkg.require(denied["code"]=="protocol_unsupported","archived reverse migration accepted")
        value=self.cli(client,"new","archive-duplicate-submit","submit","--question",QUESTION,"--max-requests",2,"--max-tools",1,"--max-tokens",tokens,expected=2)
        pkg.require(value["state"]=="ARCHIVED" and not value["created"] and value["payload_state"]==("purged" if prune else "retained"),"permanent key replay")
        resumed=self.cli(client,"new","archive-resume","resume",expected=2)
        pkg.require(resumed["no_execution"],"archive resumed execution")
        pkg.save(OUT/(client+"-archive-key.json"),retention.key_value(TASKS/"retention",store.task_name(client,"migration-query")))

    def stop(self,label):
        metrics=self.service.idle("final-idle"); pkg.save(OUT/(label+"-metrics.json"),metrics)
        pkg.require(metrics["q4t_requests_success_total"]==2 and
            metrics["q4t_structured_workers_started_total"]==metrics["q4t_structured_workers_reaped_total"]==2,"phase request/grammar counts")
        self.service.stop(True); self.service.children.reap(OUT/label)
        s.a.require_idle(OUT/(label+"-gpu-idle.json")); h.require_guard(OUT)
        pkg.save(OUT/(label+"-stopped.json"),dict(monotonic=time.monotonic(),lease_still_held=True))

    def business(self):
        store.init(TASKS,2); (ATTEMPT/"plans").mkdir(mode=0o700)
        self.launch(0); self.known("alpha",512); self.known("beta",64); self.stop("old")
        self.launch(1); self.resume("alpha",512); self.resume("beta",64)
        retention.enable(TASKS,max_keys=2,min_retain_seconds=0)
        self.archived("alpha",512,False); self.archived("beta",64,True)
        pkg.save(OUT/"retention-status.json",retention.status(TASKS))
        self.stop("new")

    def execute(self):
        pkg.require(not (OUT/"begin.json").exists(),"preserve prior execution")
        h.require_guard(OUT); pkg.save(OUT/"begin.json",dict(monotonic=self.started,unix_seconds=time.time()))
        result=dict(status="FAILED",performance_acceptance=False,long_run=False)
        def interrupted(sig,_): raise RuntimeError("validation interrupted: "+str(sig))
        handlers={sig:signal.signal(sig,interrupted) for sig in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT)}
        signal.setitimer(signal.ITIMER_REAL,GPU_WINDOW-45)
        try:
            s.a.require_idle(OUT/"gpu-before.json")
            for port in (PORT,TLS_PORT):
                with socket.socket() as sock:
                    sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); sock.bind(("127.0.0.1",port))
            self.business(); check()
            result.update(status="PASS_TASK_PROTOCOL_SHORT",model_starts=2,gateway_starts=2,complete_responses=4,real_tools=2)
        except BaseException as error:
            result.update(status="ENV_ABORT" if "ENV_ABORT" in str(error) else "FAILED",error=repr(error)); raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0); errors=[]
            for service in reversed(self.services):
                for connection in service.sockets:
                    try: connection.close()
                    except BaseException as error: errors.append(repr(error))
                try: service.children.cleanup()
                except BaseException as error: errors.append(repr(error))
            try:
                pkg.require(not errors,"own cleanup: "+repr(errors)); s.a.require_idle(OUT/"gpu-final.json")
                pkg.require(time.monotonic()-self.started<=GPU_WINDOW,"GPU time bound")
                result["cleanup_confirmed"]=True
            except BaseException as error:
                result.update(status="FAILED",cleanup_error=repr(error)); raise
            finally:
                for sig,handler in handlers.items(): signal.signal(sig,handler)
                result.update(counts=self.counts,seconds=time.monotonic()-self.started)
                pkg.save(OUT/"result.json",result)


if __name__=="__main__":
    os.umask(0o077); parser=argparse.ArgumentParser(); parser.add_argument("action",choices=("prepare","check","run"))
    action=parser.parse_args().action
    if action=="prepare": print(json.dumps(prepare()))
    elif action=="check": check(); print("protocol stage checked; no model/HTTP")
    else: Run(check()).execute()
