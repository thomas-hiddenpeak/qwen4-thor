"""Archive and independently rebuild whole-store handoff evidence without HTTP or GPU."""
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
from tools.project_tasks import backup_stage as s, engine, store, retention as r, backup as b

BASE = s.BASE
DEST = ROOT / "docs/evidence/serve-task-backup-20261104"
require = store.require


def evaluate(data):
    read = lambda name: json.loads(data[name])
    get = lambda name: read("execution/" + name)
    cli = lambda name: json.loads(get(name + ".json")["stdout"])
    plan, result = read("plan.json"), get("result.json")
    for folder, count in (("cpu-01", 88), ("cpu-driver", 4)):
        cpu = read(folder + "/result.json")
        require(cpu["passed"] and cpu["tests_run"] == count and cpu["errors"] == cpu["failures"] == 0 and
            cpu["models_started"] == cpu["listeners_started"] == cpu["gpu_started"] == 0, "CPU gate")
        for name, checksum in cpu["sources"].items():
            require(hashlib.sha256(data[folder + "/source/" + name]).hexdigest() == checksum ==
                plan["source_files"][name], "CPU source binding")
    kills = read("cpu-01/result.json")["file_operation_kills"]
    require(len(kills) == 23 and all(row["returncode"] == -9 for row in kills), "CPU file-operation death proof")
    require(result["status"] == "PASS_PROJECT_TASK_BACKUP_SHORT" and result["cleanup_confirmed"] and
        result["counts"]["generations"] == 4 and result["counts"]["negatives"] == 2 and
        result["counts"]["controls"] <= 64 and result["seconds"] <= 480 and not result["long_run"], "real bounded result")
    metrics = get("final-metrics.json")
    require(metrics["q4t_requests_total"] == metrics["q4t_requests_success_total"] == 4 and
        metrics["q4t_requests_error_total"] == metrics["q4t_requests_aborted_total"] == 0 and
        metrics["q4t_structured_workers_started_total"] == metrics["q4t_structured_workers_reaped_total"] == 4 and
        all(metrics[key] == 0 for key in s.a.EMPTY), "HTTP/grammar cleanup")
    timing = {}
    for line in get("final-00.json")["response"]["body"].splitlines():
        if line.startswith(("q4t_response_seconds_sum ", "q4t_response_seconds_count ")):
            key, value = line.split(); timing[key] = float(value)
    require(timing["q4t_response_seconds_count"] == 4 and timing["q4t_response_seconds_sum"] > 0, "actual timing")
    snapshot = read("snapshot/manifest.json"); capture = snapshot["capture"]
    snapshot_sha = store.digest(snapshot)
    require(snapshot["format"] == b.SNAPSHOT and snapshot["max_bytes"] == plan["snapshot_max_bytes"] and
        read("snapshot/seal.json") == dict(format=b.SNAPSHOT, snapshot_sha256=snapshot_sha) and
        cli("snapshot-completed")["snapshot_sha256"] == snapshot_sha, "snapshot seal")
    for name, info in capture["files"].items():
        raw = data["snapshot/data/" + name]
        require(len(raw) == info["bytes"] and hashlib.sha256(raw).hexdigest() == info["sha256"] and
            data["source-tasks/" + name] == raw, "snapshot/source original bytes")
    require({name.removeprefix("snapshot/data/") for name in data if name.startswith("snapshot/data/")} == set(capture["files"]), "snapshot file set")
    source_tree = get("source-final-tree.json")
    require({name: row for name, row in source_tree["files"].items() if name != "handoff.json"} == capture["files"] and
        source_tree["directories"] == capture["directories"], "source changed since snapshot")
    transfer = get("restore-plan.json"); transfer_sha = store.digest(transfer)
    require(transfer["snapshot_sha256"] == snapshot_sha and transfer["source_capture_sha256"] == store.digest(capture) and
        transfer["source_identity"] == capture["directories"]["."] and transfer["source"] == capture["source"], "restore plan source binding")
    work = ".q4t-restore-" + transfer_sha
    job, ready, receipt = read(work + "/job.json"), read(work + "/ready.json"), read(work + "/receipt.json")
    require(job["plan"] == transfer and job["plan_sha256"] == transfer_sha and ready["identity"] != transfer["source_identity"], "restore work/destination")
    require(read("source-tasks/handoff.json") == dict(format=b.RESTORE, plan_sha256=transfer_sha,
        destination=transfer["destination"], identity=ready["identity"], ready_sha256=store.digest(ready), snapshot_sha256=snapshot_sha), "durable source fence")
    require(cli("restore-completed") == dict(receipt, already_applied=False) and
        cli("restore-repeat") == dict(receipt, already_applied=True), "restore continuation/idempotency")
    source_meta, target_meta = read("source-tasks/store.json"), read("restored-tasks/store.json")
    require(capture["store"] == source_meta and target_meta == dict(source_meta,
        root_device=ready["identity"][0], root_inode=ready["identity"][1]) and source_meta["max_tasks"] == 3, "store identity preserved")
    placement = read("restored-tasks/restored.json"); history = placement["history"]
    require(placement["format"] == b.PLACEMENT and placement["store_id"] == source_meta["store_id"] and
        placement["root"] == transfer["destination"] and placement["identity"] == ready["identity"] and len(history) == 1 and
        history[0]["payload"]["plan_sha256"] == transfer_sha and history[0]["previous"] == "0" * 64 and
        history[0]["digest"] == store.digest([1, history[0]["payload"], "0" * 64]), "new store placement chain")
    target_tree = get("target-final-tree.json")
    require(target_tree["directories"]["."] == ready["identity"], "target physical root")
    for name, row in target_tree["files"].items():
        raw = data["restored-tasks/" + name]
        require(len(raw) == row["bytes"] and hashlib.sha256(raw).hexdigest() == row["sha256"], "final target bytes")
    parent = BASE / "offline-verification"; parent.mkdir(exist_ok=True, mode=0o700)
    names = {"known": store.task_name("alpha", "known"), "unknown": store.task_name("beta", "unknown"),
             "archived": store.task_name("beta", "archived")}
    ledgers, publics, specs = {}, {}, {}
    with tempfile.TemporaryDirectory(dir=parent) as temporary:
        for label, prefix, key in (("known-before", "source-tasks/tasks/", "known"),
            ("unknown-before", "source-tasks/tasks/", "unknown"), ("beta-original", "source-tasks/retention/payloads/", "archived"),
            ("alpha-final", "restored-tasks/tasks/", "known"), ("unknown-final", "restored-tasks/tasks/", "unknown")):
            case = Path(temporary) / label; case.mkdir(mode=0o700)
            folder = case / names[key]; folder.mkdir(mode=0o700); prefix += names[key] + "/"
            for name, raw in data.items():
                if name.startswith(prefix):
                    relative = name.removeprefix(prefix); require("/" not in relative, "nested task file")
                    store.write_new(folder / relative, raw)
            db = store.connection(folder)
            try:
                ledger = store.Ledger(folder, db, source_meta["store_id"])
                state = engine.replay(ledger); public = engine.public(ledger, state)
                ledgers[label], publics[label], specs[label] = ledger.events(), public, ledger.spec
                export_label = "unknown-before" if label == "unknown-final" else label
                exported = get(export_label + "-ledger.json")
                require(ledger.meta == exported["meta"] and ledger.events() == exported["events"] and
                    r.sha(folder / "ledger.db") == exported["database_sha256"], "actual ledger vs exports")
                require(ledger.spec["runtime_sources"] == {name: plan["source_files"][name] for name in ledger.spec["runtime_sources"]}, "task runtime provenance")
                if label in ("known-before", "unknown-before"):
                    saved = capture["summary"]["tasks"][names[key]]
                    require(public["revision"] == saved["revision"] and store.digest(public) == saved["public_sha256"] and
                        store.digest(ledger.meta) == saved["meta_sha256"], "snapshot task states")
                    restored = get(key + "-restored-ledger.json")
                    require(restored["meta"] == ledger.meta and restored["events"] == ledger.events() and
                        restored["database_sha256"] == exported["database_sha256"], "initial restored events changed")
                    require(ready["tree"]["files"]["tasks/" + names[key] + "/ledger.db"]["sha256"] == exported["database_sha256"], "prepared task changed")
            finally: db.close()
        catalog_values = {}
        for location in ("source-tasks", "restored-tasks"):
            folder = Path(temporary) / location; folder.mkdir(mode=0o700)
            store.write_new(folder / "catalog.db", data[location + "/retention/catalog.db"])
            with r.catalog(folder) as db:
                require(r.get_record(db, "control", "operation") is None, "retention operation incomplete")
                policy = r.get_record(db, "settings", "policy")
                rows = dict(db.execute("SELECT name,value FROM keys"))
            require(set(rows) == {names["archived"]} and policy["limits"] ==
                {key: value for key, value in plan["task_policy"].items() if key != "max_tasks"}, "retention key/capacity policy")
            catalog_values[location] = json.loads(rows[names["archived"]])
        original, final = catalog_values["source-tasks"], catalog_values["restored-tasks"]
        original_state = r.key_state(original, source_meta, names["archived"])
        final_state = r.key_state(final, target_meta, names["archived"])
        require(original["base"] == final["base"] and original["events"] == [] and
            [row["kind"] for row in final["events"]] == ["placement", "prune"] and
            original_state["payload"] == "retained" and final_state["payload"] == "purged", "permanent key/placement/prune history")
        require(final["events"][0]["payload"]["plan_sha256"] == transfer_sha and
            final["events"][0]["payload"]["directory_identity"] == ready["tree"]["directories"]["retention/payloads/" + names["archived"]], "placement directory identity")
        for filename, identity in final["events"][0]["payload"]["file_identities"].items():
            require(identity == ready["tree"]["files"]["retention/payloads/" + names["archived"] + "/" + filename]["identity"], "restored payload file identity")
        require(not any(name.startswith("restored-tasks/retention/payloads/") for name in data), "pruned target payload remained")
    known, unknown, completed, archived = (publics[key] for key in ("known-before", "unknown-final", "alpha-final", "beta-original"))
    require(known["state"] == "TOOLS_PENDING" and known["saved_tool_results"] == known["requests"] == 1 and
        ledgers["alpha-final"][:len(ledgers["known-before"])] == ledgers["known-before"], "known result/history not preserved")
    require(unknown == publics["unknown-before"] == cli("unknown-held") and unknown["state"] == "REQUEST_UNKNOWN" and
        unknown["requests"] == unknown["preflight_checks"] == 1 and unknown["tool_attempts"] == 0 and
        not any(name.startswith("restored-tasks/tasks/" + names["unknown"] + "/wire-") for name in data), "unknown replay or output promotion")
    totals = dict(preflights=0, intents=0, responses=0, tools=0, capabilities=0); usages = {}
    for case, client, tokens in (("alpha-final", "alpha", 512), ("beta-original", "beta", 64), ("unknown-final", "beta", 64)):
        events = ledgers[case]; by_kind = lambda kind: [row["payload"] for row in events if row["kind"] == kind]
        for target, kind in (("preflights", "preflight_started"), ("intents", "request"), ("responses", "response"), ("tools", "tool_started"), ("capabilities", "run")):
            totals[target] += len(by_kind(kind))
        require(specs[case]["binding"] == plan["bindings"][client] and specs[case]["max_tokens"] == tokens, "client budget/binding")
        if case == "unknown-final": continue
        value = publics[case]
        require(value["state"] == "COMPLETED" and value["outcome"] == "ANSWERED" and value["requests"] == 2 and
            value["saved_tool_results"] == value["tool_attempts"] == 1 and "Qwen4ExpForConditionalGeneration" in value["answer"] and
            "[docs/MODEL.md:L1-L24]" in value["answer"], "actual task quality")
        tool = json.loads(by_kind("tool_result")[0]["content"])
        require(tool["ok"] and tool["source"]["sha256"] == plan["source_files"]["docs/MODEL.md"] and
            by_kind("tool_started")[0]["arguments"] == dict(path="docs/MODEL.md", start_line=1, line_count=24), "real project read")
        usages[client] = [row["completion"]["usage"] for row in by_kind("response")]
        require(all(0 < row["completion_tokens"] <= tokens for row in usages[client]), "actual output tokens")
    require(totals == dict(preflights=5, intents=5, responses=4, tools=2, capabilities=4), "task workload totals")
    require(cli("alpha-duplicate") == dict(completed, created=False) and cli("unknown-duplicate") == dict(unknown, created=False), "ordinary duplicates")
    require(cli("archived-answer")["answer"] == archived["answer"], "restored archive answer")
    for name, payload in (("archived-retained-duplicate", "retained"), ("archived-purged-duplicate", "purged"), ("archived-purged-resume", "purged")):
        value = cli(name); require(value["state"] == "ARCHIVED" and value["payload_state"] == payload and
            "answer" not in value and get(name + ".json")["returncode"] == 2, "archive duplicate executed")
    for name in ("retired-source-resume", "retired-source-submit"):
        value = get(name + ".json"); require(value["returncode"] == 1 and not value["stdout"] and
            json.loads(value["stderr"]) == dict(error="ValueError", automatic_retry=False), "retired source admitted work")
    for label, point in (("known", "tool_result"), ("unknown", "request"), ("snapshot", "before_seal"), ("restore", "source_fenced")):
        marker, killed = get(label + "-kill/boundary.json"), get(label + "-kill/killed.json")
        require(marker["pid"] == killed["owner"]["pid"] and marker["kind"] == point and killed["returncode"] == -9, "real checkpoint/death")
    observations = [json.loads(line) for line in data["execution/known-kill/read-observations.jsonl"].splitlines()]
    require(len(observations) == 1 and observations[0]["result"]["ok"], "known actual tool observation")
    require("q4t_requests_total 3\n" in get("before-copy-00.json")["response"]["body"] and
        "q4t_requests_total 3\n" in get("after-unknown-00.json")["response"]["body"], "copy/unknown triggered generation")
    reservations = [name.rsplit("/", 1)[0] for name in data if name.startswith("owned-generations/generations/") and name.endswith("/reservation.json")]
    require(len(reservations) == 2 and sorted(read(path + "/reservation.json")["kind"] for path in reservations) == ["backend", "gateway"], "service starts")
    backend = next(path for path in reservations if read(path + "/reservation.json")["kind"] == "backend")
    terminal = read(backend + "/run/terminal.json")
    require(terminal["state"] == "STOPPED" and terminal["cleanup_confirmed"] and terminal["worker"]["attempts"] == 1 and
        terminal["worker"]["restarts"] == 0 and terminal["guard"]["status"] == "COMPLETED", "backend shutdown")
    events = [json.loads(line) for line in data[backend + "/run/lease/events.jsonl"].splitlines()]
    samples = [row for row in events if row["event"] in ("preflight", "monitor")]
    require(events[0]["event"] == "lease_acquired" and events[-1]["event"] == "lease_released" and
        events[-1]["status"] == "COMPLETED" and all(not row["conflicts"] for row in samples) and
        all(0 <= right["monotonic"] - left["monotonic"] <= 10 for left, right in zip(events, events[1:])), "guard continuity")
    gpu_seconds = events[-1]["monotonic"] - events[0]["monotonic"]; require(gpu_seconds <= 420, "GPU window")
    post = read("postflight/result.json")
    require(post["passed"] and post["default_unchanged"] and post["day_plans_unchanged"] and post["lock_available"] and
        post["sealed_generations"] == 2 and post["task_inventory"] == get("final-retention.json") and
        hashlib.sha256(data["restored-tasks/retention/catalog.db"]).hexdigest() == post["catalog_sha256"], "postflight identity/cleanup")
    return dict(status="ACCEPT_PROJECT_TASK_BACKUP_SHORT", application_cpu_tests=88, driver_tests=4, cpu_file_operation_kills=23,
        model_starts=1, gateway_starts=1, completed_tasks=2, unknown_tasks=1, restored_stores=1,
        actual_process_kills=4, duplicate_submissions=4, nonexecuting_resumes=2, retired_source_refusals=2, repeated_restores=1,
        **totals, response_usage=usages, grammar_reaped=4, owned_exited=post["owned_exited"], controls=result["counts"]["controls"],
        seconds=result["seconds"], gpu_seconds=gpu_seconds, gpu_observations=len(samples),
        successful_server_response_seconds_sum=timing["q4t_response_seconds_sum"], successful_server_response_count=4,
        cleanup_confirmed=True, default_changed=False, long_run=False, performance_acceptance=False,
        stale_snapshot_activation=False, missing_source_disaster_recovery=False)


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
    snap = s.pkg.read(s.SNAPSHOT / "manifest.json")
    with b.quiescent(s.SOURCE, allow_handoff=True) as (root, meta):
        require(b.capture(root, meta) == snap["capture"], "retired source changed")
    require((s.SOURCE / "handoff.json").exists(), "source not retired")
    value = dict(task_inventory=task_state, catalog_sha256=s.pkg.sha(s.TASKS / "retention/catalog.db"),
        task_store_identity=r.identity(s.TASKS.stat()), passed=True, owners=list(owners.values()), owned_exited=len(owners), inventory=rows,
        sealed_generations=len(rows), lock_available=True, lock_identity=[info.st_dev, info.st_ino],
        default_unchanged=True, day_plans_unchanged=True, days=plan["days"], new_models_or_http=0)
    store.write_new(output / "result.json", store.encode(value))
    return {key: value[key] for key in ("passed", "owned_exited", "sealed_generations", "day_plans_unchanged")}


def collect():
    paths = [BASE / "plan.json"]
    for folder in ("cpu-01", "cpu-driver", "execution", "owned-generations", "source-tasks", "restored-tasks", "snapshot", "postflight"):
        paths.extend(path for path in (BASE / folder).rglob("*") if path.is_file())
    for work in BASE.glob(".q4t-restore-*"):
        paths.extend(path for path in work.rglob("*") if path.is_file())
    data = {str(path.relative_to(BASE)): path.read_bytes() for path in sorted(set(paths)) if not path.is_symlink()}
    data["archiver/backup_report.py"] = Path(__file__).read_bytes()
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
