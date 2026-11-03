"""Archive and independently rebuild terminal retention evidence without HTTP or GPU."""
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
from tools.project_tasks import archive_stage as s, engine, store, retention as r

BASE = s.BASE
DEST = ROOT / "docs/evidence/serve-task-archive-20261104"
require = store.require


def evaluate(data):
    read = lambda name: json.loads(data[name])
    get = lambda name: read("execution/" + name)
    cli = lambda name: json.loads(get(name + ".json")["stdout"])
    plan, result = read("plan.json"), get("result.json")
    for folder, count, failures, errors, exceptions in (
            ("cpu-01", 69, 1, 0, {"tools/project_tasks/test_archive.py"}),
            ("cpu-driver", 5, 0, 1, {"tools/project_tasks/archive_stage.py", "tools/project_tasks/archive_driver_cpu.py"}),
            ("cpu-driver-fix", 2, 0, 0, set())):
        cpu = read(folder + "/result.json")
        require((cpu["tests_run"], cpu["failures"], cpu["errors"]) == (count, failures, errors) and
            cpu["passed"] == (failures + errors == 0) and
            cpu["models_started"] == cpu["listeners_started"] == cpu["gpu_started"] == 0, "CPU history/gates")
        for name, checksum in cpu["sources"].items():
            require(hashlib.sha256(data[folder + "/source/" + name]).hexdigest() == checksum and
                    (name in exceptions or plan["source_files"][name] == checksum), "CPU source binding")
    require(b'ValueError: wire/response identity' in data["cpu-01/tests.log"] and
            b"TypeError: 'FakeClient' object is not subscriptable" in data["cpu-driver/tests.log"] and
            b"test_exact_digest_stale_content_and_unknown_entries_refused" in data["cpu-driver/tests.log"], "initial failures/correction lost")
    kills = read("cpu-01/result.json")["archive_worker_kills"]
    require(len(kills) == 11 and all(row["returncode"] == -9 for row in kills), "CPU process-death proof")
    require(result["status"] == "PASS_PROJECT_TASK_ARCHIVE_SHORT" and result["cleanup_confirmed"] and
        result["counts"]["generations"] == 4 and result["counts"]["negatives"] == 5 and
        result["counts"]["controls"] <= 64 and result["seconds"] <= 480 and not result["long_run"], "bounded real result")
    metrics = get("final-metrics.json")
    require(metrics["q4t_requests_total"] == metrics["q4t_requests_success_total"] == 4 and
        metrics["q4t_requests_error_total"] == metrics["q4t_requests_aborted_total"] == 0 and
        metrics["q4t_structured_workers_started_total"] == metrics["q4t_structured_workers_reaped_total"] == 4 and
        all(metrics[key] == 0 for key in s.a.EMPTY), "HTTP/grammar outcome")
    timing = {}
    for line in get("final-00.json")["response"]["body"].splitlines():
        if line.startswith(("q4t_response_seconds_sum ", "q4t_response_seconds_count ")):
            key, value = line.split(); timing[key] = float(value)
    require(timing["q4t_response_seconds_count"] == 4 and timing["q4t_response_seconds_sum"] > 0, "actual response timing")
    meta = read("tasks/store.json")
    require(meta["max_tasks"] == 1 and not any(name.startswith("tasks/tasks/") for name in data), "active slot not released")
    parent = BASE / "offline-verification"; parent.mkdir(exist_ok=True, mode=0o700)
    totals = dict(preflights=0, requests=0, responses=0, tools=0, capabilities=0)
    usages = {}; keys = {}; answers = {}
    with tempfile.TemporaryDirectory(dir=parent) as temporary:
        catalog = Path(temporary) / "retention"; catalog.mkdir(mode=0o700)
        raw_catalog = data["tasks/retention/catalog.db"]
        store.write_new(catalog / "catalog.db", raw_catalog)
        with r.catalog(catalog) as db:
            exported = {table: dict(db.execute("SELECT name,value FROM " + table)) for table in ("settings", "keys", "control")}
        require(exported == get("catalog-export.json") and set(exported["control"]) == {"last"}, "actual catalog/export/completion")
        policy = json.loads(exported["settings"]["policy"])
        require(policy["store_id"] == meta["store_id"] and policy["root_identity"] ==
            [meta["root_device"], meta["root_inode"]] == read("postflight/result.json")["task_store_identity"] and
            policy["limits"] == {key: value for key, value in plan["task_policy"].items() if key != "max_tasks"} and
            not policy["automatic_prune"] and len(exported["keys"]) == 2, "store/policy/key identity")
        require(hashlib.sha256(raw_catalog).hexdigest() == read("postflight/result.json")["catalog_sha256"], "actual final catalog identity")
        charge = r.CATALOG_RESERVE
        for client, tokens in (("alpha", 512), ("beta", 64)):
            name = store.task_name(client, "same-key"); key = json.loads(exported["keys"][name]); keys[client] = key
            state = r.key_state(key, meta, name); base = key["base"]
            require(state["binding"] == plan["bindings"][client] and state["rekeys"] == 0 and
                state["payload"] == ("purged" if client == "alpha" else "retained"), "permanent key scope/payload state")
            require(base["files"] == get(client + "-original-files.json"), "original task file identities")
            archive_plan = get(client + "-archive-plan.json")
            require(base["archive_plan_sha256"] == store.digest(archive_plan) and archive_plan["files"] == base["files"] and
                base["purge_after_ns"] == base["created_ns"], "archive plan/test retention period")
            folder = Path(temporary) / name; folder.mkdir(mode=0o700)
            for filename, info in base["files"].items():
                raw = data["execution/saved-tasks/" + name + "/" + filename]
                require(len(raw) == info["bytes"] and hashlib.sha256(raw).hexdigest() == info["sha256"], "saved original bytes")
                store.write_new(folder / filename, raw)
                actual = "tasks/retention/payloads/" + name + "/" + filename
                require((actual not in data) if client == "alpha" else data[actual] == raw, "payload deletion/preservation")
            db = store.connection(folder)
            try:
                ledger = store.Ledger(folder, db, meta["store_id"])
                recorded = get(client + "-ledger.json")
                require(ledger.meta == recorded["meta"] and ledger.events() == recorded["events"] and
                    r.sha(folder / "ledger.db") == recorded["database_sha256"] == base["files"]["ledger.db"]["sha256"], "actual ledger/export")
                spec = ledger.spec
                require(spec["binding"] == plan["bindings"][client] and spec["max_tokens"] == tokens and
                    spec["runtime_sources"] == {name: plan["source_files"][name] for name in spec["runtime_sources"]} and
                    r.business_contract(spec) == base["contract_sha256"] and store.digest(ledger.meta) == base["meta_sha256"], "task/runtime/contract binding")
                completed = engine.public(ledger, engine.replay(ledger)); observed = cli(client + "-completed")
                require(observed.pop("created") and completed == observed and completed["state"] == "COMPLETED" and
                    completed["outcome"] == "ANSWERED" and completed["requests"] == completed["preflight_checks"] == 2 and
                    completed["saved_tool_results"] == completed["tool_attempts"] == completed["runs"] == 1, "original completion")
                answer = completed.pop("answer"); answers[client] = answer
                require(completed == base["terminal"] and store.digest(answer) == base["answer_sha256"] and
                    "Qwen4ExpForConditionalGeneration" in answer and "[docs/MODEL.md:L1-L24]" in answer, "answer/terminal preservation")
                require(cli(client + "-archived-answer")["answer"] == answer, "archived answer lookup")
                by_kind = lambda kind: [row["payload"] for row in ledger.events() if row["kind"] == kind]
                for target, kind in (("preflights", "preflight_started"), ("requests", "request"),
                    ("responses", "response"), ("tools", "tool_started"), ("capabilities", "run")):
                    totals[target] += len(by_kind(kind))
                require(by_kind("tool_started")[0]["arguments"] == dict(path="docs/MODEL.md", start_line=1, line_count=24), "real query")
                tool = json.loads(by_kind("tool_result")[0]["content"])
                require(tool["ok"] and tool["source"]["sha256"] == plan["source_files"]["docs/MODEL.md"], "real project source")
                usages[client] = [row["completion"]["usage"] for row in by_kind("response")]
                require(all(0 < row["completion_tokens"] <= tokens for row in usages[client]), "output limits")
            finally: db.close()
            for suffix, payload in (("retained", "retained"), *(([("purged", "purged")]) if client == "alpha" else [])):
                submitted, resumed = cli(client + "-" + suffix + "-submit"), cli(client + "-" + suffix + "-resume")
                require(submitted == dict(resumed, created=False) and resumed["state"] == "ARCHIVED" and
                    resumed["payload_state"] == payload and resumed["terminal_revision"] == base["terminal"]["revision"] and
                    "answer" not in resumed, "duplicate was not a non-executing marker")
                require(get(client + "-" + suffix + "-submit.json")["returncode"] ==
                    get(client + "-" + suffix + "-resume.json")["returncode"] == 2, "archived CLI returned completed")
            charge += len(exported["keys"][name].encode()) + (base["payload_bytes"] if client == "beta" else 0)
    require(totals == dict(preflights=4, requests=4, responses=4, tools=2, capabilities=2), "workload totals")
    for kind, point in (("archive", "payload_moved"), ("prune", "payload_file_removed")):
        marker, killed = get(kind + "-kill/boundary.json"), get(kind + "-kill/killed.json")
        operation = get(kind + "-kill/pending-operation.json")
        require(marker["pid"] == killed["owner"]["pid"] and killed["returncode"] == -9 and
            marker["kind"] == point and marker["operation_sha256"] == store.digest(operation) and
            marker["plan_sha256"] == store.digest(get("alpha-" + kind + "-plan.json")), "actual retention process death")
        require(cli(kind + "-pending")["operation_sha256"] == marker["operation_sha256"] and
            not cli(kind + "-finished")["already_finished"] and cli(kind + "-finish-repeat")["already_finished"] and
            cli(kind + "-apply-repeat")["already_applied"], "explicit operation recovery/idempotency")
        if kind == "prune": require(operation["after"] == keys["alpha"], "prune future key lost")
        else: require(operation["after"]["base"] == keys["alpha"]["base"], "archive future key lost")
    for name in ("archive-pending-submit", "prune-pending-submit", "alpha-question-conflict", "alpha-budget-conflict", "permanent-key-capacity"):
        row = get(name + ".json")
        require(row["returncode"] == 1 and not row["stdout"] and
            json.loads(row["stderr"]) == dict(error="ValueError", automatic_retry=False), "local refusal changed")
    final = cli("final-retention")
    require((final["active_tasks"], final["archived_keys"], final["retained_payloads"], final["max_active_tasks"],
        final["max_keys"], final["archive_bytes"]) == (0, 2, 1, 1, 2, charge), "final capacity/key accounting")
    require("q4t_requests_total 2\n" in get("alpha-purged-idle-00.json")["response"]["body"], "alpha recovery or duplicate generated")
    reservations = [name.rsplit("/", 1)[0] for name in data if name.startswith("owned-generations/generations/") and name.endswith("/reservation.json")]
    require(len(reservations) == 2 and sorted(read(path + "/reservation.json")["kind"] for path in reservations) == ["backend", "gateway"], "service start envelope")
    backend = next(path for path in reservations if read(path + "/reservation.json")["kind"] == "backend")
    terminal = read(backend + "/run/terminal.json")
    require(terminal["state"] == "STOPPED" and terminal["cleanup_confirmed"] and terminal["worker"]["attempts"] == 1 and
        terminal["worker"]["restarts"] == 0 and terminal["guard"]["status"] == "COMPLETED", "backend stopped")
    events = [json.loads(line) for line in data[backend + "/run/lease/events.jsonl"].splitlines()]
    samples = [row for row in events if row["event"] in ("preflight", "monitor")]
    require(events[0]["event"] == "lease_acquired" and events[-1]["event"] == "lease_released" and
        events[-1]["status"] == "COMPLETED" and all(not row["conflicts"] for row in samples) and
        all(0 <= right["monotonic"] - left["monotonic"] <= 10 for left, right in zip(events, events[1:])), "guard continuity")
    seconds = events[-1]["monotonic"] - events[0]["monotonic"]; require(seconds <= 420, "GPU window")
    post = read("postflight/result.json")
    require(post["passed"] and post["default_unchanged"] and post["day_plans_unchanged"] and post["lock_available"] and
        post["sealed_generations"] == 2 and post["task_inventory"] == final, "final cleanup/identity")
    return dict(status="ACCEPT_PROJECT_TASK_ARCHIVE_SHORT", application_cpu_tests=69, driver_tests=5,
        initial_cpu_failures=1, initial_driver_errors=1, cpu_retention_kills=11,
        model_starts=1, gateway_starts=1, completed_tasks=2, archived_tasks=2, purged_payloads=1,
        real_retention_kills=2, duplicate_submissions=3, duplicate_resumes=3, pending_refusals=2,
        conflicts=2, key_capacity_refusals=1, grammar_reaped=4, **totals, response_usage=usages,
        archive_bytes=charge, catalog_reserved_bytes=r.CATALOG_RESERVE, owned_exited=post["owned_exited"],
        controls=result["counts"]["controls"], seconds=result["seconds"], gpu_seconds=seconds,
        gpu_observations=len(samples), successful_server_response_seconds_sum=timing["q4t_response_seconds_sum"],
        successful_server_response_count=4, cleanup_confirmed=True, default_changed=False, long_run=False,
        performance_acceptance=False, filesystem_quota=False, permanent_keys=True)


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
    with r.catalog(s.TASKS / "retention") as db:
        require(r.get_record(db, "control", "operation") is None, "task retention operation remained")
    task_state = r.status(s.TASKS)
    value = dict(task_inventory=task_state, catalog_sha256=s.pkg.sha(s.TASKS / "retention/catalog.db"),
        task_store_identity=r.identity(s.TASKS.stat()), passed=True, owners=list(owners.values()), owned_exited=len(owners), inventory=rows,
        sealed_generations=len(rows), lock_available=True, lock_identity=[info.st_dev, info.st_ino],
        default_unchanged=True, day_plans_unchanged=True, days=plan["days"], new_models_or_http=0)
    store.write_new(output / "result.json", store.encode(value))
    return {key: value[key] for key in ("passed", "owned_exited", "sealed_generations", "day_plans_unchanged")}


def collect():
    paths = [BASE / "plan.json"]
    for folder in ("cpu-01", "cpu-driver", "cpu-driver-fix", "execution", "owned-generations", "tasks", "postflight"):
        paths.extend(path for path in (BASE / folder).rglob("*") if path.is_file())
    data = {str(path.relative_to(BASE)): path.read_bytes() for path in sorted(set(paths)) if not path.is_symlink()}
    data["archiver/archive_report.py"] = Path(__file__).read_bytes()
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
