"""One managed model/TLS frontend: stable command errors and unchanged execution boundaries."""
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
from tools.project_tasks import engine, store, transport, errors, retention as r, error_cpu as cpu
from tools.project_tasks.live_worker import QUESTION
from tools.serve_retention import stage as retained
from tools.serve_clients import managed_stage as previous
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from tools.serve_soak import evidence

BASE = cpu.BASE
PLAN, OUT, TASKS = BASE / "plan.json", BASE / "execution", BASE / "tasks"
LIMITS = dict(starts=1, generations=4, negatives=6, observations=64, seconds=480)
GPU_WINDOW = 420
SOURCES = sorted(set((*cpu.SOURCES, "tools/project_tasks/error_stage.py", "tools/project_tasks/error_report.py",
    "tools/project_tasks/error_cpu_fix.py",
    "tools/project_tasks/error_driver_cpu.py", "tools/project_tasks/test_stage.py",
    "docs/SERVE_TASK_ERRORS_2026-11-04.md")))


def configure():
    retained.BASE, retained.PLAN, retained.OUT = BASE, PLAN, OUT
    retained.STORE = BASE / "owned-generations"
    retained.LIMITS, retained.GPU_WINDOW = LIMITS, GPU_WINDOW
    retained.configure()


def prepare():
    store.require(not PLAN.exists() and not OUT.exists() and not TASKS.exists(), "preserve earlier attempt")
    store.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT), "commit reviewed sources")
    tested = pkg.read(BASE / "cpu-01/result.json")
    store.require(not tested["passed"] and tested["tests_run"] == 106 and
        tested["errors"] == 18 and tested["failures"] == 0, "preserved fixture failure")
    corrected = pkg.read(BASE / "cpu-fix-01/result.json")
    store.require(corrected["passed"] and corrected["tests_run"] == 18, "corrected CPU gate")
    for name, checksum in tested["sources"].items():
        if name != "tools/project_tasks/test_errors.py":
            store.require(pkg.sha(ROOT / name) == checksum, "unchanged 88-case evidence")
    driver = pkg.read(BASE / "cpu-driver/result.json")
    store.require(driver["passed"] and driver["tests_run"] == 2, "driver gate")
    for result in (corrected, driver):
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
    import secrets
    denied = private / "denied.json"
    value = pkg.read(Path(profiles["beta"]["path"]))
    value["api_key"] = secrets.token_urlsafe(36)
    store.write_new(denied, store.encode(value))
    _, bindings["denied"] = transport.profile(denied)
    profiles["denied"] = dict(path=str(denied), sha256=pkg.sha(denied))
    pkg.save(PLAN, dict(package=old["package"], gateway=old["gateway"], gateways=[old["gateways"][0]],
        unit="unused-task-errors.service", token="srv11f-short", limits=LIMITS, gpu_window_seconds=GPU_WINDOW,
        default=old["default"], private=old["private"], days=days, profiles=profiles, bindings=bindings,
        store_policy=dict(max_bytes=128 * 1024**2, max_generations=2, max_age_days=14),
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        source_files={name: pkg.sha(ROOT / name) for name in SOURCES},
        cpu_sha256=pkg.sha(BASE / "cpu-01/result.json"), fix_sha256=pkg.sha(BASE / "cpu-fix-01/result.json"), driver_sha256=pkg.sha(BASE / "cpu-driver/result.json"),
        model_starts=1, gateway_starts=1, complete_responses=4, preflights=4, capabilities=3,
        actual_application_kills=0, real_tool_executions=2, ledger_request_intents=4,
        task_policy=dict(max_tasks=3, max_keys=3, max_bytes=128 * 1024**2, min_retain_seconds=0),
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
    for path, key in ((BASE / "cpu-01/result.json", "cpu_sha256"), (BASE / "cpu-fix-01/result.json", "fix_sha256"), (BASE / "cpu-driver/result.json", "driver_sha256")):
        store.require(pkg.sha(path) == plan[key], "CPU evidence changed")
    retained.r.binding("backend", plan["package"]["package"], plan["package"]["manifest_sha256"])
    retained.r.gateway.verify(plan["gateways"][0]["package"], plan["gateways"][0]["manifest_sha256"])
    return plan


class Run(retained.Run):
    def __init__(self, plan):
        super().__init__(plan)
        self.applications = []
        self.task_root = TASKS

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

    def refused(self, client, action, key, label, code, extra=()):
        self.budget("negatives")
        command = self.cli(client, action, key, label, expected=1, extra=extra)
        value = json.loads(command.stderr)
        store.require(not command.stdout and value["profile"] == errors.PROFILE and
            value["code"] == code and value["operation"] == action and value["automatic_retry"] is False,
            "public command error contract")
        return value

    def business(self):
        policy = self.plan["task_policy"]; store.init(TASKS, policy["max_tasks"])
        r.enable(TASKS, **{key: value for key, value in policy.items() if key != "max_tasks"})
        self.start_front(1)
        for client, tokens in (("alpha", 512), ("beta", 64)):
            value = json.loads(self.submitted(client, "same", tokens, client + "-completed").stdout)
            store.require(value.pop("created") is True and value["state"] == "COMPLETED" and
                value["outcome"] == "ANSWERED" and value["requests"] == 2 and
                value["tool_attempts"] == value["saved_tool_results"] == 1 and
                "Qwen4ExpForConditionalGeneration" in value["answer"] and
                "[docs/MODEL.md:L1-L24]" in value["answer"], "actual tool/answer quality")
            store.require(self.export(client, "same", client + "-ledger") == value, "actual SQLite state")
            duplicate = json.loads(self.submitted(client, "same", tokens, client + "-duplicate").stdout)
            store.require(duplicate == dict(value, created=False), "duplicate executed")
            resumed = json.loads(self.cli(client, "resume", "same", client + "-resume").stdout)
            store.require(resumed == value, "terminal resume executed")
        self.counts["generations"] = 4
        self.refused("alpha", "submit", "same", "key-conflict", "key_conflict", ("--question", "different"))
        self.refused("alpha", "resume", "same", "invalid-control", "invalid_arguments", ("--decision", "abandon"))
        denied = self.refused("denied", "submit", "unauth", "unauthorized", "remote_auth",
            ("--question", QUESTION, "--max-requests", "2", "--max-tools", "1", "--max-tokens", "64"))
        store.require(denied["http_status"] == 401, "real gateway authentication status")
        state = self.export("denied", "unauth", "denied-ledger")
        store.require(state["state"] == "READY" and state["runs"] == 1 and state["requests"] ==
            state["preflight_checks"] == state["tool_attempts"] == 0, "auth error submitted model work")
        duplicate = json.loads(self.submitted("denied", "unauth", 64, "denied-duplicate", expected=2).stdout)
        store.require(duplicate == dict(state, created=False), "failed auth automatically retried")
        self.refused("beta", "resume", "unauth", "binding-conflict", "binding_changed")
        self.refused("alpha", "submit", "overflow", "capacity", "task_capacity", ("--question", QUESTION))
        plan_path = OUT / "archive-plan.json"
        planned = json.loads(self.cli("alpha", "plan-archive", "same", "archive-prepared",
            extra=("--plan", plan_path, "--reason", "review exact error contract")).stdout)
        store.require(planned["prepared"], "metadata plan not prepared")
        self.refused("alpha", "apply-archive", "same", "changed-plan", "plan_changed",
            ("--plan", plan_path, "--plan-sha256", "0" * 64))
        pkg.save(OUT / "final-retention.json", r.status(TASKS))
        values = self.drain("final", 4)
        store.require(values["q4t_structured_workers_started_total"] ==
            values["q4t_structured_workers_reaped_total"] == 4, "extra generation/grammar worker")
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
            result.update(status="PASS_PROJECT_TASK_ERRORS_SHORT", model_starts=1, gateway_starts=1,
                complete_responses=4, preflights=4, capabilities=3, completed_tasks=2, ready_tasks=1,
                ledger_request_intents=4, killed_applications=0, real_tool_executions=2, duplicate_submissions=3,
                nonexecuting_resumes=2, classified_errors=6)
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
