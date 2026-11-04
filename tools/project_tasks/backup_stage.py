"""One managed model/TLS frontend: consistent snapshot and single-active task-store handoff."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import engine, store, transport, backup as b, retention as r, backup_cpu as cpu
from tools.project_tasks.live_worker import QUESTION
from tools.serve_retention import stage as retained
from tools.serve_clients import managed_stage as previous
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from tools.serve_soak import evidence

BASE = cpu.BASE
PLAN, OUT, TASKS = BASE / "plan.json", BASE / "execution", BASE / "restored-tasks"
SOURCE = BASE / "source-tasks"
SNAPSHOT = BASE / "snapshot"
LIMITS = dict(starts=1, generations=4, negatives=2, observations=64, seconds=480)
GPU_WINDOW = 420
SOURCES = sorted(set((*cpu.SOURCES, "tools/project_tasks/backup_stage.py", "tools/project_tasks/backup_live_worker.py",
    "tools/project_tasks/backup_driver_cpu.py", "tools/project_tasks/test_stage.py",
    "docs/SERVE_TASK_BACKUP_SHORT_2026-11-04.md")))


def configure():
    retained.BASE, retained.PLAN, retained.OUT = BASE, PLAN, OUT
    retained.STORE = BASE / "owned-generations"
    retained.LIMITS, retained.GPU_WINDOW = LIMITS, GPU_WINDOW
    retained.configure()


def prepare():
    store.require(not PLAN.exists() and not OUT.exists() and not TASKS.exists() and not SOURCE.exists() and not SNAPSHOT.exists(), "preserve earlier attempt")
    store.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT), "commit reviewed sources")
    tested = pkg.read(BASE / "cpu-01/result.json")
    store.require(tested["passed"] and tested["tests_run"] == 88, "CPU gate")
    driver = pkg.read(BASE / "cpu-driver/result.json")
    store.require(driver["passed"] and driver["tests_run"] == 4, "driver gate")
    for result in (tested, driver):
        for name, checksum in result["sources"].items():
            store.require(pkg.sha(ROOT / name) == checksum, "tested source changed: " + name)
    previous.configure("day"); old = previous.check()
    store.require(not previous.OUT.exists(), "operator day executed; preserve its evidence")
    days = {**old["old_day_plans"], str(previous.PLAN): pkg.sha(previous.PLAN)}
    configure()
    private = BASE / "private"; private.mkdir(mode=0o700)
    policy = pkg.read(previous.BASE / "model-private/policy-private.json")
    store.require([row["id"] for row in policy["clients"]] == ["alpha", "beta"], "fixed client identities")
    protocol = pkg.read(ROOT / "build/q4t.release.json")["chat_protocol"]
    profiles, bindings = {}, {}
    for row in policy["clients"]:
        path = private / (row["id"] + ".json")
        value = dict(client_id=row["id"], api_key=row["api_key"], connect_host="127.0.0.1",
            base_url=f'https://{policy["server_name"]}:{policy["listen_port"]}/v1',
            ca_file=str(Path(old["gateway"]["package"]) / "certificate.pem"), model="qwen3.8-flash-next",
            protocol_sha256=protocol["protocol_sha256"], template_sha256=protocol["template"]["reference_sha256"])
        store.write_new(path, store.encode(value))
        _, bindings[row["id"]] = transport.profile(path)
        profiles[row["id"]] = dict(path=str(path), sha256=pkg.sha(path))
    pkg.save(PLAN, dict(package=old["package"], gateway=old["gateway"], gateways=[old["gateways"][0]],
        unit="unused-task-backup.service", token="srv11e-short", limits=LIMITS, gpu_window_seconds=GPU_WINDOW,
        default=old["default"], private=old["private"], days=days, profiles=profiles, bindings=bindings,
        store_policy=dict(max_bytes=128 * 1024**2, max_generations=2, max_age_days=14),
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        source_files={name: pkg.sha(ROOT / name) for name in SOURCES},
        cpu_sha256=pkg.sha(BASE / "cpu-01/result.json"), driver_sha256=pkg.sha(BASE / "cpu-driver/result.json"),
        model_starts=1, gateway_starts=1, complete_responses=4, preflights=5, capabilities=4,
        actual_application_kills=4, real_tool_executions=2, ledger_request_intents=5,
        task_policy=dict(max_tasks=3, max_keys=3, max_bytes=128 * 1024**2, min_retain_seconds=0),
        snapshot_max_bytes=2 * 1024**3, performance_acceptance=False))
    check()
    return dict(prepared=True, models_started=0, listeners_started=0)


def check():
    plan = pkg.read(PLAN)
    store.require(plan["limits"] == LIMITS and plan["gpu_window_seconds"] == GPU_WINDOW, "frozen envelope changed")
    for name, checksum in plan["source_files"].items():
        store.require(pkg.sha(ROOT / name) == checksum, "frozen source changed: " + name)
    for name, checksum in {**plan["default"], **plan["private"], **plan["days"]}.items():
        store.require(pkg.sha(ROOT / name) == checksum, "fixed identity changed")
    for name in plan["days"]:
        store.require(not (Path(name).parent / "execution").exists(), "user day executed")
    for name, row in plan["profiles"].items():
        store.require(pkg.sha(Path(row["path"])) == row["sha256"] and
            transport.profile(row["path"])[1] == plan["bindings"][name], "private client binding changed")
    for path, key in ((BASE / "cpu-01/result.json", "cpu_sha256"), (BASE / "cpu-driver/result.json", "driver_sha256")):
        store.require(pkg.sha(path) == plan[key], "CPU evidence changed")
    retained.r.binding("backend", plan["package"]["package"], plan["package"]["manifest_sha256"])
    retained.r.gateway.verify(plan["gateways"][0]["package"], plan["gateways"][0]["manifest_sha256"])
    return plan


class Run(retained.Run):
    def __init__(self, plan):
        super().__init__(plan)
        self.applications = []
        self.task_root = SOURCE

    def profile(self, client):
        return self.plan["profiles"][client]["path"]

    def cli(self, client, action, key, label, expected=0, extra=()):
        return self.command(label, [sys.executable, "-B", ROOT / "tools/project_tasks/runner.py",
            action, "--root", self.task_root, "--profile", self.profile(client), "--key", key, *extra], expected=expected, timeout=90)

    def submitted(self, client, key, tokens, label, expected=0):
        return self.cli(client, "submit", key, label, expected, extra=("--question", QUESTION,
            "--max-requests", "2", "--max-tools", "1", "--max-tokens", str(tokens)))

    def export(self, client, key, label):
        name = store.task_name(client, key)
        with store.open_task(self.task_root, name, exclusive=False) as ledger:
            state = engine.replay(ledger)
            pkg.save(OUT / (label + "-ledger.json"), dict(meta=ledger.meta, events=ledger.events(),
                database_sha256=pkg.sha(ledger.path / "ledger.db")))
            return engine.public(ledger, state)

    def backup_cli(self, action, label, extra):
        return self.command(label, [sys.executable, "-B", ROOT / "tools/project_tasks/backup.py", action, *extra], timeout=90)

    def kill_worker(self, label, argv, point):
        folder = OUT / label; folder.mkdir(mode=0o700)
        marker = folder / "boundary.json"
        command = argv(folder, marker)
        log = (folder / "child.log").open("xb"); self.logs.append(log)
        child = subprocess.Popen(list(map(str, command)), cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, umask=0o077)
        owner = sup.process_identity(child.pid); self.applications.append(dict(process=child, owner=owner))
        pkg.save(folder / "owner.json", owner)
        until = time.monotonic() + 45
        while not marker.exists() and time.monotonic() < until:
            self.budget(); store.require(child.poll() is None, "worker exited before boundary: " + label); time.sleep(.02)
        store.require(marker.exists() and child.poll() is None and pkg.read(marker)["pid"] == child.pid and
            pkg.read(marker)["kind"] == point, "worker boundary owner/deadline")
        store.require(sup.signal_owner(owner, signal.SIGKILL), "worker identity changed")
        store.require(child.wait(5) == -signal.SIGKILL, "worker death missing")
        pkg.save(folder / "killed.json", dict(owner=owner, returncode=child.returncode, signal="SIGKILL"))

    def application_death(self, case, client, point):
        self.kill_worker(case + "-kill", lambda folder, marker: [sys.executable, "-B",
            ROOT / "tools/project_tasks/backup_live_worker.py", self.task_root, self.profile(client), case, folder], point)
        return self.export(client, case, case + "-before")

    def manage_payload(self, kind, label):
        path = OUT / (label + "-plan.json")
        plan = json.loads(self.cli("beta", "plan-" + kind, "archived", label + "-prepared",
            extra=("--plan", path, "--reason", "explicit short handoff " + kind)).stdout)
        return json.loads(self.cli("beta", "apply-" + kind, "archived", label + "-applied",
            extra=("--plan", path, "--plan-sha256", plan["plan_sha256"])).stdout)

    def archived_duplicate(self, label):
        value = json.loads(self.submitted("beta", "archived", 64, label, expected=2).stdout)
        store.require(value["state"] == "ARCHIVED" and not value["created"] and "answer" not in value,
                      "archived duplicate was executed")
        return value

    def business(self):
        policy = self.plan["task_policy"]; store.init(SOURCE, policy["max_tasks"])
        r.enable(SOURCE, **{key: value for key, value in policy.items() if key != "max_tasks"})
        self.start_front(1)
        known = self.application_death("known", "alpha", "tool_result")
        store.require(known["requests"] == known["tool_attempts"] == known["saved_tool_results"] == 1, "known result missing")
        unknown = self.application_death("unknown", "beta", "request")
        store.require(unknown["state"] == "REQUEST_UNKNOWN" and unknown["requests"] == unknown["preflight_checks"] == 1 and
            unknown["tool_attempts"] == 0, "request intent was not unknown")
        archived = json.loads(self.submitted("beta", "archived", 64, "beta-completed").stdout)
        store.require(archived.pop("created") and archived["state"] == "COMPLETED" and self.export("beta", "archived", "beta-original") == archived,
                      "beta task not completed")
        self.manage_payload("archive", "source-archive")
        self.counts["generations"] = 3; self.drain("before-copy", 3)
        self.kill_worker("snapshot-kill", lambda folder, marker: [sys.executable, "-B", ROOT / "tools/project_tasks/backup_worker.py",
            "backup", SOURCE, SNAPSHOT, marker, "before_seal"], "before_seal")
        snapshot = json.loads(self.backup_cli("backup", "snapshot-completed", ("--root", SOURCE, "--snapshot", SNAPSHOT,
            "--max-bytes", str(self.plan["snapshot_max_bytes"]))).stdout)
        plan_path = OUT / "restore-plan.json"
        planned = json.loads(self.backup_cli("plan-restore", "restore-prepared", ("--root", SOURCE, "--snapshot", SNAPSHOT,
            "--snapshot-sha256", snapshot["snapshot_sha256"], "--destination", TASKS, "--plan", plan_path,
            "--reason", "same-host verified task-store handoff")).stdout)
        self.kill_worker("restore-kill", lambda folder, marker: [sys.executable, "-B", ROOT / "tools/project_tasks/backup_worker.py",
            "restore", plan_path, planned["plan_sha256"], marker, "source_fenced"], "source_fenced")
        for action, key, label, extra in (("resume", "known", "retired-source-resume", ()),
            ("submit", "new-key", "retired-source-submit", ("--question", QUESTION, "--max-requests", "2", "--max-tools", "1"))):
            self.budget("negatives")
            refused = self.cli("alpha", action, key, label, expected=1, extra=extra)
            store.require(not refused.stdout and json.loads(refused.stderr) == dict(error="ValueError", automatic_retry=False), "source fence did not refuse")
        applied = json.loads(self.backup_cli("apply-restore", "restore-completed", ("--plan", plan_path,
            "--plan-sha256", planned["plan_sha256"])).stdout)
        store.require(applied["source_retired"] and not applied["already_applied"] and not (SOURCE / "tasks").samefile(TASKS / "tasks"), "restore did not publish a new store")
        self.task_root = TASKS
        for client, key, before in (("alpha", "known", known), ("beta", "unknown", unknown)):
            store.require(self.export(client, key, key + "-restored") == before, "restore changed task state")
        unchanged = json.loads(self.cli("beta", "resume", "unknown", "unknown-held", expected=2).stdout)
        store.require(unchanged == unknown, "unknown request was replayed")
        self.drain("after-unknown", 3)
        final = json.loads(self.cli("alpha", "resume", "known", "alpha-completed").stdout)
        self.counts["generations"] = 4
        for value in (final, archived):
            store.require(value["state"] == "COMPLETED" and value["outcome"] == "ANSWERED" and value["requests"] == 2 and
                value["tool_attempts"] == value["saved_tool_results"] == 1 and
                "Qwen4ExpForConditionalGeneration" in value["answer"] and "[docs/MODEL.md:L1-L24]" in value["answer"], "actual answer quality")
        store.require(self.export("alpha", "known", "alpha-final") == final, "final ledger differs")
        duplicate = json.loads(self.submitted("alpha", "known", 512, "alpha-duplicate").stdout)
        store.require(duplicate == dict(final, created=False), "completed duplicate executed")
        answer = json.loads(self.cli("beta", "inspect-archive", "archived", "archived-answer").stdout)
        store.require(answer["answer"] == archived["answer"], "archived answer lost on handoff")
        store.require(self.archived_duplicate("archived-retained-duplicate")["payload_state"] == "retained", "payload missing")
        self.manage_payload("prune", "target-prune")
        store.require(self.archived_duplicate("archived-purged-duplicate")["payload_state"] == "purged", "payload not pruned")
        resumed = json.loads(self.cli("beta", "resume", "archived", "archived-purged-resume", expected=2).stdout)
        store.require(resumed["state"] == "ARCHIVED" and resumed["payload_state"] == "purged", "archived resume executed")
        duplicate = json.loads(self.submitted("beta", "unknown", 64, "unknown-duplicate", expected=2).stdout)
        store.require(duplicate == dict(unknown, created=False), "unknown duplicate executed")
        before_repeat = b.regular_tree(TASKS)
        repeated = json.loads(self.backup_cli("apply-restore", "restore-repeat", ("--plan", plan_path,
            "--plan-sha256", planned["plan_sha256"])).stdout)
        store.require(repeated["already_applied"] and b.regular_tree(TASKS) == before_repeat, "repeat restore rolled back new work")
        pkg.save(OUT / "final-retention.json", r.status(TASKS))
        pkg.save(OUT / "source-final-tree.json", b.regular_tree(SOURCE))
        pkg.save(OUT / "target-final-tree.json", b.regular_tree(TASKS))
        values = self.drain("final", 4)
        store.require(values["q4t_structured_workers_started_total"] == values["q4t_structured_workers_reaped_total"] == 4, "grammar count or duplicate generation")
        pkg.save(OUT / "final-metrics.json", values)
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
            result.update(status="PASS_PROJECT_TASK_BACKUP_SHORT", model_starts=1, gateway_starts=1,
                complete_responses=4, preflights=5, capabilities=4, completed_tasks=2, unknown_tasks=1,
                ledger_request_intents=5, killed_applications=4, real_tool_executions=2, duplicate_submissions=4,
                nonexecuting_resumes=2, retired_source_refusals=2, repeated_restores=1)
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
        plan = check(); store.require(not OUT.exists(), "preserve previous run")
        with sup.processes.child_subreaper(): value = Run(plan).execute()
    print(json.dumps(value))
