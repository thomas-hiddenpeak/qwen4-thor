"""Finish only unexecuted metadata checks; preserve the failed live driver."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import error_stage as s, store, engine, backup, retention, transport

BASE = s.BASE
OUT = BASE / "offline-finish"
SOURCES = ("tools/project_tasks/error_finish.py", "tools/project_tasks/error_noexec.py",
    "tools/project_tasks/test_error_finish.py", "tools/project_tasks/error_finish_cpu.py",
    "tools/project_tasks/error_report.py", "docs/SERVE_TASK_ERRORS_FINISH_2026-11-04.md")


def export_task(root, binding, key):
    # The profile alias is never a client identity. Use its validated binding.
    name = store.task_name(binding["client_id"], key)
    with store.open_task(root, name, exclusive=False) as ledger:
        exported = dict(meta=ledger.meta, events=ledger.events(),
            database_sha256=s.pkg.sha(ledger.path / "ledger.db"))
        return exported, engine.public(ledger, engine.replay(ledger))


def check():
    plan = s.pkg.read(s.PLAN)
    for name, checksum in plan["source_files"].items():
        if name == "tools/project_tasks/error_report.py":
            path = BASE / "cpu-driver/source" / name
        else: path = ROOT / name
        store.require(s.pkg.sha(path) == checksum, "original runtime/driver changed")
    for name, checksum in {**plan["default"], **plan["private"], **plan["days"]}.items():
        store.require(s.pkg.sha(name) == checksum, "default/private/day changed")
    for name in plan["days"]:
        store.require(not (Path(name).parent / "execution").exists(), "day started")
    for alias, row in plan["profiles"].items():
        store.require(s.pkg.sha(row["path"]) == row["sha256"] and
            transport.profile(row["path"])[1] == plan["bindings"][alias], "profile changed")
    failed = s.pkg.read(s.OUT / "result.json")
    store.require(failed["status"] == "FAILED" and failed["cleanup_confirmed"] and failed["counts"] ==
        dict(generations=4, negatives=3, controls=11) and failed["error"] ==
        "FileNotFoundError(2, 'No such file or directory')", "original failure boundary")
    tested = s.pkg.read(BASE / "cpu-finish/result.json")
    store.require(tested["passed"] and tested["tests_run"] == 2, "continuation CPU gate")
    for name, checksum in tested["sources"].items():
        store.require(s.pkg.sha(ROOT / name) == checksum, "continuation source changed")
    return plan


def run():
    os.umask(0o077)
    plan = check(); OUT.mkdir(mode=0o700)
    before = backup.regular_tree(s.TASKS)
    original = {str(path.relative_to(BASE)): s.pkg.sha(path) for path in s.OUT.rglob("*") if path.is_file()}
    started=time.monotonic(); commands=[]
    def cli(alias, action, key, label, expected, extra=(), code=None):
        marker=OUT/(label+"-forbidden.json")
        argv=[sys.executable,"-B",ROOT/"tools/project_tasks/error_noexec.py",marker,action,
            "--root",s.TASKS,"--profile",plan["profiles"][alias]["path"],"--key",key,*extra]
        child=subprocess.run(list(map(str,argv)),capture_output=True,text=True,timeout=15)
        s.pkg.save(OUT/(label+".json"),dict(returncode=child.returncode,stdout=child.stdout,stderr=child.stderr,
            execution_forbidden=True,tripwire_triggered=marker.exists()))
        store.require(child.returncode==expected and not marker.exists(),"metadata command executed or failed")
        commands.append(label)
        if code:
            value=json.loads(child.stderr)
            store.require(not child.stdout and value["profile"]=="q4t-project-error-v1" and
                value["code"]==code and value["operation"]==action and not value["automatic_retry"],"continued typed refusal")
            return value
        store.require(not child.stderr,"unexpected stderr")
        return json.loads(child.stdout)
    exported,state=export_task(s.TASKS,plan["bindings"]["denied"],"unauth")
    s.pkg.save(OUT/"denied-ledger.json",exported)
    store.require(state["state"]=="READY" and state["runs"]==1 and state["requests"]==
        state["preflight_checks"]==state["tool_attempts"]==0,"authentication task state")
    duplicate=cli("denied","submit","unauth","denied-duplicate",2,
        ("--question",s.QUESTION,"--max-requests","2","--max-tools","1","--max-tokens","64"))
    store.require(duplicate==dict(state,created=False),"auth error retried")
    cli("beta","resume","unauth","binding-conflict",1,code="binding_changed")
    cli("alpha","submit","overflow","capacity",1,("--question",s.QUESTION),code="task_capacity")
    path=OUT/"archive-plan.json"
    planned=cli("alpha","plan-archive","same","archive-prepared",0,
        ("--plan",path,"--reason","review exact error contract"))
    store.require(planned["prepared"],"archive plan not prepared")
    cli("alpha","apply-archive","same","changed-plan",1,
        ("--plan",path,"--plan-sha256","0"*64),code="plan_changed")
    store.require(backup.regular_tree(s.TASKS)==before,"offline metadata changed task state")
    store.require(all(s.pkg.sha(BASE/name)==sha for name,sha in original.items()),"original live evidence changed")
    s.pkg.save(OUT/"final-retention.json",retention.status(s.TASKS))
    post=s.pkg.read(BASE/"postflight/result.json")
    store.require(all(not s.retained.r.owner_live(owner) for owner in post["owners"]),"owned service restarted")
    check()
    result=dict(passed=True,status="PASS_ERRORS_OFFLINE_FINISH",models_started=0,listeners_started=0,http_requests=0,
        tools_executed=0,gpu_started=0,commands=commands,task_tree=before,original=original,
        source_files={name:s.pkg.sha(ROOT/name) for name in SOURCES},original_plan_sha256=s.pkg.sha(s.PLAN),
        cpu_sha256=s.pkg.sha(BASE/"cpu-finish/result.json"),seconds=time.monotonic()-started,
        classified_errors=3,duplicate_submissions=1,task_state_unchanged=True,original_live_evidence_unchanged=True)
    store.require(result["seconds"]<=60,"offline continuation deadline")
    s.pkg.save(OUT/"result.json",result)
    print(json.dumps({key:result[key] for key in ("passed","status","commands","seconds","http_requests","tools_executed")}))


if __name__ == "__main__":
    run()
