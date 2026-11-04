"""One explicit correction for the observed Content-Length EOF socket failure."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import stage, store

ORIGINAL = stage.cpu.BASE
BASE = ORIGINAL / "tls-correction"
SOURCES = sorted(set((*stage.SOURCES, "tools/project_tasks/repair_cpu.py",
                      "tools/project_tasks/correction.py")))


def configure():
    stage.BASE, stage.PLAN, stage.OUT = BASE, BASE / "plan.json", BASE / "execution"
    stage.TASKS, stage.PROFILE = BASE / "tasks", ORIGINAL / "private/client.json"
    stage.configure()
    stage.check = check


def original():
    value = stage.pkg.read(ORIGINAL / "execution/result.json")
    store.require(value["status"] == "FAILED" and value["cleanup_confirmed"] and
        value["counts"]["generations"] == value["counts"]["negatives"] == 0 and
        "known" in value["error"], "unexpected original failure")
    log = (ORIGINAL / "execution/known/child.log").read_text()
    store.require("Bad file descriptor" in log and "capabilities" in log,
                  "not the observed EOF failure")
    terminals = list((ORIGINAL / "owned-generations/generations").glob("*/run/terminal.json"))
    store.require(len(terminals) == 2 and all(stage.pkg.read(p)["state"] == "STOPPED" and
        stage.pkg.read(p)["cleanup_confirmed"] for p in terminals), "old service cleanup missing")
    return value


def prepare():
    original()
    store.require(not BASE.exists() and not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT), "preserve prior correction/commit sources")
    fixed = stage.pkg.read(ORIGINAL / "cpu-transport-fixed/result.json")
    store.require(fixed["passed"] and fixed["effective_tests"] == 20 and fixed["tests_run"] == 2,
                  "targeted correction gate")
    for name, checksum in fixed["sources"].items():
        store.require(stage.pkg.sha(ROOT / name) == checksum, "targeted source changed: " + name)
    plan = stage.pkg.read(ORIGINAL / "plan.json")
    plan.update(source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        source_files={name: stage.pkg.sha(ROOT / name) for name in SOURCES},
        corrected_cpu_sha256=stage.pkg.sha(ORIGINAL / "cpu-transport-fixed/result.json"),
        original_plan_sha256=stage.pkg.sha(ORIGINAL / "plan.json"),
        original_result_sha256=stage.pkg.sha(ORIGINAL / "execution/result.json"),
        correction="Content-Length EOF retires socket; observe response EOF before timeout access",
        unchanged_business_matrix=True, automatic_retry=False)
    BASE.mkdir(mode=0o700)
    stage.pkg.save(stage.PLAN, plan)
    check()
    return dict(prepared=True, new_models_or_listeners=0)


def check():
    original()
    plan = stage.pkg.read(stage.PLAN)
    store.require(plan["limits"] == stage.LIMITS and plan["gpu_window_seconds"] == stage.GPU_WINDOW and
        plan["cases"] == {k: list(v) for k, v in stage.CASES.items()}, "correction expanded budget")
    for name, checksum in plan["source_files"].items():
        store.require(stage.pkg.sha(ROOT / name) == checksum, "correction source changed: " + name)
    for name, checksum in {**plan["default"], **plan["private"], **plan["days"]}.items():
        store.require(stage.pkg.sha(ROOT / name) == checksum, "original identity changed")
    for name in plan["days"]:
        store.require(not (Path(name).parent / "execution").exists(), "operator day executed")
    for path, key in ((stage.PROFILE, "profile_sha256"),
            (ORIGINAL / "cpu-transport-fixed/result.json", "corrected_cpu_sha256"),
            (ORIGINAL / "plan.json", "original_plan_sha256"),
            (ORIGINAL / "execution/result.json", "original_result_sha256")):
        store.require(stage.pkg.sha(path) == plan[key], "original/corrected evidence changed")
    stage.retained.r.binding("backend", plan["package"]["package"], plan["package"]["manifest_sha256"])
    stage.retained.r.gateway.verify(plan["gateways"][0]["package"], plan["gateways"][0]["manifest_sha256"])
    return plan


if __name__ == "__main__":
    os.umask(0o077); configure()
    parser = argparse.ArgumentParser(); parser.add_argument("action", choices=("prepare", "check", "run"))
    args = parser.parse_args()
    if args.action == "prepare":
        value = prepare()
    elif args.action == "check":
        check(); value = dict(checked=True, started=False)
    else:
        plan = check(); store.require(not stage.OUT.exists(), "preserve correction attempt")
        with stage.sup.processes.child_subreaper():
            value = stage.Run(plan).execute()
    print(json.dumps(value))
