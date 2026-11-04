"""One managed model/TLS frontend, three interrupted application tasks; bounded."""
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
from tools.project_tasks import engine, store, runner, transport, cpu
from tools.project_tasks.live_worker import QUESTION, CASES
from tools.serve_retention import stage as retained
from tools.serve_clients import managed_stage as previous
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from tools.serve_soak import evidence

BASE = cpu.BASE
PLAN, OUT = BASE / "plan.json", BASE / "execution"
TASKS, PROFILE = BASE / "tasks", BASE / "private/client.json"
LIMITS = dict(starts=1, generations=7, negatives=3, observations=64, seconds=480)
GPU_WINDOW = 420
SOURCES = sorted(set((*retained.SOURCES, *cpu.SOURCES,
    "tools/project_tasks/stage.py", "tools/project_tasks/live_worker.py",
    "tools/project_tasks/test_stage.py", "tools/project_tasks/driver_cpu.py")))


def configure():
    retained.BASE, retained.PLAN, retained.OUT = BASE, PLAN, OUT
    retained.STORE = BASE / "owned-generations"
    retained.LIMITS, retained.GPU_WINDOW = LIMITS, GPU_WINDOW
    retained.configure()


def prepare():
    store.require(not PLAN.exists() and not OUT.exists() and not TASKS.exists(), "preserve prior phase")
    store.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
                  "commit reviewed sources before model freeze")
    tested = pkg.read(BASE / "cpu-wire/result.json")
    store.require(tested["passed"] and tested["effective_tests"] == 19, "CPU gate")
    for name, checksum in tested["sources"].items():
        store.require(pkg.sha(ROOT / name) == checksum, "CPU source changed: " + name)
    driver = pkg.read(BASE / "cpu-driver/result.json")
    store.require(driver["passed"], "driver CPU gate")
    for name, checksum in driver["sources"].items():
        store.require(pkg.sha(ROOT / name) == checksum, "driver source changed")
    previous.configure("day"); old = previous.check()
    store.require(not previous.OUT.exists(), "operator day has executed; preserve its runtime")
    days = {**old["old_day_plans"], str(previous.PLAN): pkg.sha(previous.PLAN)}
    configure()
    PROFILE.parent.mkdir(mode=0o700)
    policy = pkg.read(previous.BASE / "model-private/policy-private.json")
    protocol = pkg.read(ROOT / "build/q4t.release.json")["chat_protocol"]
    value = dict(client_id=policy["clients"][0]["id"],
        base_url=f'https://{policy["server_name"]}:{policy["listen_port"]}/v1',
        connect_host="127.0.0.1", api_key=policy["clients"][0]["api_key"],
        ca_file=str(Path(old["gateway"]["package"]) / "certificate.pem"),
        model="qwen3.8-flash-next", protocol_sha256=protocol["protocol_sha256"],
        template_sha256=protocol["template"]["reference_sha256"])
    store.write_new(PROFILE, store.encode(value))
    _, binding = transport.profile(PROFILE)
    pkg.save(PLAN, dict(package=old["package"], gateway=old["gateway"],
        gateways=[old["gateways"][0]], unit="unused-project-tasks.service", token="srv11-short",
        limits=LIMITS, gpu_window_seconds=GPU_WINDOW, default=old["default"], private=old["private"],
        days=days, profile_sha256=pkg.sha(PROFILE), binding=binding, cases=CASES,
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        source_files={name: pkg.sha(ROOT / name) for name in SOURCES},
        cpu_sha256=pkg.sha(BASE / "cpu-wire/result.json"), driver_sha256=pkg.sha(BASE / "cpu-driver/result.json"),
        store_policy=dict(max_bytes=128 * 1024**2, max_generations=2, max_age_days=14),
        model_starts=1, gateway_starts=1, application_kills=3, successful_complete_responses=6,
        client_incomplete_responses=1, max_tool_attempts=4, performance_acceptance=False))
    check()
    return dict(prepared=True, models_started=0, listeners_started=0)


