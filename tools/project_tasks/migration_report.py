"""Rebuild historical-to-current task handoffs and preserve the failed first phase."""
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
from tools.project_tasks import migration_continue as c, engine, migration, store

BASE = c.ORIGINAL
DEST = ROOT / "docs/evidence/serve-task-migration-20261104"
pkg, require = c.pkg, store.require


def evaluate(data):
    read = lambda name: json.loads(data[name])
    get = lambda name: read("continuation/execution/" + name)
    original, plan = read("plan.json"), read("continuation/plan.json")
    initial, result = read("execution/result.json"), get("result.json")
    require(initial["status"] == "FAILED" and initial["cleanup_confirmed"] and
        b"retention capacity exhausted" in data["execution/front-2-wrapper.log"] and
        result["status"] == "PASS_PROJECT_TASK_MIGRATION_CONTINUATION" and result["cleanup_confirmed"] and
        result["seconds"] <= 360 and result["counts"]["controls"] <= 64, "phase outcomes")
    require(read("cpu-continuation-import/result.json")["tests_run"] == 0 and
            not read("cpu-continuation-import/result.json")["passed"], "pre-test import failure lost")
    for folder, count, sources in (("cpu-01", 49, original["source_files"]),
        ("cpu-driver", 3, original["source_files"]), ("cpu-continuation", 5, plan["source_files"])):
        cpu = read(folder + "/result.json")
        require(cpu["passed"] and cpu["tests_run"] == count and cpu["errors"] == cpu["failures"] == 0, "CPU gates")
        for name, digest in cpu["sources"].items():
            require(hashlib.sha256(data[folder + "/source/" + name]).hexdigest() == digest and
                    sources[name] == digest, "CPU source identity")
    old_metrics = read("execution/beta-before-kill-00.json")["metrics"]
    metrics = get("final-metrics.json")
    for values in (old_metrics, metrics):
        require(values["q4t_requests_total"] == values["q4t_requests_success_total"] == 2 and
            values["q4t_requests_error_total"] == values["q4t_requests_aborted_total"] == 0 and
            values["q4t_structured_workers_started_total"] == values["q4t_structured_workers_reaped_total"] == 2 and
            all(values[key] == 0 for key in c.stage.a.EMPTY), "actual server outcomes")
    # The failed driver did not reach its final counter update. Preserve both facts.
    require(initial["counts"]["generations"] == 0 and result["counts"]["generations"] == 2,
            "driver attempt counter history changed")
    timing = {}
    for phase, name in (("original", "execution/beta-before-kill-00.json"),
                         ("continuation", "continuation/execution/final-00.json")):
        values = {}
        for line in read(name)["response"]["body"].splitlines():
            if line.startswith(("q4t_response_seconds_sum ", "q4t_response_seconds_count ")):
                key, value = line.split(); values[key] = float(value)
        require(values["q4t_response_seconds_count"] == 2 and values["q4t_response_seconds_sum"] > 0, "timing missing")
        timing[phase] = values
    revoked = get("revoked-key.json")
    require(revoked["http_status"] == 401 and revoked["no_generation"] and
            read("continuation/execution/revoked-key-wire.bin")["error"]["code"] == revoked["code"], "actual key revocation")
    totals = dict(requests=0, responses=0, tools=0, preflights=0, capabilities=0, migrations=0)
    usages = {}; preserved_files = 0
    parent = BASE / "offline-verification"; parent.mkdir(exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        for client in ("alpha", "beta"):
            row = original["clients"][client]
            before = read("execution/" + client + "-before-ledger.json")
            final = get(client + "-final-ledger.json")
            task_id = final["meta"]["task_id"]
            require(final["meta"] == before["meta"] and final["events"][:len(before["events"])] == before["events"],
                    "historical meta/events rewritten")
            states = {}
            for version, exported, prefix in (("before", before, "continuation/prior-tasks/" + client + "/tasks/"),
                ("after", final, "historical-" + client + "/.q4t-work/tasks/tasks/")):
                prefix += task_id + "/"
                folder = Path(temp) / (client + "-" + version) / task_id; folder.mkdir(parents=True, mode=0o700)
                for name, raw in data.items():
                    if name.startswith(prefix):
                        child = name[len(prefix):]; require("/" not in child, "nested task artifact")
                        store.write_new(folder / child, raw)
                require(hashlib.sha256(data[prefix + "ledger.db"]).hexdigest() == exported["database_sha256"],
                        "actual SQLite identity")
                db = store.connection(folder)
                try:
                    ledger = store.Ledger(folder, db, exported["meta"]["store_id"])
                    require(ledger.meta == exported["meta"] and ledger.events() == exported["events"], "SQLite/export differs")
                    state = engine.replay(ledger); public = engine.public(ledger, state)
                    states[version] = state
                    if version == "before":
                        require(state["phase"] == "TOOLS_PENDING" and state["requests"] == state["tool_results"] == 1 and
                            state["migrations"] == 0 and ledger.spec["runtime_sources"] == row["runtime"]["sources"],
                            "historical saved result/runtime")
                    else:
                        observed = json.loads(get(client + "-completed.json")["stdout"])
                        duplicate = json.loads(get(client + "-duplicate.json")["stdout"])
                        require(public == observed and duplicate == dict(public, created=False) and
                            public["state"] == "COMPLETED" and public["outcome"] == "ANSWERED" and
                            public["requests"] == 2 and public["tool_attempts"] == public["saved_tool_results"] ==
                            public["migrations"] == 1 and public["max_tokens"] == row["tokens"], "migrated business outcome")
                        require("Qwen4ExpForConditionalGeneration" in public["answer"] and
                                "[docs/MODEL.md:L1-L24]" in public["answer"], "actual business quality")
                        effective = state["execution_spec"]
                        require(effective["binding"] == row["binding"] and effective["runtime_sources"] ==
                            {name: plan["source_files"][name] for name in effective["runtime_sources"]}, "new execution binding")
                finally: db.close()
            for name, digest in row["runtime"]["sources"].items():
                require(hashlib.sha256(data["historical-" + client + "/" + name]).hexdigest() == digest,
                        "historical implementation bytes")
            added = final["events"][len(before["events"]):]
            require(sum(event["kind"] == "request" for event in added) == 1 and
                    not any(event["kind"] in ("tool_started", "tool_result") for event in added), "known result reexecuted")
            events = final["events"]
            by_kind = lambda kind: [event["payload"] for event in events if event["kind"] == kind]
            for key, kind in (("requests", "request"), ("responses", "response"), ("tools", "tool_started"),
                              ("preflights", "preflight_started"), ("capabilities", "run"), ("migrations", "migration")):
                totals[key] += len(by_kind(kind))
            handoff = by_kind("migration")[0]
            migration_plan = get(client + "/migration-plan.json")
            require(handoff == dict(plan=migration_plan, plan_sha256=store.digest(migration_plan)) and
                migration_plan["source_spec"] == before["meta"]["spec"] and
                migration_plan["revision"] == before["events"][-1]["digest"], "reviewed migration receipt")
            moved = json.loads(get(client + "-migrated.json")["stdout"])
            again = json.loads(get(client + "-migration-duplicate.json")["stdout"])
            require(again == dict(moved, migration_applied=False, already_applied=True) and
                    moved["requests"] == moved["saved_tool_results"] == 1 and moved["no_execution"], "migration repeated/reset")
            for filename, identity in migration_plan["artifacts"].items():
                raw = data["continuation/prior-tasks/" + client + "/tasks/" + task_id + "/" + filename]
                require(len(raw) == identity["bytes"] and hashlib.sha256(raw).hexdigest() == identity["sha256"],
                        "original snapshot differs from reviewed plan")
                if filename != "ledger.db":
                    require(data["historical-" + client + "/.q4t-work/tasks/tasks/" + task_id + "/" + filename] == raw,
                            "old response/check bytes changed")
                preserved_files += 1
            migration.same_identity(row["old_binding"], row["binding"])
            require((row["old_binding"] != row["binding"]) == (client == "alpha"), "wrong key rotated")
            marker = read("execution/" + client + "/boundary.json")
            killed = read("execution/" + client + "/killed.json")
            require(marker["task_id"] == task_id and marker["pid"] == killed["owner"]["pid"] and
                    marker["kind"] == "tool_result" and killed["returncode"] == -9, "historical app death missing")
            observations = [json.loads(line) for line in data["execution/" + client + "/read-observations.jsonl"].splitlines()]
            tool = json.loads(by_kind("tool_result")[0]["content"])
            require(len(observations) == 1 and observations[0]["result"]["source"] == tool["source"] and
                tool["source"]["sha256"] == original["source_files"]["docs/MODEL.md"] and
                tool["source"]["citation"] == "[docs/MODEL.md:L1-L24]", "real saved tool identity")
            usages[client] = [row["completion"]["usage"] for row in by_kind("response")]
            require(all(0 < usage["completion_tokens"] <= row["tokens"] for usage in usages[client]), "output usage")
    require(totals == dict(requests=4, responses=4, tools=2, preflights=3, capabilities=4, migrations=2), "workload changed")
    guards = {}
    for phase in ("", "continuation/"):
        folders = [name.rsplit("/", 1)[0] for name in data if name.startswith(phase + "owned-generations/generations/")
                   and name.endswith("/reservation.json")]
        require(len(folders) == 2, "actual generation count")
        backend = next(path for path in folders if read(path + "/reservation.json")["kind"] == "backend")
        terminal = read(backend + "/run/terminal.json")
        require(terminal["state"] == "STOPPED" and terminal["cleanup_confirmed"] and
            terminal["worker"]["attempts"] == 1 and terminal["worker"]["restarts"] == 0 and
            terminal["guard"]["status"] == "COMPLETED", "backend cleanup")
        events = [json.loads(line) for line in data[backend + "/run/lease/events.jsonl"].splitlines()]
        samples = [event for event in events if event["event"] in ("preflight", "monitor")]
        require(events[0]["event"] == "lease_acquired" and events[-1]["event"] == "lease_released" and
            events[-1]["status"] == "COMPLETED" and all(not event["conflicts"] for event in samples) and
            all(0 <= right["monotonic"] - left["monotonic"] <= 10 for left, right in zip(events, events[1:])), "guard continuity")
        seconds = events[-1]["monotonic"] - events[0]["monotonic"]
        require(seconds <= (300 if phase else 420), "GPU envelope")
        guards[phase.rstrip("/") or "original"] = dict(seconds=seconds, observations=len(samples))
    post = read("postflight/result.json")
    require(post["passed"] and post["default_unchanged"] and post["day_plans_unchanged"] and
            post["lock_available"] and post["sealed_generations"] == 4, "postflight")
    return dict(status="ACCEPT_PROJECT_TASK_MIGRATION_SHORT", application_cpu_tests=49,
        original_driver_tests=3, continuation_driver_tests=5, import_failure_retained=True,
        original_capacity_failure_retained=True, original_raw_counter=0, original_successful_requests=2,
        cumulative_model_starts=2, cumulative_gateway_starts=2, gateway_prestart_refusals=1,
        completed_tasks=2, application_kills=2, new_tools_during_continuation=0,
        duplicate_migrations=2, duplicate_submissions=2, revoked_key_status=401, **totals,
        preserved_original_files=preserved_files, response_usage=usages, guards=guards,
        original_seconds=initial["seconds"], continuation_seconds=result["seconds"],
        original_controls=initial["counts"]["controls"], continuation_controls=result["counts"]["controls"],
        timing=timing, owned_exited=post["owned_exited"], cleanup_confirmed=True,
        default_changed=False, long_run=False, performance_acceptance=False)


def postflight():
    c.configure(); plan = c.check()
    output = BASE / "postflight"; output.mkdir(mode=0o700)
    c.a.require_idle(output / "gpu.json")
    lock = c.a.guard.common_lock(); info = lock.stat()
    require([info.st_dev, info.st_ino] == [66305, 3294067] and c.a.lock_available(lock), "original GPU lock")
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
    owners, inventories = {}, []
    for root in (BASE, c.BASE):
        namespace = root / "owned-generations"
        with c.stage.retained.store.locked(namespace) as (path, meta): rows = c.stage.retained.store.inventory(path, meta)
        require(len(rows) == 2 and all(row["state"] == "SEALED" for row in rows), "unsealed generation")
        inventories.extend(rows)
        for path in (namespace / "generations").iterdir():
            reservation, seal = pkg.read(path / "reservation.json"), pkg.read(path / "seal.json")
            for owner in [reservation["owner"], *seal["proof"]["owners"]]:
                owners[owner["pid"], owner["start_ticks"], owner["boot_id"]] = owner
        for path in (root / "execution").glob("*/owner.json"):
            owner = pkg.read(path); owners[owner["pid"], owner["start_ticks"], owner["boot_id"]] = owner
    require(all(not c.stage.retained.r.owner_live(value) for value in owners.values()), "owned process remained")
    value = dict(passed=True, owners=list(owners.values()), owned_exited=len(owners), inventory=inventories,
        sealed_generations=len(inventories), lock_available=True, lock_identity=[info.st_dev, info.st_ino],
        default_unchanged=True, day_plans_unchanged=True, days=plan["days"], new_models_or_http=0)
    store.write_new(output / "result.json", store.encode(value))
    return {key: value[key] for key in ("passed", "owned_exited", "sealed_generations", "day_plans_unchanged")}


def collect():
    paths = [BASE / "plan.json", c.BASE / "plan.json"]
    for folder in ("cpu-01", "cpu-driver", "cpu-continuation-import", "cpu-continuation", "execution",
                   "owned-generations", "postflight", "continuation/execution", "continuation/prior-tasks",
                   "continuation/owned-generations", "historical-alpha/tools", "historical-beta/tools",
                   "historical-alpha/.q4t-work/tasks", "historical-beta/.q4t-work/tasks"):
        paths.extend(path for path in (BASE / folder).rglob("*") if path.is_file())
    data = {str(path.relative_to(BASE)): path.read_bytes() for path in sorted(set(paths)) if not path.is_symlink()}
    data["archiver/migration_report.py"] = Path(__file__).read_bytes()
    private = c.stage.previous.BASE / "model-private"
    policy = pkg.read(private / "policy-private.json"); rotated = pkg.read(BASE / "private/rotated-policy.json")
    secrets = [row["api_key"] for row in policy["clients"] + rotated["clients"]] + [pkg.read(private / "clients.json")["gateway_token"]]
    for name, raw in data.items():
        require(all(secret.encode() not in raw for secret in secrets) and not any(
            b"-----BEGIN " + kind + b"PRIVATE KEY-----" in raw for kind in (b"", b"RSA ", b"EC ", b"ENCRYPTED ")),
            "private credential in public records: " + name)
    plan = pkg.read(BASE / "plan.json"); local = {}
    for path in [Path(plan["package"]["package"]) / "q4t", *(Path(row["package"]) / "nginx" for row in plan["gateways"])]:
        local[str(path)] = dict(bytes=path.stat().st_size, sha256=pkg.sha(path))
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
    pkg.save(DEST / "manifest.json", dict(files={name: dict(bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest()) for name, raw in data.items()}, local_identities=local,
        archive=dict(bytes=len(packed), sha256=hashlib.sha256(packed).hexdigest())))
    pkg.save(DEST / "result.json", result)
    return dict(records=len(data), local_identities=len(local), compressed_bytes=len(packed), result=result)


def verify():
    manifest = pkg.read(DEST / "manifest.json"); packed = (DEST / "records.tar.gz").read_bytes()
    require(len(packed) == manifest["archive"]["bytes"] and hashlib.sha256(packed).hexdigest() ==
            manifest["archive"]["sha256"], "archive changed")
    data = {}
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as source:
        for member in source:
            require(member.isfile() and member.name not in data and member.name in manifest["files"], "unexpected member")
            raw = source.extractfile(member).read(); expected = manifest["files"][member.name]
            require(len(raw) == expected["bytes"] and hashlib.sha256(raw).hexdigest() == expected["sha256"], "record changed")
            data[member.name] = raw
    require(data.keys() == manifest["files"].keys() and evaluate(data) == pkg.read(DEST / "result.json"),
            "offline outcome differs")
    return dict(passed=True, records=len(data), local_identities=len(manifest["local_identities"]), new_models_or_http=0)


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(); parser.add_argument("action", choices=("postflight", "archive", "verify"))
    print(json.dumps(globals()[parser.parse_args().action]()))
