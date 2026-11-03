"""One model/TLS frontend: terminal archive, permanent keys and actual file-operation death."""
import argparse
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import engine, store, transport, retention as r, archive_cpu as cpu
from tools.project_tasks.live_worker import QUESTION
from tools.serve_retention import stage as retained
from tools.serve_clients import managed_stage as previous
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from tools.serve_soak import evidence

BASE = cpu.BASE
PLAN, OUT, TASKS = BASE / "plan.json", BASE / "execution", BASE / "tasks"
LIMITS = dict(starts=1, generations=4, negatives=5, observations=64, seconds=480)
GPU_WINDOW = 420
SOURCES = sorted(set((*cpu.SOURCES, "tools/project_tasks/archive_stage.py",
    "tools/project_tasks/archive_driver_cpu.py", "tools/project_tasks/archive_driver_fix.py",
    "tools/project_tasks/test_stage.py")))


def refused(result):
    # The public CLI deliberately hides exception text and all private values.
    store.require(result.returncode == 1 and not result.stdout and
        json.loads(result.stderr) == dict(error="ValueError", automatic_retry=False),
        "expected bounded CLI refusal")


def configure():
    retained.BASE, retained.PLAN, retained.OUT = BASE, PLAN, OUT
    retained.STORE = BASE / "owned-generations"
    retained.LIMITS, retained.GPU_WINDOW = LIMITS, GPU_WINDOW
    retained.configure()


def prepare():
    store.require(not PLAN.exists() and not OUT.exists() and not TASKS.exists(), "preserve earlier attempt")
    store.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT), "commit reviewed sources")
    tested = pkg.read(BASE / "cpu-01/result.json")
    store.require(not tested["passed"] and tested["tests_run"] == 69 and tested["failures"] == 1 and tested["errors"] == 0, "preserved first CPU gate")
    driver = pkg.read(BASE / "cpu-driver/result.json")
    store.require(not driver["passed"] and driver["tests_run"] == 5 and driver["errors"] == 1 and driver["failures"] == 0, "preserved driver fixture error")
    fixed = pkg.read(BASE / "cpu-driver-fix/result.json")
    store.require(fixed["passed"] and fixed["tests_run"] == 2, "focused driver gate")
    for result in (tested, driver, fixed):
        for name, checksum in result["sources"].items():
            if result is tested and name == "tools/project_tasks/test_archive.py": continue
            if result is driver and name in ("tools/project_tasks/archive_stage.py", "tools/project_tasks/archive_driver_cpu.py"): continue
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
        unit="unused-task-archive.service", token="srv11d-short", limits=LIMITS, gpu_window_seconds=GPU_WINDOW,
        default=old["default"], private=old["private"], days=days, profiles=profiles, bindings=bindings,
        store_policy=dict(max_bytes=128 * 1024**2, max_generations=2, max_age_days=14),
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        source_files={name: pkg.sha(ROOT / name) for name in SOURCES},
        cpu_sha256=pkg.sha(BASE / "cpu-01/result.json"), driver_sha256=pkg.sha(BASE / "cpu-driver/result.json"),
        driver_fix_sha256=pkg.sha(BASE / "cpu-driver-fix/result.json"),
        model_starts=1, gateway_starts=1, complete_responses=4, preflights=4, capabilities=2,
        actual_application_kills=2, real_tool_executions=2,
        task_policy=dict(max_tasks=1, max_keys=2, max_bytes=128 * 1024**2, min_retain_seconds=0),
        performance_acceptance=False))
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
    for path, key in ((BASE / "cpu-01/result.json", "cpu_sha256"), (BASE / "cpu-driver/result.json", "driver_sha256"),
                      (BASE / "cpu-driver-fix/result.json", "driver_fix_sha256")):
        store.require(pkg.sha(path) == plan[key], "CPU evidence changed")
    retained.r.binding("backend", plan["package"]["package"], plan["package"]["manifest_sha256"])
    retained.r.gateway.verify(plan["gateways"][0]["package"], plan["gateways"][0]["manifest_sha256"])
    return plan


