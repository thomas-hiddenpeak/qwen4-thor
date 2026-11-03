"""Continue the two saved real tasks after the observed retention admission refusal."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import signal
import time
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import migration_stage as stage, engine, store
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from tools.serve_soak import evidence

ORIGINAL = stage.BASE
BASE = ORIGINAL / "continuation"
PLAN, OUT = BASE / "plan.json", BASE / "execution"
LIMITS = dict(starts=1, generations=2, negatives=1, observations=64, seconds=360)
GPU_WINDOW = 300
SOURCES = sorted(set((*stage.SOURCES, "tools/project_tasks/migration_continue.py",
    "tools/project_tasks/migration_continue_cpu.py", "docs/SERVE_TASK_MIGRATION_CONTINUE_2026-11-04.md")))


def capacity(policy, kinds):
    required = sum(stage.retained.store.RESERVATIONS[kind] for kind in kinds)
    store.require(policy["max_bytes"] >= required and policy["max_generations"] >= len(kinds),
                  "full retained service sequence exceeds frozen capacity")
    return required


def configure():
    stage.BASE, stage.PLAN, stage.OUT = BASE, PLAN, OUT
    stage.LIMITS, stage.GPU_WINDOW = LIMITS, GPU_WINDOW
    stage.configure()
    stage.check = check


def original():
    result = stage.pkg.read(ORIGINAL / "execution/result.json")
    store.require(result["status"] == "FAILED" and result["cleanup_confirmed"] and
        "before reservation" in result["error"] and result["counts"]["negatives"] == 2 and
        "retention capacity exhausted" in (ORIGINAL / "execution/front-2-wrapper.log").read_text(),
        "unexpected original failure")
    # The failed driver's end-only counter is zero; actual server evidence is two.
    metrics = stage.pkg.read(ORIGINAL / "execution/beta-before-kill-00.json")["metrics"]
    store.require(metrics["q4t_requests_total"] == metrics["q4t_requests_success_total"] == 2 and
                  metrics["q4t_requests_error_total"] == 0, "original two successful calls missing")
    folders = list((ORIGINAL / "owned-generations/generations").iterdir())
    store.require(len(folders) == 2, "original service generations")
    for folder in folders:
        seal = stage.pkg.read(folder / "seal.json")
        store.require(seal["proof"]["cleanup_confirmed"] and all(
            not stage.retained.r.owner_live(owner) for owner in seal["proof"]["owners"]), "original service still live")
    return result


def prepare():
    original()
    store.require(not BASE.exists() and not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT), "preserve continuation and commit sources")
    tested = stage.pkg.read(ORIGINAL / "cpu-continuation/result.json")
    store.require(tested["passed"] and tested["tests_run"] == 5, "targeted driver gate")
    for name, digest in tested["sources"].items():
        store.require(stage.pkg.sha(ROOT / name) == digest, "tested source changed: " + name)
    plan = stage.pkg.read(ORIGINAL / "plan.json")
    for name, digest in plan["source_files"].items():
        store.require(stage.pkg.sha(ROOT / name) == digest, "original production source changed")
    BASE.mkdir(mode=0o700)
    snapshot = {}
    for client, row in plan["clients"].items():
        root, _ = store.checked_root(row["root"])
        name = store.task_name(client, "migration-query")
        before = stage.pkg.read(ORIGINAL / "execution" / (client + "-before-ledger.json"))
        with store.open_task(root, name) as ledger:
            state = engine.replay(ledger)
            store.require(ledger.meta == before["meta"] and ledger.events() == before["events"] and
                stage.pkg.sha(ledger.path / "ledger.db") == before["database_sha256"] and
                state["phase"] == "TOOLS_PENDING" and state["tool_results"] == 1 and state["migrations"] == 0,
                "saved task changed since original failure")
            for path in [root / "store.json", root / ".lock", *ledger.path.iterdir()]:
                relative = str(path.relative_to(root))
                target = BASE / "prior-tasks" / client / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                store.write_new(target, path.read_bytes())
                snapshot[str(target.relative_to(BASE))] = stage.pkg.sha(target)
    selected = plan["gateways"][1]
    policy = dict(max_bytes=128 * 1024**2, max_generations=2, max_age_days=14)
    required = capacity(policy, ("backend", "gateway"))
    plan.update(gateways=[selected], store_policy=policy, limits=LIMITS, gpu_window_seconds=GPU_WINDOW,
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        source_files={name: stage.pkg.sha(ROOT / name) for name in SOURCES},
        original_plan_sha256=stage.pkg.sha(ORIGINAL / "plan.json"),
        original_result_sha256=stage.pkg.sha(ORIGINAL / "execution/result.json"),
        targeted_cpu_sha256=stage.pkg.sha(ORIGINAL / "cpu-continuation/result.json"),
        snapshots=snapshot, required_retention_bytes=required, original_successful_requests=2,
        complete_responses=2, preflights=2, capabilities=2, actual_application_kills=0,
        real_tool_executions=0, model_starts=1, gateway_starts=1,
        continuation_only=True, no_automatic_retry=True, no_repeated_historical_work=True)
    stage.pkg.save(PLAN, plan); check()
    return dict(prepared=True, models_started=0, historical_tasks_preserved=2)


def check():
    original()
    plan = stage.pkg.read(PLAN)
    store.require(plan["limits"] == LIMITS and plan["gpu_window_seconds"] == GPU_WINDOW and
        plan["required_retention_bytes"] == capacity(plan["store_policy"], ("backend", "gateway")), "continued envelope")
    for name, digest in plan["source_files"].items():
        store.require(stage.pkg.sha(ROOT / name) == digest, "source changed: " + name)
    for name, digest in {**plan["default"], **plan["private"], **plan["days"]}.items():
        store.require(stage.pkg.sha(ROOT / name) == digest, "fixed identity changed")
    for name in plan["days"]:
        store.require(not (Path(name).parent / "execution").exists(), "user day executed")
    for name, digest in plan["snapshots"].items():
        store.require(stage.pkg.sha(BASE / name) == digest, "original task snapshot changed")
    for path, key in ((ORIGINAL / "plan.json", "original_plan_sha256"),
        (ORIGINAL / "execution/result.json", "original_result_sha256"),
        (ORIGINAL / "cpu-continuation/result.json", "targeted_cpu_sha256")):
        store.require(stage.pkg.sha(path) == plan[key], "original/targeted evidence changed")
    store.require(stage.pkg.sha(plan["new_policy"]["path"]) == plan["new_policy"]["sha256"], "rotated policy changed")
    for row in plan["clients"].values():
        for prefix in ("", "old_"):
            store.require(stage.pkg.sha(row[prefix + "profile"]) == row[prefix + "profile_sha256"] and
                stage.transport.profile(row[prefix + "profile"])[1] == row[prefix + "binding"], "profile changed")
        for name, digest in row["runtime"]["sources"].items():
            store.require(stage.pkg.sha(Path(row["runtime"]["root"]) / name) == digest, "historical runtime changed")
    stage.retained.r.binding("backend", plan["package"]["package"], plan["package"]["manifest_sha256"])
    for selected in plan["gateways"]: stage.gateway.verify(selected["package"], selected["manifest_sha256"])
    return plan


class Run(stage.Run):
    def business(self):
        for client in ("alpha", "beta"): (OUT / client).mkdir(mode=0o700)
        self.start_front(1); self.revoked()
        self.migrated("alpha", 1); self.migrated("beta", 2)
        self.counts["generations"] = 2
        values = self.drain("final", 2)
        store.require(values["q4t_structured_workers_started_total"] == values["q4t_structured_workers_reaped_total"] == 2,
                      "continued grammar cleanup")
        stage.pkg.save(OUT / "final-metrics.json", values)
        self.front_cli(1, "front-1-stop", "stop"); self.wait_front(1, {"STOPPED"}); self.wait_gone(1)
        self.preserve(self.fronts[1]["folder"]); self.stop_backend(); self.preserve(self.backend_folder)


    def execute(self):
        OUT.mkdir(mode=0o700)
        pkg.save(OUT / "begin.json", dict(monotonic=self.started, unix_seconds=time.time()))
        result = dict(status="FAILED", performance_acceptance=False, long_run=False)
        def interrupted(signum, _):
            raise RuntimeError("validation interrupted: " + str(signum))
        handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)}
        try:
            a.require_idle(OUT / "gpu-before.json"); self.launch(); self.business()
            time.sleep(10)
            store.require(self.backend_process.poll() == 0 and all(f["process"].poll() == 0 for f in self.fronts.values()),
                          "service restarted")
            result.update(status="PASS_PROJECT_TASK_MIGRATION_CONTINUATION", model_starts=1, gateway_starts=1,
                complete_responses=2, preflights=2, capabilities=2, completed_tasks=2, revoked_key_requests=1,
                killed_applications=0, real_tool_executions=0, duplicate_submissions=2, applied_migrations=2, duplicate_migrations=2)
        except BaseException as error:
            result.update(status="ENV_ABORT" if str(error).startswith("ENV_ABORT:") else "FAILED", error=repr(error))
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0); self.cleaning = True
            try:
                for child in self.applications:
                    if child["process"].poll() is None:
                        sup.signal_owner(child["owner"], signal.SIGTERM); child["process"].wait(5)
                for index, front in self.fronts.items():
                    if front["process"].poll() is None:
                        self.front_cli(index, f"front-{index}-cleanup-stop", "stop"); front["process"].wait(110)
                self.stop_backend(); a.require_idle(OUT / "gpu-final.json")
                store.require(a.lock_available(a.guard.common_lock()), "GPU lock retained")
                store.require(time.monotonic() - self.started <= LIMITS["seconds"], "phase deadline")
                store.require(self.gpu_started is not None and time.monotonic() - self.gpu_started <= GPU_WINDOW, "GPU window")
                pkg.save(OUT / "guard-archive.json", evidence.guard_archive(self.run_dir / "lease/events.jsonl",
                    pkg.read(self.run_dir / "lease/exit.json")))
                check(); result["cleanup_confirmed"] = True
            except BaseException as error:
                result.update(status="ENV_ABORT" if result["status"] == "ENV_ABORT" or
                    str(error).startswith("ENV_ABORT:") else "FAILED", cleanup_error=repr(error))
                raise
            finally:
                path = self.run_dir / "lease/exit.json"
                if path.exists() and pkg.read(path)["status"] in ("ENV_ABORT", "LEASE_BUSY", "RECOVERY_REQUIRED"):
                    result["status"] = "ENV_ABORT"
                for sig, handler in handlers.items(): signal.signal(sig, handler)
                for log in self.logs: log.close()
                result.update(counts=self.counts, seconds=time.monotonic() - self.started)
                pkg.save(OUT / "result.json", result)
        return result


if __name__ == "__main__":
    os.umask(0o077); configure()
    parser = argparse.ArgumentParser(); parser.add_argument("action", choices=("prepare", "check", "run"))
    args = parser.parse_args()
    if args.action == "prepare": value = prepare()
    elif args.action == "check": check(); value = dict(checked=True, started=False)
    else:
        plan = check(); store.require(not OUT.exists(), "preserve continuation result")
        with stage.sup.processes.child_subreaper(): value = Run(plan).execute()
    print(json.dumps(value))
