"""One managed model/TLS frontend: two client budgets and fresh recovery checks."""
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
from tools.project_tasks import engine, store, transport, budget_cpu as cpu
from tools.project_tasks.live_worker import QUESTION
from tools.serve_retention import stage as retained
from tools.serve_clients import managed_stage as previous
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from tools.serve_soak import evidence

BASE = cpu.BASE
PLAN, OUT, TASKS = BASE / "plan.json", BASE / "execution", BASE / "tasks"
LIMITS = dict(starts=1, generations=4, negatives=2, observations=64, seconds=480)
GPU_WINDOW = 420
SOURCES = sorted(set((*cpu.SOURCES, "tools/project_tasks/budget_stage.py",
    "tools/project_tasks/budget_worker.py", "tools/project_tasks/budget_driver_cpu.py")))


def configure():
    retained.BASE, retained.PLAN, retained.OUT = BASE, PLAN, OUT
    retained.STORE = BASE / "owned-generations"
    retained.LIMITS, retained.GPU_WINDOW = LIMITS, GPU_WINDOW
    retained.configure()


def prepare():
    store.require(not PLAN.exists() and not OUT.exists() and not TASKS.exists(), "preserve earlier attempt")
    store.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT), "commit reviewed sources")
    tested = pkg.read(BASE / "cpu-01/result.json")
    store.require(tested["passed"] and tested["tests_run"] == 31, "CPU gate")
    driver = pkg.read(BASE / "cpu-driver/result.json")
    store.require(driver["passed"] and driver["tests_run"] == 3, "driver gate")
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
        unit="unused-task-budget.service", token="srv11b-short", limits=LIMITS, gpu_window_seconds=GPU_WINDOW,
        default=old["default"], private=old["private"], days=days, profiles=profiles, bindings=bindings,
        store_policy=dict(max_bytes=128 * 1024**2, max_generations=2, max_age_days=14),
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        source_files={name: pkg.sha(ROOT / name) for name in SOURCES},
        cpu_sha256=pkg.sha(BASE / "cpu-01/result.json"), driver_sha256=pkg.sha(BASE / "cpu-driver/result.json"),
        model_starts=1, gateway_starts=1, complete_responses=4, preflights=6, capabilities=4,
        actual_application_kills=1, real_tool_executions=2, performance_acceptance=False))
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

    def negative(self):
        self.budget("negatives")
        output = OUT / "negative"; output.mkdir(mode=0o700)
        result = self.command("beta-above-limit", [sys.executable, "-B", ROOT / "tools/project_tasks/budget_worker.py",
            TASKS, self.profile("beta"), "negative", output])
        value = json.loads(result.stdout)
        store.require(value["state"] == "PREFLIGHT_REJECTED" and value["requests"] == value["tool_attempts"] == 0 and
            value["preflight_checks"] == 1 and value["preflight_error"] == dict(category="remote_error", http_status=400,
                code="client_output_limit_exceeded", param="max_tokens"), "budget rejection was not before generation")
        duplicate = json.loads(self.submitted("beta", "above-limit", 512, "beta-rejected-duplicate", expected=2).stdout)
        store.require(duplicate == dict(value, created=False), "rejected duplicate was resubmitted")
        self.drain("after-negative", 0)
        store.require(self.export("beta", "above-limit", "negative") == value, "negative ledger mismatch")

    def approved_death(self):
        folder = OUT / "approved"; folder.mkdir(mode=0o700)
        log = (folder / "child.log").open("xb"); self.logs.append(log)
        argv = [sys.executable, "-B", ROOT / "tools/project_tasks/budget_worker.py", TASKS,
                self.profile("alpha"), "alpha", folder]
        process = subprocess.Popen(list(map(str, argv)), cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, umask=0o077)
        owner = sup.process_identity(process.pid)
        self.applications.append(dict(process=process, owner=owner))
        pkg.save(folder / "owner.json", owner)
        marker = folder / "boundary.json"; until = time.monotonic() + 30
        while time.monotonic() < until and not marker.exists():
            self.budget(); store.require(process.poll() is None, "application exited before approval boundary")
            time.sleep(.02)
        store.require(marker.exists() and process.poll() is None and pkg.read(marker)["pid"] == process.pid,
                      "preflight boundary owner/deadline")
        active = json.loads(self.cli("alpha", "inspect", "same-key", "alpha-active", expected=2).stdout)
        store.require(active["state"] == "ACTIVE", "task writer lock absent")
        self.drain("approved-no-generation", 0)
        self.budget("negatives")
        store.require(sup.signal_owner(owner, signal.SIGKILL), "application identity changed")
        store.require(process.wait(5) == -signal.SIGKILL, "application death missing")
        pkg.save(folder / "killed.json", dict(owner=owner, returncode=process.returncode, signal="SIGKILL"))
        value = json.loads(self.cli("alpha", "inspect", "same-key", "alpha-stopped", expected=2).stdout)
        store.require(value["state"] == "PREFLIGHT_APPROVED" and value["requests"] == 0 and value["preflight_checks"] == 1,
                      "approval was submitted or lost")

    def business(self):
        store.init(TASKS, max_tasks=3); self.start_front(1)
        self.negative(); self.approved_death()
        alpha = json.loads(self.cli("alpha", "resume", "same-key", "alpha-completed").stdout)
        self.drain("alpha-idle", 2)
        beta = json.loads(self.submitted("beta", "same-key", 64, "beta-completed").stdout)
        store.require(beta.pop("created") is True, "beta task collided with alpha")
        self.drain("beta-idle", 4)
        for client, tokens, value in (("alpha", 512, alpha), ("beta", 64, beta)):
            store.require(value["state"] == "COMPLETED" and value["outcome"] == "ANSWERED" and
                value["requests"] == 2 and value["saved_tool_results"] == value["tool_attempts"] == 1 and
                value["max_tokens"] == tokens and value["preflight_checks"] == (3 if client == "alpha" else 2) and
                "Qwen4ExpForConditionalGeneration" in value["answer"] and "[docs/MODEL.md:L1-L24]" in value["answer"],
                "real client task budget/quality")
            duplicate = json.loads(self.submitted(client, "same-key", tokens, client + "-duplicate").stdout)
            store.require(duplicate == dict(value, created=False), "completed duplicate executed")
            store.require(self.export(client, "same-key", client) == value, "task reconstruction changed")
        store.require(alpha["task_id"] != beta["task_id"], "client task keys not isolated")
        self.counts["generations"] = 4
        values = self.drain("final", 4)
        store.require(values["q4t_structured_workers_started_total"] == values["q4t_structured_workers_reaped_total"] == 4,
                      "preflight created a grammar worker or worker not reaped")
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
            result.update(status="PASS_PROJECT_TASK_BUDGET_SHORT", model_starts=1, gateway_starts=1,
                complete_responses=4, preflights=6, capabilities=4, completed_tasks=2, rejected_tasks=1,
                killed_applications=1, real_tool_executions=2, duplicate_submissions=3)
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