class Run(retained.Run):
    def __init__(self, plan):
        super().__init__(plan)
        self.applications = []

    def profile(self, client):
        return self.plan["profiles"][client]["path"]

    def cli(self, client, action, key, label, expected=0, extra=()):
        return self.command(label, [sys.executable, "-B", ROOT / "tools/project_tasks/runner.py",
            action, "--root", TASKS, "--profile", self.profile(client), "--key", key, *extra], expected=expected, timeout=90)

    def submitted(self, client, key, tokens, label, expected=0):
        return self.cli(client, "submit", key, label, expected, extra=("--question", QUESTION,
            "--max-requests", "2", "--max-tools", "1", "--max-tokens", str(tokens)))

    def export(self, client, key, label):
        name = store.task_name(client, key)
        with store.open_task(TASKS, name, exclusive=False) as ledger:
            state = engine.replay(ledger)
            pkg.save(OUT / (label + "-ledger.json"), dict(meta=ledger.meta, events=ledger.events(),
                database_sha256=pkg.sha(ledger.path / "ledger.db")))
            return engine.public(ledger, state)

    def meta(self, action, label, extra=(), expected=0):
        return self.command(label, [sys.executable, "-B", ROOT / "tools/project_tasks/runner.py",
            action, "--root", TASKS, *extra], expected=expected)

    def prepare_operation(self, client, kind):
        plan = OUT / (client + "-" + kind + "-plan.json")
        value = json.loads(self.cli(client, "plan-" + kind, "same-key", client + "-" + kind + "-prepared",
            extra=("--plan", plan, "--reason", "explicit short acceptance " + kind)).stdout)
        return plan, value["plan_sha256"]

    def interrupted_operation(self, kind, point):
        plan, checksum = self.prepare_operation("alpha", kind)
        folder = OUT / (kind + "-kill"); folder.mkdir(mode=0o700)
        log = (folder / "child.log").open("xb"); self.logs.append(log)
        marker = folder / "boundary.json"
        process = subprocess.Popen(list(map(str, [sys.executable, "-B", ROOT / "tools/project_tasks/archive_worker.py",
            TASKS, "same-key", plan, checksum, marker, point])), cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, umask=0o077)
        owner = sup.process_identity(process.pid)
        self.applications.append(dict(process=process, owner=owner)); pkg.save(folder / "owner.json", owner)
        until = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < until:
            self.budget(); store.require(process.poll() is None, "retention worker exited before boundary")
            time.sleep(.02)
        store.require(marker.exists() and process.poll() is None and pkg.read(marker)["pid"] == process.pid,
                      "retention checkpoint owner/deadline")
        store.require(sup.signal_owner(owner, signal.SIGKILL), "retention worker identity changed")
        store.require(process.wait(5) == -signal.SIGKILL, "retention process death missing")
        pkg.save(folder / "killed.json", dict(owner=owner, returncode=process.returncode, signal="SIGKILL"))
        pending = json.loads(self.meta("retention-status", kind + "-pending").stdout)
        store.require(pending["state"] == "RETENTION_PENDING" and
            pending["operation_sha256"] == pkg.read(marker)["operation_sha256"], "interrupted operation identity")
        with r.catalog(TASKS / "retention") as db:
            pkg.save(folder / "pending-operation.json", r.get_record(db, "control", "operation"))
        self.budget("negatives")
        rejected = self.submitted("alpha", "same-key", 512, kind + "-pending-submit", expected=1)
        refused(rejected)
        extra = ("--operation-sha256", pending["operation_sha256"])
        finished = json.loads(self.meta("finish-retention", kind + "-finished", extra).stdout)
        again = json.loads(self.meta("finish-retention", kind + "-finish-repeat", extra).stdout)
        store.require(not finished["already_finished"] and again["already_finished"], "finish was not idempotent")
        repeated = json.loads(self.cli("alpha", "apply-" + kind, "same-key", kind + "-apply-repeat",
            extra=("--plan", plan, "--plan-sha256", checksum)).stdout)
        store.require(repeated["already_applied"] and not repeated["applied"], "completed plan repeated")

    def archived_duplicate(self, client, tokens, label, payload):
        submitted = json.loads(self.submitted(client, "same-key", tokens, label + "-submit", expected=2).stdout)
        resumed = json.loads(self.cli(client, "resume", "same-key", label + "-resume", expected=2).stdout)
        store.require(submitted == dict(resumed, created=False) and resumed["state"] == "ARCHIVED" and
            resumed["payload_state"] == payload and "answer" not in resumed, "archived duplicate executed or returned stale answer")

    def business(self):
        policy = self.plan["task_policy"]; store.init(TASKS, policy["max_tasks"])
        r.enable(TASKS, **{key: value for key, value in policy.items() if key != "max_tasks"})
        self.start_front(1)
        for client, tokens, count in (("alpha", 512, 2), ("beta", 64, 4)):
            value = json.loads(self.submitted(client, "same-key", tokens, client + "-completed").stdout)
            store.require(value.pop("created") and value["state"] == "COMPLETED" and value["outcome"] == "ANSWERED" and
                value["requests"] == value["preflight_checks"] == 2 and value["runs"] == 1 and
                value["saved_tool_results"] == value["tool_attempts"] == 1 and value["max_tokens"] == tokens and
                "Qwen4ExpForConditionalGeneration" in value["answer"] and "[docs/MODEL.md:L1-L24]" in value["answer"],
                "real client task budget/quality")
            store.require(self.export(client, "same-key", client) == value, "task reconstruction changed")
            name = store.task_name(client, "same-key"); source = TASKS / "tasks" / name
            saved = OUT / "saved-tasks" / name; saved.parent.mkdir(exist_ok=True); shutil.copytree(source, saved)
            identities = r.files(source)
            store.require(all(pkg.sha(saved / file) == row["sha256"] for file, row in identities.items()), "original task copy differs")
            pkg.save(OUT / (client + "-original-files.json"), identities)
            self.counts["generations"] = count; self.drain(client + "-completed-idle", count)
            if client == "alpha": self.interrupted_operation("archive", "payload_moved")
            else:
                plan, checksum = self.prepare_operation(client, "archive")
                self.cli(client, "apply-archive", "same-key", client + "-archive-applied",
                    extra=("--plan", plan, "--plan-sha256", checksum))
            archived = json.loads(self.cli(client, "inspect-archive", "same-key", client + "-archived-answer").stdout)
            store.require(archived["answer"] == value["answer"] and archived["terminal_revision"] == value["revision"],
                          "archive did not retain exact answer")
            self.archived_duplicate(client, tokens, client + "-retained", "retained")
            if client == "alpha":
                self.interrupted_operation("prune", "payload_file_removed")
                self.archived_duplicate(client, tokens, client + "-purged", "purged")
                self.budget("negatives")
                conflict = self.cli(client, "submit", "same-key", "alpha-question-conflict", expected=1,
                    extra=("--question", "different business question", "--max-requests", "2", "--max-tools", "1"))
                refused(conflict)
                self.budget("negatives")
                conflict = self.submitted(client, "same-key", 64, "alpha-budget-conflict", expected=1)
                refused(conflict)
                self.drain("alpha-purged-idle", 2)
        self.budget("negatives")
        full = self.submitted("alpha", "new-key", 512, "permanent-key-capacity", expected=1)
        refused(full)
        inventory = json.loads(self.meta("retention-status", "final-retention").stdout)
        store.require((inventory["active_tasks"], inventory["archived_keys"], inventory["retained_payloads"]) == (0, 2, 1),
                      "terminal slot/key/payload accounting")
        with r.catalog(TASKS / "retention") as db:
            pkg.save(OUT / "catalog-export.json", {table: dict(db.execute("SELECT name,value FROM " + table))
                for table in ("settings", "keys", "control")})
        values = self.drain("final", 4)
        store.require(values["q4t_structured_workers_started_total"] == values["q4t_structured_workers_reaped_total"] == 4,
                      "preflight or duplicate started grammar work")
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
            result.update(status="PASS_PROJECT_TASK_ARCHIVE_SHORT", model_starts=1, gateway_starts=1,
                complete_responses=4, preflights=4, capabilities=2, completed_tasks=2, archived_tasks=2,
                pruned_payloads=1, killed_applications=2, real_tool_executions=2,
                duplicate_submissions=3, duplicate_resumes=3, pending_refusals=2, conflicts=2, key_capacity_refusals=1)
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
