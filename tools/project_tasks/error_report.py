"""Archive and independently rebuild typed command errors and unchanged tasks without HTTP or GPU."""
import argparse
import gzip
import hashlib
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import error_stage as s, engine, store

BASE = s.BASE
DEST = ROOT / "docs/evidence/serve-task-errors-20261104"
require = store.require


def evaluate(data):
    read = lambda name: json.loads(data[name])
    get = lambda name: read(("execution/" if "execution/" + name in data else "offline-finish/") + name)
    cli = lambda label: json.loads(get(label + ".json")["stdout"])
    plan, result = read("plan.json"), get("result.json")
    for folder, count in (("cpu-01", 106), ("cpu-fix-01", 18), ("cpu-driver", 2)):
        cpu = read(folder + "/result.json")
        first = folder == "cpu-01"
        require(cpu["tests_run"] == count and cpu["passed"] is (not first) and
            cpu["failures"] == 0 and cpu["errors"] == (18 if first else 0) and
            cpu["models_started"] == cpu["listeners_started"] == cpu["gpu_started"] == 0, "CPU outcomes")
        for name, digest in cpu["sources"].items():
            require(hashlib.sha256(data[folder + "/source/" + name]).hexdigest() == digest, "CPU snapshot identity")
            if not (first and name == "tools/project_tasks/test_errors.py"):
                require(plan["source_files"][name] == digest, "CPU frozen source")
    first_log = data["cpu-01/tests.log"].decode()
    require(first_log.count(" ... ok\n") == 88 and first_log.count(" ... ERROR\n") == 18 and
        all("tools.project_tasks.test_errors.Errors." in line for line in first_log.splitlines()
            if line.endswith(" ... ERROR")), "preserved CA fixture failure scope")
    kills = read("cpu-01/result.json")["file_operation_kills"]
    require(len(kills) == 23 and all(row["returncode"] == -9 for row in kills), "existing crash coverage")
    require(result["status"] == "FAILED" and result["cleanup_confirmed"] and
        result["error"] == "FileNotFoundError(2, 'No such file or directory')" and
        result["counts"]["generations"] == 4 and result["counts"]["negatives"] == 3 and
        result["counts"]["controls"] <= 64 and result["seconds"] <= 480 and not result["long_run"], "bounded live result")
    finish = read("offline-finish/result.json")
    require(finish["passed"] and finish["status"] == "PASS_ERRORS_OFFLINE_FINISH" and
        finish["task_state_unchanged"] and finish["original_live_evidence_unchanged"] and
        finish["seconds"] <= 60 and len(finish["commands"]) == 5 and
        all(finish[key] == 0 for key in ("models_started","listeners_started","http_requests","tools_executed","gpu_started")), "offline continuation")
    require(finish["original_plan_sha256"] == hashlib.sha256(data["plan.json"]).hexdigest() and
        finish["cpu_sha256"] == hashlib.sha256(data["cpu-finish/result.json"]).hexdigest(), "continuation plan/gate")
    for name, checksum in finish["original"].items():
        require(hashlib.sha256(data[name]).hexdigest() == checksum, "original live evidence changed")
    extra = read("cpu-finish/result.json")
    require(extra["passed"] and extra["tests_run"] == 2 and extra["errors"] == extra["failures"] == 0, "targeted driver regression")
    for name, checksum in extra["sources"].items():
        require(hashlib.sha256(data["cpu-finish/source/" + name]).hexdigest() == checksum ==
            finish["source_files"][name], "continuation source identity")
    for label in finish["commands"]:
        command = get(label + ".json")
        require(command["execution_forbidden"] and not command["tripwire_triggered"], "offline command attempted execution")
    schema = read("cpu-fix-01/source/docs/project-task-errors-v1.schema.json")
    codes = {"key-conflict": ("key_conflict", "submit"), "invalid-control": ("invalid_arguments", "resume"),
        "unauthorized": ("remote_auth", "submit"), "binding-conflict": ("binding_changed", "resume"),
        "capacity": ("task_capacity", "submit"), "changed-plan": ("plan_changed", "apply-archive")}
    for label, (code, operation) in codes.items():
        command = get(label + ".json"); error = json.loads(command["stderr"])
        require(command["returncode"] == 1 and not command["stdout"] and
            set(error) == set(schema["required"]) and error["profile"] == "q4t-project-error-v1" and
            error["code"] == code and error["operation"] == operation and error["automatic_retry"] is False and
            error["http_status"] == (401 if label == "unauthorized" else None), "live command error")
        for key in ("error", "category", "message", "next_action"):
            require(error[key] in schema["properties"][key]["enum"], "unsafe public error field")
    reservations = [name.rsplit("/",1)[0] for name in data if name.startswith("owned-generations/generations/") and name.endswith("/reservation.json")]
    backend = next(p for p in reservations if read(p + "/reservation.json")["kind"] == "backend")
    gateway = next(p for p in reservations if read(p + "/reservation.json")["kind"] == "gateway")
    backend_log = data[backend + "/run/backend.log"].decode()
    ends = [dict(re.findall(r"([a-z_]+)=([^ ]+)", line)) for line in backend_log.splitlines()
            if line.startswith("[q4t][request]") and " event=end " in line]
    require(len(ends) == 4 and all(row["outcome"] == "success" and row["reason"] == "completed" and
        row["released"] == "1" for row in ends) and len({row["id"] for row in ends}) == 4,
        "actual request ends")
    require(backend_log.count("grammar worker reaped=1 ") == 4 and
        "shutdown complete (0 in-flight remaining)" in backend_log, "worker reap/shutdown logs")
    timing_sum = sum(float(row["elapsed_ms"]) for row in ends)/1000
    gateway_rows=[json.loads(line) for line in data[gateway + "/run/gateway.log"].splitlines() if line.startswith(b"{")]
    require(len(gateway_rows)==11 and sum(row["status"]==401 and row["client"]=="" and
        row["upstream_status"]=="" for row in gateway_rows)==1 and
        all(sum(row["client"]==client and row["status"]==200 and row["upstream_status"]=="200" and
            row["lane"]==lane for row in gateway_rows)==count for client in ("alpha","beta")
            for lane,count in (("control",1),("data",4))), "actual gateway workload/auth rejection")
    usages, states, response_ids = {}, {}, set()
    totals = dict(preflights=0, requests=0, responses=0, tools=0, capability_attempts=0)
    parent = BASE / "offline-verification"; parent.mkdir(exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        for case, tokens in (("alpha",512),("beta",64),("denied",64)):
            exported = get(case + ("-ledger.json" if case == "denied" else "-ledger-ledger.json")); name = exported["meta"]["task_id"]
            prefix = "tasks/tasks/" + name + "/"
            folder = Path(temp) / name; folder.mkdir(mode=0o700)
            for path, raw in data.items():
                if path.startswith(prefix):
                    child = path[len(prefix):]; require("/" not in child, "nested task entry")
                    store.write_new(folder / child, raw)
            require(hashlib.sha256(data[prefix + "ledger.db"]).hexdigest() == exported["database_sha256"], "actual database")
            db = store.connection(folder)
            try:
                ledger = store.Ledger(folder, db, exported["meta"]["store_id"])
                require(ledger.meta == exported["meta"] and ledger.events() == exported["events"] and
                    ledger.spec["binding"] == plan["bindings"][case] and ledger.spec["max_tokens"] == tokens and
                    ledger.spec["runtime_sources"] == {n: plan["source_files"][n] for n in ledger.spec["runtime_sources"]}, "database binding")
                state = engine.replay(ledger); value = engine.public(ledger, state); states[case] = value
                response_ids.update(row["payload"]["completion"]["id"] for row in ledger.events() if row["kind"] == "response")
                by_kind = lambda kind: [row["payload"] for row in ledger.events() if row["kind"] == kind]
                for key, kind in (("preflights","preflight_started"),("requests","request"),("responses","response"),
                                  ("tools","tool_started"),("capability_attempts","run")):
                    totals[key] += len(by_kind(kind))
                require(cli(case + "-duplicate") == dict(value, created=False), "duplicate mutated task")
                if case == "denied":
                    require(value["state"] == "READY" and value["runs"] == 1 and value["requests"] ==
                        value["preflight_checks"] == value["tool_attempts"] == 0 and len(ledger.events()) == 1 and
                        not list(folder.glob("wire-*")) and not list(folder.glob("check-*")), "auth error executed/retried")
                else:
                    require(cli(case + "-completed") == dict(value, created=True) and cli(case + "-resume") == value and
                        value["state"] == "COMPLETED" and value["outcome"] == "ANSWERED" and value["requests"] ==
                        value["preflight_checks"] == 2 and value["tool_attempts"] == value["saved_tool_results"] == 1 and
                        "Qwen4ExpForConditionalGeneration" in value["answer"] and "[docs/MODEL.md:L1-L24]" in value["answer"], "business outcome")
                    tool = by_kind("tool_started")[0]; record = json.loads(by_kind("tool_result")[0]["content"])
                    require(tool["name"] == "read_project_record" and tool["arguments"] ==
                        dict(path="docs/MODEL.md",start_line=1,line_count=24) and record["ok"] and
                        record["source"]["sha256"] == plan["source_files"]["docs/MODEL.md"], "real project record")
                    usages[case] = [row["completion"]["usage"] for row in by_kind("response")]
                    require(all(0 < row["completion_tokens"] <= tokens for row in usages[case]), "budget usage")
                    require(all(row["max_tokens"] == tokens for row in by_kind("request")), "output contract")
            finally: db.close()
    require(totals == dict(preflights=4, requests=4, responses=4, tools=2, capability_attempts=3) and
        len({row["task_id"] for row in states.values()}) == 3, "exact workload/isolation")
    require(response_ids == {row["id"] for row in ends}, "ledger/service request identity")
    active = {name.split("/")[2] for name in data if name.startswith("tasks/tasks/")}
    require(active == {row["task_id"] for row in states.values()}, "rejected task created")
    require(get("final-retention.json")["active_tasks"] == 3 and get("final-retention.json")["archived_keys"] == 0, "plan error archived data")
    reservations = [name.rsplit("/",1)[0] for name in data if name.startswith("owned-generations/generations/") and name.endswith("/reservation.json")]
    require(len(reservations) == 2 and sorted(read(p + "/reservation.json")["kind"] for p in reservations) == ["backend","gateway"], "service starts")
    backend = next(p for p in reservations if read(p + "/reservation.json")["kind"] == "backend")
    terminal = read(backend + "/run/terminal.json")
    require(terminal["state"] == "STOPPED" and terminal["cleanup_confirmed"] and terminal["worker"]["attempts"] == 1 and
        terminal["worker"]["restarts"] == 0 and terminal["guard"]["status"] == "COMPLETED", "backend shutdown")
    events = [json.loads(line) for line in data[backend + "/run/lease/events.jsonl"].splitlines()]
    samples = [row for row in events if row["event"] in ("preflight","monitor")]
    require(events[0]["event"] == "lease_acquired" and events[-1]["event"] == "lease_released" and
        events[-1]["status"] == "COMPLETED" and all(not row["conflicts"] for row in samples) and
        all(0 <= right["monotonic"] - left["monotonic"] <= 10 for left,right in zip(events,events[1:])), "GPU guard")
    gpu_seconds = events[-1]["monotonic"] - events[0]["monotonic"]
    require(gpu_seconds <= 420, "GPU budget")
    post=read("postflight/result.json")
    require(post["passed"] and post["default_unchanged"] and post["day_plans_unchanged"] and post["lock_available"] and
        post["sealed_generations"] == 2, "postflight")
    return dict(status="ACCEPT_PROJECT_TASK_ERRORS_SHORT", application_cpu_tests=106, driver_tests=2,
        original_fixture_errors=18, targeted_correction_tests=18, targeted_driver_tests=2,
        original_live_driver_failed=True, offline_finish_commands=5, offline_finish_seconds=finish["seconds"],
        cpu_file_operation_kills=23,
        model_starts=1, gateway_starts=1, completed_tasks=2, ready_after_auth_error=1,
        duplicate_submissions=3, nonexecuting_resumes=2, classified_errors=6, **totals,
        response_usage=usages, grammar_reaped=4, owned_exited=post["owned_exited"], controls=result["counts"]["controls"]+len(finish["commands"]),
        seconds=result["seconds"], gpu_seconds=gpu_seconds, gpu_observations=len(samples),
        successful_request_end_seconds_sum=timing_sum, successful_server_response_count=4,
        timing_source="backend_request_end_logs", final_metrics_collected=False,
        cleanup_confirmed=True, default_changed=False, long_run=False, performance_acceptance=False)


def postflight():
    s.configure(); plan = s.check()
    output = BASE / "postflight"; output.mkdir(mode=0o700)
    s.a.require_idle(output / "gpu.json")
    lock = s.a.guard.common_lock(); info = lock.stat()
    require([info.st_dev, info.st_ino] == [66305, 3294067] and s.a.lock_available(lock), "original GPU lock")
    result = subprocess.run(["systemctl", "--user", "show",
        "soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service", "-p", "LoadState",
        "-p", "ActiveState", "-p", "SubState", "-p", "MainPID"], capture_output=True, text=True, timeout=10, check=True)
    require(dict(line.split("=", 1) for line in result.stdout.splitlines()) ==
        dict(LoadState="masked", ActiveState="inactive", SubState="dead", MainPID="0"), "SOAR changed")
    (output / "soar-unit.txt").write_text(result.stdout)
    containers = subprocess.run(["sudo", "-n", "docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=10, check=True)
    require(not containers.stdout.strip(), "foreign containers; preserve them")
    (output / "containers.txt").write_text(containers.stdout)
    namespace = BASE / "owned-generations"
    with s.retained.store.locked(namespace) as (path, meta):
        rows = s.retained.store.inventory(path, meta)
    require(len(rows) == 2 and all(row["state"] == "SEALED" for row in rows), "unsealed generation")
    owners = {}
    for path in (namespace / "generations").iterdir():
        reservation, seal = s.pkg.read(path / "reservation.json"), s.pkg.read(path / "seal.json")
        for owner in [reservation["owner"], *seal["proof"]["owners"]]:
            owners[owner["pid"], owner["start_ticks"], owner["boot_id"]] = owner
    for path in (BASE / "execution").glob("*/owner.json"):
        owner = s.pkg.read(path); owners[owner["pid"], owner["start_ticks"], owner["boot_id"]] = owner
    require(all(not s.retained.r.owner_live(value) for value in owners.values()), "owned process remained")
    value = dict(passed=True, owners=list(owners.values()), owned_exited=len(owners), inventory=rows,
        sealed_generations=len(rows), lock_available=True, lock_identity=[info.st_dev, info.st_ino],
        default_unchanged=True, day_plans_unchanged=True, days=plan["days"], new_models_or_http=0)
    store.write_new(output / "result.json", store.encode(value))
    return {key: value[key] for key in ("passed", "owned_exited", "sealed_generations", "day_plans_unchanged")}


def collect():
    paths = [BASE / "plan.json", BASE / "live-command.log", BASE / "historical-verification.json"]
    for folder in ("cpu-01", "cpu-fix-01", "cpu-driver", "cpu-finish", "offline-finish", "execution", "owned-generations", "tasks", "postflight"):
        paths.extend(path for path in (BASE / folder).rglob("*") if path.is_file())
    paths.extend((BASE / "fixtures").glob("*/commands.json"))
    data = {str(path.relative_to(BASE)): path.read_bytes() for path in sorted(set(paths)) if not path.is_symlink()}
    data["archiver/error_report.py"] = Path(__file__).read_bytes()
    private = s.previous.BASE / "model-private"
    policy = s.pkg.read(private / "policy-private.json")
    secrets = [row["api_key"] for row in policy["clients"]] + [s.pkg.read(private / "clients.json")["gateway_token"]]
    secrets.append(s.pkg.read(BASE / "private/denied.json")["api_key"])
    for name, raw in data.items():
        require(all(secret.encode() not in raw for secret in secrets) and not any(
            b"-----BEGIN " + kind + b"PRIVATE KEY-----" in raw for kind in (b"", b"RSA ", b"EC ", b"ENCRYPTED ")),
            "private credential in public records: " + name)
    plan = s.pkg.read(BASE / "plan.json"); local = {}
    for path in (Path(plan["package"]["package"]) / "q4t", Path(plan["gateways"][0]["package"]) / "nginx"):
        local[str(path)] = dict(bytes=path.stat().st_size, sha256=s.pkg.sha(path))
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
    s.pkg.save(DEST / "manifest.json", dict(files={name: dict(bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest()) for name, raw in data.items()}, local_identities=local,
        archive=dict(bytes=len(packed), sha256=hashlib.sha256(packed).hexdigest())))
    s.pkg.save(DEST / "result.json", result)
    return dict(records=len(data), local_identities=len(local), compressed_bytes=len(packed), result=result)


def verify():
    manifest = s.pkg.read(DEST / "manifest.json"); packed = (DEST / "records.tar.gz").read_bytes()
    require(len(packed) == manifest["archive"]["bytes"] and hashlib.sha256(packed).hexdigest() ==
            manifest["archive"]["sha256"], "archive changed")
    data = {}
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as source:
        for member in source:
            require(member.isfile() and member.name not in data and member.name in manifest["files"], "unexpected member")
            raw = source.extractfile(member).read(); expected = manifest["files"][member.name]
            require(len(raw) == expected["bytes"] and hashlib.sha256(raw).hexdigest() == expected["sha256"], "record changed")
            data[member.name] = raw
    require(data.keys() == manifest["files"].keys() and evaluate(data) == s.pkg.read(DEST / "result.json"),
            "offline outcome differs")
    return dict(passed=True, records=len(data), local_identities=len(manifest["local_identities"]), new_models_or_http=0)


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(); parser.add_argument("action", choices=("postflight", "archive", "verify"))
    print(json.dumps(globals()[parser.parse_args().action]()))
