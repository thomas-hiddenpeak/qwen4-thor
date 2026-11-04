"""Offline reconstruction of actual durable tasks and both original/corrected runs."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import correction as c, engine, store

BASE = c.ORIGINAL
DEST = ROOT / "docs/evidence/serve-project-tasks-20261104"
require = store.require


def evaluate(data):
    read = lambda name: json.loads(data[name])
    get = lambda name: read("tls-correction/execution/" + name)
    plan = read("tls-correction/plan.json")
    initial = read("execution/result.json"); result = get("result.json")
    require(initial["status"] == "FAILED" and initial["cleanup_confirmed"] and
        initial["counts"]["generations"] == 0 and b"Bad file descriptor" in
        data["execution/known/child.log"], "original failure lost")
    require(read("cpu-01/result.json")["passed"] and read("cpu-01/result.json")["tests_run"] == 18 and
        read("cpu-wire/result.json")["passed"] and read("cpu-wire/result.json")["effective_tests"] == 19 and
        not read("cpu-transport-original/result.json")["passed"] and
        read("cpu-transport-original/result.json")["errors"] == 1 and
        read("cpu-transport-fixed/result.json")["passed"] and
        read("cpu-transport-fixed/result.json")["effective_tests"] == 20 and
        read("cpu-driver/result.json")["passed"] and read("cpu-driver/result.json")["tests_run"] == 2,
        "CPU acceptance/retained failure")
    require(result["status"] == "PASS_PROJECT_TASK_RECOVERY_SHORT" and result["cleanup_confirmed"] and
        result["counts"]["generations"] == 7 and result["counts"]["negatives"] == 3 and
        result["counts"]["controls"] <= 64 and result["seconds"] <= 480, "bounded result")
    metrics = get("final-metrics.json")
    require(metrics["q4t_requests_total"] == 7 and metrics["q4t_requests_success_total"] == 6 and
        metrics["q4t_requests_error_total"] == 0 and metrics["q4t_requests_aborted_total"] == 1 and
        metrics['q4t_requests_aborted_by_reason_total{reason="client_disconnected"}'] == 1 and
        metrics["q4t_structured_workers_started_total"] == metrics["q4t_structured_workers_reaped_total"] == 7 and
        all(metrics[key] == 0 for key in c.stage.a.EMPTY), "server outcomes/cleanup")
    timing = {}
    for line in get("final-00.json")["response"]["body"].splitlines():
        if line.startswith(("q4t_response_seconds_sum ", "q4t_response_seconds_count ")):
            name, value = line.split(); timing[name] = float(value)
    require(timing["q4t_response_seconds_count"] == 6 and timing["q4t_response_seconds_sum"] > 0,
            "server response timing missing")
    cases = {}
    scratch_parent = BASE / "offline-verification"
    scratch_parent.mkdir(exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=scratch_parent) as temp:
        for case, (requests, tools, _) in c.stage.CASES.items():
            exported = get(case + "-ledger.json")
            task_id = exported["meta"]["task_id"]
            prefix = "tls-correction/tasks/tasks/" + task_id + "/"
            folder = Path(temp) / task_id; folder.mkdir(mode=0o700)
            for name, raw in data.items():
                if name.startswith(prefix):
                    child = name[len(prefix):]
                    require("/" not in child, "nested task artifact")
                    store.write_new(folder / child, raw)
            db_raw = data[prefix + "ledger.db"]
            require(hashlib.sha256(db_raw).hexdigest() == exported["database_sha256"], "database identity")
            db = store.connection(folder)
            try:
                ledger = store.Ledger(folder, db, exported["meta"]["store_id"])
                require(ledger.meta == exported["meta"] and ledger.events() == exported["events"],
                        "SQLite/export mismatch")
                state = engine.replay(ledger)
                public = engine.public(ledger, state)
                completed = json.loads(get(case + "-completed.json")["stdout"])
                duplicate = json.loads(get(case + "-duplicate.json")["stdout"])
                require(public == completed and duplicate == dict(public, created=False) and
                    public["state"] == "COMPLETED" and public["outcome"] == "ANSWERED" and
                    public["requests"] == requests and public["tool_attempts"] == tools and
                    public["saved_tool_results"] == 1 and public["runs"] == 2,
                    "task recovery/repetition outcome")
                require("Qwen4ExpForConditionalGeneration" in public["answer"] and
                    "[docs/MODEL.md:L1-L24]" in public["answer"], "business answer")
                events = ledger.events()
                starts = [row for row in events if row["kind"] == "tool_started"]
                results = [row for row in events if row["kind"] == "tool_result"]
                responses = [row for row in events if row["kind"] == "response"]
                require(len(starts) == tools and len(results) == 1 and len(responses) == 2,
                        "tool or response duplicated")
                for row in starts:
                    require(row["payload"]["name"] == "read_project_record" and
                        row["payload"]["arguments"] == dict(path="docs/MODEL.md", start_line=1, line_count=24),
                        "unexpected real tool")
                tool = json.loads(results[0]["payload"]["content"])
                require(tool["ok"] and tool["source"]["sha256"] == plan["source_files"]["docs/MODEL.md"] and
                    tool["source"]["citation"] == "[docs/MODEL.md:L1-L24]", "actual source identity")
                marker = get(case + "/boundary.json"); killed = get(case + "/killed.json")
                require(marker["task_id"] == task_id and marker["pid"] == killed["owner"]["pid"] and
                    killed["returncode"] == -9 and marker["kind"] == c.stage.CASES[case][2],
                    "actual process death boundary")
                if case != "known":
                    stopped = get(case + "-stopped.json"); blocked = get(case + "-blocked.json")
                    require(stopped["returncode"] == blocked["returncode"] == 2 and
                            json.loads(stopped["stdout"]) == json.loads(blocked["stdout"]),
                            "unknown resume changed state")
                    resolutions = [row for row in events if row["kind"] == "resolve"]
                    require(len(resolutions) == 1 and resolutions[0]["payload"]["decision"] ==
                        ("repeat-read" if case == "unknown" else "discard-request"), "explicit decision missing")
                if case == "partial":
                    raw = data[prefix + "wire-001.bin"]
                    require(raw and b"[DONE]" not in raw and public["discarded_requests"] == 1,
                            "incomplete wire promoted or lost")
                else:
                    observed = [json.loads(line) for line in data[
                        "tls-correction/execution/" + case + "/read-observations.jsonl"].splitlines()]
                    require(len(observed) == 1 and observed[0]["result"]["source"] == tool["source"],
                            "real pre-crash read missing")
                require(public["read_repetitions"] == (case == "unknown"), "read repetition count")
                cases[case] = public
            finally:
                db.close()
    guards = {}
    for phase in ("", "tls-correction/"):
        prefix = phase + "owned-generations/generations/"
        backends = [name.rsplit("/", 1)[0] for name in data if name.startswith(prefix) and
                    name.endswith("/reservation.json") and read(name)["kind"] == "backend"]
        require(len(backends) == 1, "model generation budget")
        backend = backends[0]
        terminal = read(backend + "/run/terminal.json")
        require(terminal["state"] == "STOPPED" and terminal["cleanup_confirmed"] and
            terminal["worker"]["attempts"] == 1 and terminal["worker"]["restarts"] == 0 and
            terminal["guard"]["status"] == "COMPLETED", "managed backend outcome")
        events = [json.loads(line) for line in data[backend + "/run/lease/events.jsonl"].splitlines()]
        samples = [v for v in events if v["event"] in ("preflight", "monitor")]
        require(events[0]["event"] == "lease_acquired" and events[-1]["event"] == "lease_released" and
            events[-1]["status"] == "COMPLETED" and all(not v["conflicts"] for v in samples) and
            all(0 <= b["monotonic"] - a["monotonic"] <= 10 for a, b in zip(events, events[1:])),
            "GPU guard continuity")
        guards[phase or "original"] = dict(seconds=events[-1]["monotonic"] - events[0]["monotonic"],
                                           observations=len(samples), records=len(events))
    post = read("postflight/result.json")
    require(post["passed"] and post["default_unchanged"] and post["day_plans_unchanged"] and
            post["lock_available"] and post["sealed_generations"] == 4, "postflight missing")
    return dict(status="ACCEPT_PROJECT_TASK_RECOVERY_SHORT", cpu_effective_tests=20, driver_tests=2,
        original_failure_retained=True, original_model_starts=1, original_generations=0,
        correction_model_starts=1, correction_gateway_starts=1, cumulative_model_starts=2,
        requests=7, complete_responses=6, client_incomplete_responses=1, completed_tasks=3,
        actual_application_kills=3, tool_attempts=4, saved_tool_results=3, duplicate_submissions=3,
        grammar_reaped=7, owned_exited=post["owned_exited"], controls=result["counts"]["controls"],
        seconds=result["seconds"], original_seconds=initial["seconds"], guards=guards,
        successful_server_response_seconds_sum=timing["q4t_response_seconds_sum"],
        successful_server_response_count=6, cleanup_confirmed=True, default_changed=False,
        long_run=False, performance_acceptance=False, exactly_once_claim=False)


def postflight():
    c.configure(); plan = c.check()
    output = BASE / "postflight"; output.mkdir(mode=0o700)
    c.stage.a.require_idle(output / "gpu.json")
    lock = c.stage.a.guard.common_lock(); info = lock.stat()
    require([info.st_dev, info.st_ino] == [66305, 3294067] and
            c.stage.a.lock_available(lock), "original GPU lock unavailable")
    result = subprocess.run(["systemctl", "--user", "show",
        "soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service", "-p", "LoadState",
        "-p", "ActiveState", "-p", "SubState", "-p", "MainPID"], capture_output=True, text=True, timeout=10, check=True)
    require(dict(line.split("=", 1) for line in result.stdout.splitlines()) ==
        dict(LoadState="masked", ActiveState="inactive", SubState="dead", MainPID="0"), "SOAR changed")
    (output / "soar-unit.txt").write_text(result.stdout)
    containers = subprocess.run(["sudo", "-n", "docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=10, check=True)
    require(not containers.stdout.strip(), "foreign containers; preserve, do not interfere")
    (output / "containers.txt").write_text(containers.stdout)
    owners, inventories = {}, []
    for root in (BASE, c.BASE):
        namespace = root / "owned-generations"
        with c.stage.retained.store.locked(namespace) as (path, meta):
            rows = c.stage.retained.store.inventory(path, meta)
        require(len(rows) == 2 and all(row["state"] == "SEALED" for row in rows), "unsealed service generation")
        inventories.extend(rows)
        for path in (namespace / "generations").iterdir():
            reservation = c.stage.pkg.read(path / "reservation.json")
            seal = c.stage.pkg.read(path / "seal.json")
            for owner in [reservation["owner"], *seal["proof"]["owners"]]:
                owners[owner["pid"], owner["start_ticks"], owner["boot_id"]] = owner
        for path in (root / "execution").glob("*/owner.json"):
            owner = c.stage.pkg.read(path)
            owners[owner["pid"], owner["start_ticks"], owner["boot_id"]] = owner
    require(all(not c.stage.retained.r.owner_live(v) for v in owners.values()), "owned process remained")
    value = dict(passed=True, owners=list(owners.values()), owned_exited=len(owners),
        inventory=inventories, sealed_generations=len(inventories), lock_available=True,
        lock_identity=[info.st_dev, info.st_ino], default_unchanged=True,
        day_plans_unchanged=True, days=plan["days"], new_models_or_http=0)
    store.write_new(output / "result.json", store.encode(value))
    return {k: value[k] for k in ("passed", "owned_exited", "sealed_generations", "day_plans_unchanged")}


def collect():
    paths = [BASE / "plan.json", c.BASE / "plan.json"]
    for folder in ("cpu-01", "cpu-wire", "cpu-driver", "cpu-transport-original", "cpu-transport-fixed",
                   "execution", "owned-generations", "tasks", "postflight", "tls-correction/execution",
                   "tls-correction/owned-generations", "tls-correction/tasks"):
        paths.extend(p for p in (BASE / folder).rglob("*") if p.is_file())
    data = {str(p.relative_to(BASE)): p.read_bytes() for p in sorted(set(paths)) if not p.is_symlink()}
    data["archiver/report.py"] = Path(__file__).read_bytes()
    private = c.stage.previous.BASE / "model-private"
    policy = c.stage.pkg.read(private / "policy-private.json")
    secrets = [row["api_key"] for row in policy["clients"]] + [c.stage.pkg.read(private / "clients.json")["gateway_token"]]
    for name, raw in data.items():
        require(all(secret.encode() not in raw for secret in secrets) and not any(
            b"-----BEGIN " + kind + b"PRIVATE KEY-----" in raw for kind in (b"", b"RSA ", b"EC ", b"ENCRYPTED ")),
            "private credential in public records: " + name)
    plan = c.stage.pkg.read(c.BASE / "plan.json")
    local = {}
    for path in (Path(plan["package"]["package"]) / "q4t", Path(plan["gateways"][0]["package"]) / "nginx"):
        local[str(path)] = dict(bytes=path.stat().st_size, sha256=c.stage.pkg.sha(path))
    return data, local


def archive():
    data, local = collect(); result = evaluate(data)
    DEST.mkdir()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as output:
        for name, raw in sorted(data.items()):
            info = tarfile.TarInfo(name); info.size, info.mode, info.mtime = len(raw), 0o600, 0
            output.addfile(info, io.BytesIO(raw))
    packed = gzip.compress(buffer.getvalue(), mtime=0)
    (DEST / "records.tar.gz").write_bytes(packed)
    c.stage.pkg.save(DEST / "manifest.json", dict(files={name: dict(bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest()) for name, raw in data.items()}, local_identities=local,
        archive=dict(bytes=len(packed), sha256=hashlib.sha256(packed).hexdigest())))
    c.stage.pkg.save(DEST / "result.json", result)
    return dict(records=len(data), local_identities=len(local), compressed_bytes=len(packed), result=result)


def verify():
    manifest = c.stage.pkg.read(DEST / "manifest.json")
    packed = (DEST / "records.tar.gz").read_bytes()
    require(len(packed) == manifest["archive"]["bytes"] and hashlib.sha256(packed).hexdigest() ==
            manifest["archive"]["sha256"], "archive changed")
    data = {}
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as source:
        for member in source:
            require(member.isfile() and member.name not in data and member.name in manifest["files"], "unexpected archive member")
            raw = source.extractfile(member).read(); expected = manifest["files"][member.name]
            require(len(raw) == expected["bytes"] and hashlib.sha256(raw).hexdigest() == expected["sha256"], "record changed")
            data[member.name] = raw
    require(data.keys() == manifest["files"].keys() and evaluate(data) == c.stage.pkg.read(DEST / "result.json"),
            "offline outcome differs")
    return dict(passed=True, records=len(data), local_identities=len(manifest["local_identities"]), new_models_or_http=0)


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(); parser.add_argument("action", choices=("postflight", "archive", "verify"))
    args = parser.parse_args()
    print(json.dumps(globals()[args.action]()))