def check():
    plan = pkg.read(PLAN)
    store.require(plan["limits"] == LIMITS and plan["gpu_window_seconds"] == GPU_WINDOW and
                  plan["cases"] == {k: list(v) for k, v in CASES.items()}, "frozen budget changed")
    for name, checksum in plan["source_files"].items():
        store.require(pkg.sha(ROOT / name) == checksum, "frozen source changed: " + name)
    for name, checksum in {**plan["default"], **plan["private"], **plan["days"]}.items():
        store.require(pkg.sha(ROOT / name) == checksum, "fixed identity changed")
    for name in plan["days"]:
        store.require(not (Path(name).parent / "execution").exists(), "user day executed")
    for path, key in ((PROFILE, "profile_sha256"), (BASE / "cpu-wire/result.json", "cpu_sha256"),
                      (BASE / "cpu-driver/result.json", "driver_sha256")):
        store.require(pkg.sha(path) == plan[key], "frozen evidence changed")
    retained.r.binding("backend", plan["package"]["package"], plan["package"]["manifest_sha256"])
    retained.r.gateway.verify(plan["gateways"][0]["package"], plan["gateways"][0]["manifest_sha256"])
    return plan


class Run(retained.Run):
    def __init__(self, plan):
        super().__init__(plan)
        self.applications = []

    def app_cli(self, case, action, label, expected=0, extra=()):
        return self.command(label, [sys.executable, "-B", ROOT / "tools/project_tasks/runner.py",
            action, "--root", TASKS, "--profile", PROFILE, "--key", case, *extra],
            expected=expected, timeout=90)

    def idle(self, label, expected):
        for number in range(40):
            values = self.metrics(label + f"-{number:02d}")
            if all(values[key] == 0 for key in a.EMPTY):
                store.require(values["q4t_requests_total"] == expected and
                    values["q4t_structured_workers_started_total"] ==
                    values["q4t_structured_workers_reaped_total"], "unexpected request/grammar count")
                return values
            time.sleep(.05)
        raise RuntimeError("application request did not drain")

    def interrupted_task(self, case):
        folder = OUT / case; folder.mkdir(mode=0o700)
        log = (folder / "child.log").open("xb"); self.logs.append(log)
        command = [sys.executable, "-B", ROOT / "tools/project_tasks/live_worker.py",
                   TASKS, PROFILE, case, folder]
        process = subprocess.Popen(list(map(str, command)), cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, umask=0o077)
        owner = sup.process_identity(process.pid)
        self.applications.append(dict(process=process, owner=owner))
        pkg.save(folder / "owner.json", owner)
        until = time.monotonic() + 90
        marker = folder / "boundary.json"
        while time.monotonic() < until and not marker.exists():
            self.budget()
            store.require(process.poll() is None, "application stopped before fault boundary: " + case)
            time.sleep(.02)
        store.require(marker.exists() and process.poll() is None, "application boundary deadline")
        store.require(pkg.read(marker)["pid"] == process.pid, "application boundary owner")
        self.app_cli(case, "inspect", case + "-active", expected=2)
        store.require(json.loads(pkg.read(OUT / (case + "-active.json"))["stdout"])["state"] == "ACTIVE", "task lock absent")
        self.budget("negatives")
        store.require(sup.signal_owner(owner, signal.SIGKILL), "application identity disappeared")
        store.require(process.wait(5) == -signal.SIGKILL, "application death not observed")
        pkg.save(folder / "killed.json", dict(owner=owner, signal="SIGKILL", returncode=process.returncode))
        state = json.loads(self.app_cli(case, "inspect", case + "-stopped", expected=2).stdout)
        expected = {"known": "TOOLS_PENDING", "unknown": "TOOL_UNKNOWN", "partial": "REQUEST_UNKNOWN"}[case]
        store.require(state["state"] == expected, "wrong recovered state")
        if case != "known":
            result = json.loads(self.app_cli(case, "resume", case + "-blocked", expected=2).stdout)
            store.require(result == state, "unknown resume made progress")
            decision = "repeat-read" if case == "unknown" else "discard-request"
            self.app_cli(case, "resolve", case + "-resolve", expected=2,
                extra=("--decision", decision, "--revision", state["revision"],
                       "--reason", "explicit bounded application recovery validation"))
        return state

    def business(self):
        store.init(TASKS, max_tasks=3)
        self.start_front(1)
        expected = 0
        for case in CASES:
            self.interrupted_task(case)
            expected += 1
            self.idle(case + "-drained", expected)
            result = json.loads(self.app_cli(case, "resume", case + "-completed").stdout)
            store.require(result["state"] == "COMPLETED" and result["outcome"] == "ANSWERED" and
                "Qwen4ExpForConditionalGeneration" in result["answer"] and
                "[docs/MODEL.md:L1-L24]" in result["answer"], "real task quality")
            expected += 2 if case == "partial" else 1
            self.idle(case + "-final-idle", expected)
            requests, tools, _ = CASES[case]
            duplicate = json.loads(self.app_cli(case, "submit", case + "-duplicate", extra=(
                "--question", QUESTION, "--max-requests", str(requests), "--max-tools", str(tools))).stdout)
            store.require(duplicate == dict(result, created=False), "duplicate task changed/ran")
            with store.open_task(TASKS, result["task_id"], exclusive=False) as ledger:
                events = ledger.events()
                pkg.save(OUT / (case + "-ledger.json"), dict(meta=ledger.meta, events=events,
                    database_sha256=pkg.sha(ledger.path / "ledger.db")))
            store.require(result["requests"] == requests and result["tool_attempts"] == tools and
                result["saved_tool_results"] == 1 and result["read_repetitions"] == (case == "unknown") and
                result["discarded_requests"] == (case == "partial"), "attempt accounting")
        self.counts["generations"] = expected
        store.require(expected == LIMITS["generations"], "request envelope")
        values = self.idle("final", expected)
        store.require(values["q4t_requests_success_total"] >= 6 and
            values["q4t_requests_error_total"] == 0, "unexpected real business error")
        pkg.save(OUT / "final-metrics.json", values)
        self.front_cli(1, "front-1-stop", "stop"); self.wait_front(1, {"STOPPED"}); self.wait_gone(1)
        self.preserve(self.fronts[1]["folder"])
        self.stop_backend(); self.preserve(self.backend_folder)

    def execute(self):
        OUT.mkdir(mode=0o700)
        pkg.save(OUT / "begin.json", dict(monotonic=self.started, unix_seconds=time.time()))
        result = dict(status="FAILED", performance_acceptance=False, long_run=False)
        def interrupted(signum, _):
            raise RuntimeError("validation interrupted: " + str(signum))
        handlers = {sig: signal.signal(sig, interrupted) for sig in
                    (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)}
        try:
            a.require_idle(OUT / "gpu-before.json")
            self.launch(); self.business()
            time.sleep(10)
            store.require(self.backend_process.poll() == 0 and
                all(f["process"].poll() == 0 for f in self.fronts.values()), "service restarted")
            result.update(status="PASS_PROJECT_TASK_RECOVERY_SHORT", model_starts=1, gateway_starts=1,
                successful_complete_responses=6, incomplete_client_responses=1,
                completed_tasks=3, killed_applications=3, tool_attempts=4, saved_tool_results=3)
        except BaseException as error:
            result.update(status="ENV_ABORT" if str(error).startswith("ENV_ABORT:") else "FAILED",
                          error=repr(error))
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0); self.cleaning = True
            try:
                for child in self.applications:
                    if child["process"].poll() is None:
                        sup.signal_owner(child["owner"], signal.SIGTERM)
                        child["process"].wait(5)
                for index, front in self.fronts.items():
                    if front["process"].poll() is None:
                        self.front_cli(index, f"front-{index}-cleanup-stop", "stop")
                        front["process"].wait(110)
                self.stop_backend()
                a.require_idle(OUT / "gpu-final.json")
                store.require(a.lock_available(a.guard.common_lock()), "GPU lock retained")
                store.require(time.monotonic() - self.started <= LIMITS["seconds"], "phase deadline")
                store.require(self.gpu_started is not None and
                    time.monotonic() - self.gpu_started <= GPU_WINDOW, "GPU window deadline")
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
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
                for log in self.logs:
                    log.close()
                result.update(counts=self.counts, seconds=time.monotonic() - self.started)
                pkg.save(OUT / "result.json", result)
        return result


if __name__ == "__main__":
    os.umask(0o077); configure()
    parser = argparse.ArgumentParser(); parser.add_argument("action", choices=("prepare", "check", "run"))
    args = parser.parse_args()
    if args.action == "prepare":
        value = prepare()
    elif args.action == "check":
        check(); value = dict(checked=True, started=False)
    else:
        plan = check(); store.require(not OUT.exists(), "preserve prior run")
        with sup.processes.child_subreaper():
            value = Run(plan).execute()
    print(json.dumps(value))
