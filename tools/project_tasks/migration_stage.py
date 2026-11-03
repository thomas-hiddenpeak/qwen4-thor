"""One managed model: actual historical tasks, key revocation, explicit upgrades."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import engine, migration, store, transport, migration_cpu as cpu
from tools.project_tasks import migration_fixtures as fixtures
from tools.project_tasks.live_worker import QUESTION
from tools.serve_retention import stage as retained
from tools.serve_clients import managed_stage as previous
from tools.serve_gateway import managed as gateway
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from tools.serve_soak import evidence

BASE = cpu.BASE
PLAN, OUT = BASE / "plan.json", BASE / "execution"
LIMITS = dict(starts=1, generations=4, negatives=3, observations=64, seconds=480)
GPU_WINDOW = 420
SOURCES = sorted(set((*cpu.SOURCES, "tools/project_tasks/migration_stage.py",
    "tools/project_tasks/migration_worker.py", "tools/project_tasks/migration_driver_cpu.py")))


def configure():
    retained.BASE, retained.PLAN, retained.OUT = BASE, PLAN, OUT
    retained.STORE = BASE / "owned-generations"
    retained.LIMITS, retained.GPU_WINDOW = LIMITS, GPU_WINDOW
    retained.configure()


def prepare():
    store.require(not PLAN.exists() and not OUT.exists(), "preserve prior migration attempt")
    store.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT), "commit reviewed sources")
    for folder, count in (("cpu-01", 49), ("cpu-driver", 3)):
        tested = pkg.read(BASE / folder / "result.json")
        store.require(tested["passed"] and tested["tests_run"] == count, "CPU gate")
        for name, digest in tested["sources"].items():
            store.require(pkg.sha(ROOT / name) == digest, "tested source changed: " + name)
    previous.configure("day"); old = previous.check()
    store.require(not previous.OUT.exists(), "operator day executed")
    days = {**old["old_day_plans"], str(previous.PLAN): pkg.sha(previous.PLAN)}
    configure()
    private = BASE / "private"; private.mkdir(mode=0o700)
    policy = pkg.read(previous.BASE / "model-private/policy-private.json")
    rotated = deepcopy(policy)
    store.require([row["id"] for row in policy["clients"]] == ["alpha", "beta"], "client identities")
    rotated["clients"][0]["api_key"] = secrets.token_hex(32)
    policy_path = private / "rotated-policy.json"; store.write_new(policy_path, store.encode(rotated))
    old_gateway = Path(old["gateway"]["package"])
    new_gateway = gateway.prepare(policy_path, Path(old["gateways"][0]["package"]) / "nginx",
        old_gateway / "certificate.pem", old_gateway / "private-key.pem", BASE / "rotated-gateway",
        previous.BASE / "model-private/clients.json")
    protocol = pkg.read(ROOT / "build/q4t.release.json")["chat_protocol"]
    clients = {}
    for index, client in enumerate(("alpha", "beta")):
        runtime = BASE / ("historical-" + client)
        receipt = fixtures.extract_runtime(index, runtime)
        local = runtime / ".q4t-work"; local.mkdir(mode=0o700)
        profile = local / "old-profile.json"; current = private / (client + ".json")
        value = dict(client_id=client, api_key=policy["clients"][index]["api_key"], connect_host="127.0.0.1",
            base_url=f'https://{policy["server_name"]}:{policy["listen_port"]}/v1',
            ca_file=str(old_gateway / "certificate.pem"), model="qwen3.8-flash-next",
            protocol_sha256=protocol["protocol_sha256"], template_sha256=protocol["template"]["reference_sha256"])
        store.write_new(profile, store.encode(value))
        store.write_new(current, store.encode(dict(value, api_key=rotated["clients"][index]["api_key"])))
        clients[client] = dict(runtime=receipt, root=str(local / "tasks"), tokens=512 if index == 0 else 64,
            old_profile=str(profile), old_profile_sha256=pkg.sha(profile), old_binding=transport.profile(profile)[1],
            profile=str(current), profile_sha256=pkg.sha(current), binding=transport.profile(current)[1])
    pkg.save(PLAN, dict(package=old["package"], gateway=old["gateway"], gateways=[old["gateways"][0], new_gateway],
        unit="unused-task-migration.service", token="srv11c-short", limits=LIMITS, gpu_window_seconds=GPU_WINDOW,
        default=old["default"], private=old["private"], new_policy=dict(path=str(policy_path), sha256=pkg.sha(policy_path)),
        days=days, clients=clients, store_policy=dict(max_bytes=128 * 1024**2, max_generations=3, max_age_days=14),
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        source_files={name: pkg.sha(ROOT / name) for name in SOURCES},
        cpu_sha256=pkg.sha(BASE / "cpu-01/result.json"), driver_sha256=pkg.sha(BASE / "cpu-driver/result.json"),
        model_starts=1, gateway_starts=2, complete_responses=4, preflights=3, capabilities=4,
        revoked_key_requests=1, actual_application_kills=2, real_tool_executions=2, performance_acceptance=False))
    check()
    return dict(prepared=True, models_started=0, listeners_started=0)


def check():
    plan = pkg.read(PLAN)
    store.require(plan["limits"] == LIMITS and plan["gpu_window_seconds"] == GPU_WINDOW, "frozen envelope changed")
    for name, checksum in plan["source_files"].items():
        store.require(pkg.sha(ROOT / name) == checksum, "source changed: " + name)
    for name, checksum in {**plan["default"], **plan["private"], **plan["days"]}.items():
        store.require(pkg.sha(ROOT / name) == checksum, "fixed identity changed")
    for name in plan["days"]:
        store.require(not (Path(name).parent / "execution").exists(), "user day executed")
    store.require(pkg.sha(plan["new_policy"]["path"]) == plan["new_policy"]["sha256"], "new key policy changed")
    for row in plan["clients"].values():
        for prefix in ("", "old_"):
            store.require(pkg.sha(row[prefix + "profile"]) == row[prefix + "profile_sha256"] and
                transport.profile(row[prefix + "profile"])[1] == row[prefix + "binding"], "private profile changed")
        for name, digest in row["runtime"]["sources"].items():
            store.require(pkg.sha(Path(row["runtime"]["root"]) / name) == digest, "historical runtime changed")
    for folder, key in (("cpu-01", "cpu_sha256"), ("cpu-driver", "driver_sha256")):
        store.require(pkg.sha(BASE / folder / "result.json") == plan[key], "CPU record changed")
    retained.r.binding("backend", plan["package"]["package"], plan["package"]["manifest_sha256"])
    for selected in plan["gateways"]: gateway.verify(selected["package"], selected["manifest_sha256"])
    return plan


class Run(retained.Run):
    def __init__(self, plan):
        super().__init__(plan)
        self.applications = []

    def cli(self, client, action, label, *, expected=0, extra=(), old=False):
        row = self.plan["clients"][client]
        return self.command(label, [sys.executable, "-B", ROOT / "tools/project_tasks/runner.py", action,
            "--root", row["root"], "--profile", row["old_profile" if old else "profile"],
            "--key", "migration-query", *extra], expected=expected, timeout=90)

    def export(self, client, label):
        row = self.plan["clients"][client]; name = store.task_name(client, "migration-query")
        with store.open_task(row["root"], name, exclusive=False) as ledger:
            state = engine.replay(ledger)
            pkg.save(OUT / (label + "-ledger.json"), dict(meta=ledger.meta, events=ledger.events(),
                database_sha256=pkg.sha(ledger.path / "ledger.db")))
            return engine.public(ledger, state)

    def historical(self, client, expected_requests):
        row = self.plan["clients"][client]
        store.init(row["root"], max_tasks=1)
        folder = OUT / client; folder.mkdir(mode=0o700)
        log = (folder / "child.log").open("xb"); self.logs.append(log)
        argv = [sys.executable, "-B", ROOT / "tools/project_tasks/migration_worker.py", row["runtime"]["root"],
            row["root"], row["old_profile"], ROOT, folder, row["tokens"], QUESTION]
        process = subprocess.Popen(list(map(str, argv)), cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, umask=0o077)
        owner = sup.process_identity(process.pid); self.applications.append(dict(process=process, owner=owner))
        pkg.save(folder / "owner.json", owner)
        marker = folder / "boundary.json"; until = time.monotonic() + 90
        while time.monotonic() < until and not marker.exists():
            self.budget(); store.require(process.poll() is None, "historical task exited before saved result")
            time.sleep(.02)
        store.require(marker.exists() and process.poll() is None and pkg.read(marker)["pid"] == process.pid,
                      "historical boundary owner/deadline")
        self.drain(client + "-before-kill", expected_requests)
        self.budget("negatives")
        store.require(sup.signal_owner(owner, signal.SIGKILL), "application identity changed")
        store.require(process.wait(5) == -signal.SIGKILL, "application did not die")
        pkg.save(folder / "killed.json", dict(owner=owner, returncode=process.returncode, signal="SIGKILL"))
        before = self.export(client, client + "-before")
        store.require(before["state"] == "TOOLS_PENDING" and before["requests"] == before["saved_tool_results"] == 1,
                      "historical saved result lost")
        refused = self.cli(client, "resume", client + "-upgrade-required", expected=1, old=True)
        store.require(json.loads(refused.stderr)["error"] == "ValueError", "historical runtime unexpectedly executes")

    def revoked(self):
        self.budget("negatives")
        value, _ = transport.profile(self.plan["clients"]["alpha"]["old_profile"])
        with (OUT / "revoked-key-wire.bin").open("xb") as wire:
            try:
                transport.Client(value).request("GET", "/capabilities", None, 10,
                    lambda response, reader: reader.read(65536), error_sink=wire.write)
            except engine.chat.RemoteError as error:
                store.require(error.status == 401, "old API key was not unauthorized")
                pkg.save(OUT / "revoked-key.json", dict(http_status=error.status, code=error.code,
                    no_generation=True, checked_against_gateway=2))
            else: raise ValueError("old API key still accepted")

    def migrated(self, client, expected_requests):
        row = self.plan["clients"][client]; plan_path = OUT / client / "migration-plan.json"
        planned = json.loads(self.cli(client, "plan-migration", client + "-planned", extra=(
            "--plan", plan_path, "--reason", "verified accepted runtime upgrade and same-client credential handoff")).stdout)
        extra = ("--plan", plan_path, "--plan-sha256", planned["plan_sha256"])
        moved = json.loads(self.cli(client, "apply-migration", client + "-migrated", extra=extra).stdout)
        store.require(moved["migration_applied"] and moved["no_execution"] and moved["requests"] == 1 and
            moved["tool_attempts"] == moved["saved_tool_results"] == 1, "migration reset saved progress")
        again = json.loads(self.cli(client, "apply-migration", client + "-migration-duplicate", extra=extra).stdout)
        store.require(again == dict(moved, migration_applied=False, already_applied=True), "migration repeated")
        result = json.loads(self.cli(client, "resume", client + "-completed").stdout)
        self.drain(client + "-idle", expected_requests)
        store.require(result["state"] == "COMPLETED" and result["outcome"] == "ANSWERED" and
            result["requests"] == 2 and result["tool_attempts"] == result["saved_tool_results"] == result["migrations"] == 1 and
            result["max_tokens"] == row["tokens"] and "Qwen4ExpForConditionalGeneration" in result["answer"] and
            "[docs/MODEL.md:L1-L24]" in result["answer"], "migrated task quality/budget")
        duplicate = json.loads(self.cli(client, "submit", client + "-duplicate", extra=("--question", QUESTION,
            "--max-requests", "2", "--max-tools", "1", "--max-tokens", str(row["tokens"]))).stdout)
        store.require(duplicate == dict(result, created=False), "migrated business key duplicated")
        store.require(self.export(client, client + "-final") == result, "reconstruction changed")

    def business(self):
        self.start_front(1)
        self.historical("alpha", 1); self.historical("beta", 2)
        self.front_cli(1, "front-1-stop", "stop"); self.wait_front(1, {"STOPPED"}); self.wait_gone(1)
        self.preserve(self.fronts[1]["folder"])
        self.start_front(2); self.revoked()
        self.migrated("alpha", 3); self.migrated("beta", 4)
        self.counts["generations"] = 4
        values = self.drain("final", 4)
        store.require(values["q4t_structured_workers_started_total"] == values["q4t_structured_workers_reaped_total"] == 4,
                      "grammar cleanup")
        pkg.save(OUT / "final-metrics.json", values)
        self.front_cli(2, "front-2-stop", "stop"); self.wait_front(2, {"STOPPED"}); self.wait_gone(2)
        self.preserve(self.fronts[2]["folder"]); self.stop_backend(); self.preserve(self.backend_folder)

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
            result.update(status="PASS_PROJECT_TASK_MIGRATION_SHORT", model_starts=1, gateway_starts=2,
                complete_responses=4, preflights=3, capabilities=4, completed_tasks=2, revoked_key_requests=1,
                killed_applications=2, real_tool_executions=2, duplicate_submissions=2, applied_migrations=2, duplicate_migrations=2)
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
        plan = check(); store.require(not OUT.exists(), "preserve previous execution")
        with sup.processes.child_subreaper(): value = Run(plan).execute()
    print(json.dumps(value))
