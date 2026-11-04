"""Archive and independently rebuild client budget tasks without HTTP or GPU."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import budget_stage as s, engine, store

BASE = s.BASE
DEST = ROOT / "docs/evidence/serve-task-budgets-20261104"
require = store.require


def evaluate(data):
    read = lambda name: json.loads(data[name])
    get = lambda name: read("execution/" + name)
    plan, result = read("plan.json"), get("result.json")
    for folder, count in (("cpu-01", 31), ("cpu-driver", 3)):
        cpu = read(folder + "/result.json")
        require(cpu["passed"] and cpu["tests_run"] == count and
                cpu["errors"] == cpu["failures"] == 0, "CPU gate")
        for name, digest in cpu["sources"].items():
            require(hashlib.sha256(data[folder + "/source/" + name]).hexdigest() == digest and
                    plan["source_files"][name] == digest, "CPU/source binding")
    require(result["status"] == "PASS_PROJECT_TASK_BUDGET_SHORT" and result["cleanup_confirmed"] and
        result["counts"]["generations"] == 4 and result["counts"]["negatives"] == 2 and
        result["counts"]["controls"] <= 64 and result["seconds"] <= 480 and not result["long_run"],
        "bounded real result")
    metrics = get("final-metrics.json")
    require(metrics["q4t_requests_total"] == metrics["q4t_requests_success_total"] == 4 and
        metrics["q4t_requests_error_total"] == metrics["q4t_requests_aborted_total"] == 0 and
        metrics["q4t_structured_workers_started_total"] == metrics["q4t_structured_workers_reaped_total"] == 4 and
        all(metrics[key] == 0 for key in s.a.EMPTY), "server outcomes/idle cleanup")
    timing = {}
    for line in get("final-00.json")["response"]["body"].splitlines():
        if line.startswith(("q4t_response_seconds_sum ", "q4t_response_seconds_count ")):
            name, value = line.split(); timing[name] = float(value)
    require(timing["q4t_response_seconds_count"] == 4 and timing["q4t_response_seconds_sum"] > 0,
            "bounded server timing missing")
    cases, totals, usages = {}, dict(preflights=0, requests=0, responses=0, tools=0, capabilities=0), {}
    parent = BASE / "offline-verification"; parent.mkdir(exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        for case, client, tokens in (("negative", "beta", 512), ("alpha", "alpha", 512), ("beta", "beta", 64)):
            exported = get(case + "-ledger.json")
            task_id = exported["meta"]["task_id"]
            prefix = "tasks/tasks/" + task_id + "/"
            folder = Path(temp) / task_id; folder.mkdir(mode=0o700)
            for name, raw in data.items():
                if name.startswith(prefix):
                    child = name[len(prefix):]
                    require("/" not in child, "nested task entry")
                    store.write_new(folder / child, raw)
            require(hashlib.sha256(data[prefix + "ledger.db"]).hexdigest() ==
                    exported["database_sha256"], "actual SQLite identity")
            db = store.connection(folder)
            try:
                ledger = store.Ledger(folder, db, exported["meta"]["store_id"])
                require(ledger.meta == exported["meta"] and ledger.events() == exported["events"] and
                    ledger.spec["binding"] == plan["bindings"][client] and ledger.spec["max_tokens"] == tokens,
                    "SQLite/export/client binding")
                require(ledger.spec["runtime_sources"] == {name: plan["source_files"][name]
                    for name in ledger.spec["runtime_sources"]}, "task runtime identity")
                state = engine.replay(ledger); public = engine.public(ledger, state)
                events = ledger.events()
                by_kind = lambda kind: [event["payload"] for event in events if event["kind"] == kind]
                checks, requests = by_kind("preflight_started"), by_kind("request")
                responses, tools = by_kind("response"), by_kind("tool_started")
                for key, value in (("preflights", len(checks)), ("requests", len(requests)),
                                   ("responses", len(responses)), ("tools", len(tools)),
                                   ("capabilities", len(by_kind("run")))):
                    totals[key] += value
                require(all(row["max_tokens"] == tokens for row in checks + requests), "output budget changed")
                if case == "negative":
                    observed = json.loads(get("beta-above-limit.json")["stdout"])
                    duplicate = json.loads(get("beta-rejected-duplicate.json")["stdout"])
                    require(public == observed and duplicate == dict(public, created=False) and
                        public["state"] == "PREFLIGHT_REJECTED" and len(checks) == 1 and
                        not requests and not tools and not responses and not list(folder.glob("wire-*.bin")) and
                        public["preflight_error"] == dict(category="remote_error", http_status=400,
                            code="client_output_limit_exceeded", param="max_tokens"), "negative generated/retried")
                    error = read(prefix + "check-001.bin")["error"]
                    require(error["code"] == "client_output_limit_exceeded" and error["param"] == "max_tokens",
                            "actual rejection wire")
                else:
                    observed = json.loads(get(case + "-completed.json")["stdout"])
                    if case == "beta": require(observed.pop("created") is True, "client key collision")
                    duplicate = json.loads(get(case + "-duplicate.json")["stdout"])
                    require(public == observed and duplicate == dict(public, created=False) and
                        public["state"] == "COMPLETED" and public["outcome"] == "ANSWERED" and
                        len(requests) == len(responses) == 2 and len(tools) == public["saved_tool_results"] == 1 and
                        len(checks) == (3 if case == "alpha" else 2), "task/duplicate outcome")
                    require("Qwen4ExpForConditionalGeneration" in public["answer"] and
                            "[docs/MODEL.md:L1-L24]" in public["answer"], "actual answer quality")
                    require(tools[0]["name"] == "read_project_record" and tools[0]["arguments"] ==
                        dict(path="docs/MODEL.md", start_line=1, line_count=24), "actual tool query")
                    tool = json.loads(by_kind("tool_result")[0]["content"])
                    require(tool["ok"] and tool["source"]["sha256"] == plan["source_files"]["docs/MODEL.md"] and
                            tool["source"]["citation"] == "[docs/MODEL.md:L1-L24]", "real document identity")
                    usages[case] = [row["completion"]["usage"] for row in responses]
                    require(all(0 < row["completion_tokens"] <= tokens for row in usages[case]), "output usage")
                    if case == "alpha":
                        marker, killed = get("approved/boundary.json"), get("approved/killed.json")
                        stopped = json.loads(get("alpha-stopped.json")["stdout"])
                        require(marker["task_id"] == task_id and marker["pid"] == killed["owner"]["pid"] and
                            killed["returncode"] == -9 and marker["kind"] == "preflight_result" and
                            stopped["state"] == "PREFLIGHT_APPROVED" and stopped["requests"] == 0 and
                            stopped["preflight_checks"] == 1 and len(by_kind("preflight_reset")) == 1 and
                            checks[0]["request_id"] not in {row["request_id"] for row in requests},
                            "pre-crash approval reused or death proof missing")
                cases[case] = public
            finally:
                db.close()
    require(totals == dict(preflights=6, requests=4, responses=4, tools=2, capabilities=4), "actual workload envelope")
    require(cases["alpha"]["task_id"] != cases["beta"]["task_id"], "same business key not isolated")
    for name in ("after-negative-00.json", "approved-no-generation-00.json"):
        body = get(name)["response"]["body"]
        require("q4t_requests_total 0\n" in body and "q4t_structured_workers_started_total 0\n" in body,
                "preflight created generation or grammar work")
    reservations = [name.rsplit("/", 1)[0] for name in data if name.startswith("owned-generations/generations/")
                    and name.endswith("/reservation.json")]
    require(len(reservations) == 2 and sorted(read(path + "/reservation.json")["kind"]
        for path in reservations) == ["backend", "gateway"], "service start count")
    backend = next(path for path in reservations if read(path + "/reservation.json")["kind"] == "backend")
    terminal = read(backend + "/run/terminal.json")
    require(terminal["state"] == "STOPPED" and terminal["cleanup_confirmed"] and
        terminal["worker"]["attempts"] == 1 and terminal["worker"]["restarts"] == 0 and
        terminal["guard"]["status"] == "COMPLETED", "backend terminal")
    events = [json.loads(line) for line in data[backend + "/run/lease/events.jsonl"].splitlines()]
    samples = [row for row in events if row["event"] in ("preflight", "monitor")]
    require(events[0]["event"] == "lease_acquired" and events[-1]["event"] == "lease_released" and
        events[-1]["status"] == "COMPLETED" and all(not row["conflicts"] for row in samples) and
        all(0 <= right["monotonic"] - left["monotonic"] <= 10 for left, right in zip(events, events[1:])),
        "GPU guard continuity")
    gpu_seconds = events[-1]["monotonic"] - events[0]["monotonic"]
    require(gpu_seconds <= 420, "GPU envelope")
    post = read("postflight/result.json")
    require(post["passed"] and post["default_unchanged"] and post["day_plans_unchanged"] and
            post["lock_available"] and post["sealed_generations"] == 2, "postflight")
    return dict(status="ACCEPT_PROJECT_TASK_BUDGET_SHORT", cpu_tests=31, driver_tests=3,
        model_starts=1, gateway_starts=1, completed_tasks=2, rejected_tasks=1,
        actual_application_kills=1, duplicate_submissions=3, grammar_reaped=4, **totals,
        response_usage=usages, owned_exited=post["owned_exited"], controls=result["counts"]["controls"],
        seconds=result["seconds"], gpu_seconds=gpu_seconds, gpu_observations=len(samples),
        successful_server_response_seconds_sum=timing["q4t_response_seconds_sum"],
        successful_server_response_count=4, cleanup_confirmed=True, default_changed=False,
        long_run=False, performance_acceptance=False, preflight_reserves_capacity=False)


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
    paths = [BASE / "plan.json"]
    for folder in ("cpu-01", "cpu-driver", "execution", "owned-generations", "tasks", "postflight"):
        paths.extend(path for path in (BASE / folder).rglob("*") if path.is_file())
    data = {str(path.relative_to(BASE)): path.read_bytes() for path in sorted(set(paths)) if not path.is_symlink()}
    data["archiver/budget_report.py"] = Path(__file__).read_bytes()
    private = s.previous.BASE / "model-private"
    policy = s.pkg.read(private / "policy-private.json")
    secrets = [row["api_key"] for row in policy["clients"]] + [s.pkg.read(private / "clients.json")["gateway_token"]]
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
