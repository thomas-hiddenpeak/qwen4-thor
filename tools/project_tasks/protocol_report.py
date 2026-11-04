"""Lossless receipts and offline replay of the bounded protocol handoff."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True; sys.path.insert(0,str(ROOT))
from tools.project_tasks import protocol_stage as s, engine, store, retention, migration, protocol
from tools.serve_service import package as pkg
from tools.serve_cuda_quota import correction as custody

BASE=s.BASE; DEST=ROOT/"docs/evidence/serve-task-protocol-20261104"
require=pkg.require; sha=lambda raw:hashlib.sha256(raw).hexdigest()
READERS=sorted(set((*engine.RUNTIME_FILES,"tools/project_tasks/protocol_report.py",
    "tools/project_tasks/protocol_stage.py","tools/project_tasks/protocol_cpu.py","tools/project_tasks/protocol_fix_cpu.py")))


def postflight():
    plan=s.check(); out=BASE/"postflight"; out.mkdir(mode=0o700)
    s.s.a.require_idle(out/"gpu.json"); lock=s.s.a.guard.common_lock(); info=lock.stat()
    require((info.st_dev,info.st_ino)==(66305,3294067) and s.s.a.lock_available(lock),"original GPU lock not released")
    owners=[]
    for path in sorted(s.OUT.rglob("identity.json")):
        owner=pkg.read(path)
        try: current=custody.proc(owner["pid"])
        except FileNotFoundError: current=None
        require(current is None or current["start_ticks"]!=owner["start_ticks"],"own process remains")
        owners.append(dict(pid=owner["pid"],start_ticks=owner["start_ticks"],gone=True))
    require(len(owners)==6,"expected two backend/gateway pairs and two applications")
    for port in (s.PORT,s.TLS_PORT):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); sock.bind(("127.0.0.1",port))
    listeners=subprocess.check_output(["ss","-ltn",f"( sport = :{s.PORT} or sport = :{s.TLS_PORT} )"],text=True)
    (out/"listeners.txt").write_text(listeners); require(len(listeners.splitlines())==1,"retained listener")
    unit="soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service"
    raw=subprocess.check_output(["systemctl","--user","show",unit,"-p","LoadState","-p","ActiveState","-p","SubState","-p","MainPID"],text=True)
    (out/"soar-unit.txt").write_text(raw)
    require(dict(line.split("=",1) for line in raw.splitlines())==
        dict(LoadState="masked",ActiveState="inactive",SubState="dead",MainPID="0"),"recovery unit changed")
    containers=subprocess.check_output(["sudo","-n","docker","ps","--format","{{.ID}} {{.Names}}"],text=True)
    (out/"containers.txt").write_text(containers); require(not containers.strip(),"running container")
    pkg.save(out/"result.json",dict(passed=True,owners=owners,lock=dict(dev=info.st_dev,inode=info.st_ino,released=True),
        ports=[s.PORT,s.TLS_PORT],default_unchanged=True,day_plans=plan["day_plans"],long_run=False))


def tasks(data,plan,ends):
    read=lambda name:json.loads(data[name]); usage={}; ids=set()
    parent=BASE/"offline"; parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        for client,tokens in (("alpha",512),("beta",64)):
            known=read("execution/"+client+"-known-ledger.json"); final=read("execution/"+client+"-ledger.json")
            require(known["meta"]==final["meta"] and known["events"]==final["events"][:len(known["events"])] and
                known["events"][-1]["kind"]=="tool_result","saved history changed")
            prefix="execution/"+client+"-completed-task/"; folder=Path(temp)/final["meta"]["task_id"]; folder.mkdir(mode=0o700)
            for name,raw in data.items():
                if name.startswith(prefix): store.write_new(folder/name[len(prefix):],raw)
            require(sha(data[prefix+"ledger.db"])==final["database_sha256"],"ledger identity")
            db=store.connection(folder)
            try:
                ledger=store.Ledger(folder,db,final["meta"]["store_id"]); state=engine.replay(ledger); public=engine.public(ledger,state)
                require(ledger.meta==final["meta"] and ledger.events()==final["events"] and
                    ledger.spec["runtime_sources"]==plan["historical"]["sources"] and ledger.spec["binding"]==plan["bindings"][client+"-old"] and
                    state["execution_spec"]["binding"]==plan["bindings"][client+"-new"] and
                    state["execution_spec"]["runtime_sources"]=={n:plan["source_files"][n] for n in engine.RUNTIME_FILES},"runtime/protocol binding")
                expected=json.loads(read("execution/"+client+"-completed.json")["stdout"])
                require(public==expected and public["state"]=="COMPLETED" and public["outcome"]=="ANSWERED" and public["requests"]==2 and
                    public["tool_attempts"]==public["saved_tool_results"]==public["migrations"]==1 and public["max_tokens"]==tokens and
                    "Qwen4ExpForConditionalGeneration" in public["answer"] and "[docs/MODEL.md:L1-L24]" in public["answer"],"answer/budgets")
                by=lambda kind:[row["payload"] for row in ledger.events() if row["kind"]==kind]
                change=by("migration")[0]["plan"]
                require(change["format"]==migration.PROTOCOL_FORMAT and change["source_release"]=="srv11g-7d0d8dec" and
                    change["protocol_review"]==plan["protocol_review"],"explicit reviewed pair")
                protocol.validate(change["protocol_review"],change["source_spec"]["binding"],change["target_spec"]["binding"])
                require([row["protocol_source_sha256"] for row in by("preflight_result")]==
                    [v["protocol"] for v in plan["versions"]],"old/new actual preflight")
                require(by("tool_started")[0]["name"]=="read_project_record" and by("tool_started")[0]["arguments"]==
                    dict(path="docs/MODEL.md",start_line=1,line_count=24),"actual tool arguments")
                returned=json.loads(by("tool_result")[0]["content"])
                require(returned["ok"] and returned["source"]["sha256"]==plan["source_files"]["docs/MODEL.md"],"actual source content")
                observations=[json.loads(raw) for raw in data["execution/"+client+"-old/read-observations.jsonl"].splitlines()]
                require(len(observations)==1 and observations[0]["invocation"]["name"]=="read_project_record","exactly one real tool execution")
                usage[client]=[row["completion"]["usage"] for row in by("response")]
                require(len(usage[client])==2 and all(0<row["completion_tokens"]<=tokens for row in usage[client]),"output bounds")
                ids.update(row["completion"]["id"] for row in by("response"))
                require(json.loads(read("execution/"+client+"-duplicate-apply.json")["stdout"])["already_applied"] and
                    json.loads(read("execution/"+client+"-duplicate-submit.json")["stdout"])==dict(public,created=False),"active duplicate")
                key=read("execution/"+client+"-archive-key.json")
                archived=retention.key_state(key,read("tasks/store.json"),ledger.meta["task_id"])
                require(archived["binding"]==plan["bindings"][client+"-new"] and archived["rekeys"]==0 and
                    archived["payload"]==("retained" if client=="alpha" else "purged") and
                    key["base"]["contract_sha256"]==retention.business_contract(state["execution_spec"]) and
                    key["base"]["answer_sha256"]==store.digest(public["answer"]),"permanent key identity")
                for label in ("archive-duplicate-submit","archive-resume"):
                    value=json.loads(read("execution/"+client+"-"+label+".json")["stdout"])
                    require(value["state"]=="ARCHIVED" and value["no_execution"] and value["payload_state"]==archived["payload"],"archive no execution")
                prefix="tasks/retention/payloads/"+ledger.meta["task_id"]+"/"
                if client=="alpha": require(all(sha(data[prefix+name])==row["sha256"] for name,row in archived["files"].items()),"retained payload bytes")
                else: require(not any(name.startswith(prefix) for name in data),"pruned payload retained")
            finally: db.close()
    require(ids=={row["id"] for row in ends},"backend response correlation")
    return usage


def evaluate(data):
    read=lambda name:json.loads(data[name]); plan=read("plan.json"); result=read("execution/result.json")
    first=read("cpu-01/result.json")
    require(first["tests_run"]==140 and first["errors"]==1 and first["failures"]==0 and not first["passed"] and
        b"KeyError: 'previous'" in data["cpu-01/tests.log"],"original fixture failure lost")
    for folder,count in (("cpu-history-fix",1),("cpu-driver",4)):
        receipt=read(folder+"/result.json")
        require(receipt["passed"] and receipt["tests_run"]==count and receipt["failures"]==receipt["errors"]==0 and
            receipt["models_started"]==receipt["listeners_started"]==receipt["gpu_started"]==0,"CPU acceptance")
    for folder in ("cpu-01","cpu-history-fix","cpu-driver"):
        receipt=read(folder+"/result.json"); require(sha(data[folder+"/result.json"])==plan["receipts"][folder],"CPU receipt identity")
        for name,digest in receipt["sources"].items():
            require(sha(data[folder+"/source/"+name])==digest,"CPU source snapshot")
            if folder!="cpu-01" or name!="tools/project_tasks/test_protocol.py": require(digest==plan["source_files"][name],"CPU runtime changed")
    for name,digest in plan["historical"]["sources"].items(): require(sha(data["historical-source/"+name])==digest,"historical source")
    require(result["status"]=="PASS_TASK_PROTOCOL_SHORT" and result["cleanup_confirmed"] and result["seconds"]<=270 and
        result["counts"]["starts"]==2 and result["counts"]["generation_attempts"]==4 and result["counts"]["controls"]<=80 and not result["long_run"],"bounded real result")
    ends=[]
    for label in ("old","new"):
        log=data[f"execution/{label}/scoped-model/stderr.log"].decode()
        records=[dict(re.findall(r"([a-z_]+)=([^ ]+)",row)) for row in log.splitlines() if row.startswith("[q4t][request]") and " event=end " in row]
        require(len(records)==2 and all(row["outcome"]=="success" and row["reason"]=="completed" and row["released"]=="1" for row in records) and
            log.count("grammar worker reaped=1 ")==2 and "shutdown complete (0 in-flight remaining)" in log,"backend responses and drain")
        ends+=records
        metrics=read("execution/"+label+"-metrics.json")
        require(metrics["q4t_requests_success_total"]==metrics["q4t_structured_workers_started_total"]==metrics["q4t_structured_workers_reaped_total"]==2 and
            all(metrics[name]==0 for name in s.s.a.EMPTY),"phase resource release")
        for component in ("scoped-model","nginx"): require(read(f"execution/{label}/{component}/reaped.json")["returncode"]==0,"service child exit")
    require(read("execution/old-stopped.json")["monotonic"]<read("execution/new/scoped-model/identity.json")["registered_monotonic"],"backend overlap")
    for client in ("alpha","beta"):
        require(read("execution/"+client+"-old/reaped.json")["returncode"]==-9,"application not interrupted")
        for label,code in (("unmigrated","binding_changed"),("wrong-operation","plan_changed"),("archive-reverse","protocol_unsupported")):
            value=read("execution/"+client+"-"+label+".json")
            require(value["returncode"]==1 and json.loads(value["stderr"])["code"]==code,"metadata refusal")
    usage=tasks(data,plan,ends)
    state=read("execution/retention-status.json")
    require((state["active_tasks"],state["archived_keys"],state["retained_payloads"])==(0,2,1),"permanent key counts")
    events=[json.loads(raw) for raw in data["execution/lease/events.jsonl"].splitlines()]
    starts=[row for row in events if row["event"]=="lease_acquired"]; stops=[row for row in events if row["event"]=="lease_released"]
    observations=[row for row in events if row["event"] in ("preflight","monitor")]
    require(len(starts)==len(stops)==1 and stops[0]["status"]==read("execution/lease/exit.json")["status"]=="COMPLETED" and
        observations and all(not row["conflicts"] for row in observations),"shared lease coverage")
    seconds=stops[0]["monotonic"]-starts[0]["monotonic"]; require(0<seconds<=270,"GPU duration")
    post=read("postflight/result.json")
    require(post["passed"] and len(post["owners"])==6 and all(row["gone"] for row in post["owners"]) and
        post["lock"]==dict(dev=66305,inode=3294067,released=True) and post["default_unchanged"] and
        post["day_plans"]==plan["day_plans"] and read("postflight/gpu.json")["pids"]==[],"postflight release")
    require(not data["postflight/containers.txt"].strip() and len(data["postflight/listeners.txt"].splitlines())==1 and
        dict(row.split("=",1) for row in data["postflight/soar-unit.txt"].decode().splitlines())==
        dict(LoadState="masked",ActiveState="inactive",SubState="dead",MainPID="0"),"service/container state")
    return dict(passed=True,cpu_effective_tests=140,driver_tests=4,cpu_file_operation_kills=len(first["file_operation_kills"]),
        models=2,gateways=2,complete_responses=4,real_tools=2,application_kills=2,active_protocol_migrations=2,
        duplicate_applies=2,duplicate_submits=4,archive_resumes_without_execution=2,metadata_refusals=6,
        permanent_keys=2,retained_payloads=1,usage=usage,gpu_seconds=seconds,guard_observations=len(observations),
        seconds=result["seconds"],controls=result["counts"]["controls"],owned_exited=6,default_unchanged=True,
        reviewed_transition=plan["protocol_review"],original_fixture_failure_preserved=True,
        actual_archived_protocol_migrations=0,archived_protocol_migrations_cpu_only=True,
        long_run=False,performance_acceptance=False,actual_lan_accepted=False,physical_ram_limit=False)


def archive():
    plan=s.check(); data={"plan.json":s.PLAN.read_bytes()}
    for folder in ("cpu-01","cpu-history-fix","cpu-driver","execution","postflight","plans"):
        for path in sorted((BASE/folder).rglob("*")):
            if path.is_file(): data[str(path.relative_to(BASE))]=path.read_bytes()
    for path in sorted(s.TASKS.rglob("*")):
        if path.is_file(): data["tasks/"+str(path.relative_to(s.TASKS))]=path.read_bytes()
    for name in plan["historical"]["sources"]: data["historical-source/"+name]=(s.HISTORICAL/name).read_bytes()
    for name in READERS: data["source/"+name]=(ROOT/name).read_bytes()
    gateway=Path(plan["gateway"]["package"])
    for name in ("manifest.json","certificate.pem"): data["gateway/"+name]=(gateway/name).read_bytes()
    for label in ("access.log","error.log"):
        path=gateway/"runtime"/label
        if path.exists(): data["gateway/"+label]=path.read_bytes()
    secrets=[row["api_key"].encode() for row in pkg.read(plan["frontend"])["clients"]]
    secrets.append(pkg.read(plan["policy"])["gateway_token"].encode())
    for name,raw in data.items():
        require(len(raw)<32*1024**2 and not any(secret in raw for secret in secrets) and
            not re.search(br"(?m)^-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----\r?$",raw),"private/oversized record: "+name)
    result=evaluate(data); DEST.mkdir()
    with (DEST/"records.tar.gz").open("xb") as stream,gzip.GzipFile(fileobj=stream,mode="wb",mtime=0,filename="") as compressed:
        with tarfile.open(fileobj=compressed,mode="w") as tar:
            for name,raw in sorted(data.items()):
                info=tarfile.TarInfo(name); info.size=len(raw); info.mode=0o600; info.mtime=0; tar.addfile(info,io.BytesIO(raw))
    pkg.save(DEST/"result.json",result)
    pkg.save(DEST/"manifest.json",dict(records={name:dict(bytes=len(raw),sha256=sha(raw)) for name,raw in sorted(data.items())},
        archive_sha256=pkg.sha(DEST/"records.tar.gz"),result_sha256=pkg.sha(DEST/"result.json"),
        readers={name:pkg.sha(ROOT/name) for name in READERS},
        local_binaries={str(path):pkg.sha(path) for path in [*(Path(v["binary"]) for v in plan["versions"]),
            Path(pkg.read(gateway/"manifest.json")["nginx"]["path"]),Path(sys.executable).resolve(),Path("/usr/bin/openssl")]}))
    print(json.dumps(dict(archived=len(data),bytes=(DEST/"records.tar.gz").stat().st_size,passed=True)))


def verify():
    manifest=pkg.read(DEST/"manifest.json")
    for name,digest in manifest["readers"].items(): require(pkg.sha(ROOT/name)==digest,"offline reader changed")
    require(pkg.sha(DEST/"records.tar.gz")==manifest["archive_sha256"] and pkg.sha(DEST/"result.json")==manifest["result_sha256"],"archive/result identity")
    with tarfile.open(DEST/"records.tar.gz") as tar:
        members=tar.getmembers()
        require(len(members)==len(manifest["records"]) and all(m.isfile() and m.size<32*1024**2 for m in members),"archive members")
        data={member.name:tar.extractfile(member).read() for member in members}
    require(set(data)==set(manifest["records"]),"archive file set")
    for name,raw in data.items(): require(manifest["records"][name]==dict(bytes=len(raw),sha256=sha(raw)),"record changed")
    require(evaluate(data)==pkg.read(DEST/"result.json"),"offline replay differs")
    for name,digest in manifest["local_binaries"].items(): require(pkg.sha(name)==digest,"binary path changed")
    print(json.dumps(dict(passed=True,records=len(data),models_started=0,http_started=0)))


if __name__=="__main__":
    os.umask(0o077); parser=argparse.ArgumentParser(); parser.add_argument("action",choices=("postflight","archive","verify"))
    globals()[parser.parse_args().action]()
