"""Read-only audit of the completed, frozen numeric-admission GPU stage.

No inference, subprocesses, imports of runtime tools, or writes to raw evidence.
Run only after the root stage driver and outer GPU guard have both exited.
This audit does not calculate throughput acceptance or authorize deployment.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import statistics


BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
EXPECTED_PLAN_SHA256 = (
    "dbdfea2061ad866db0f6ba83bb8106f654d753958c4484b8ca249f7999b14902")
COUNTERS = (
    "queries hits candidates compared matched no_match incomplete d0_mismatch "
    "invalid exhausted indexed peak_bytes object_bytes init_ns append_ns "
    "query_ns release_ns numeric_checks classified_tokens numeric_rejected "
    "numeric_ns").split()


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(path.read_text())


def jsonl(path):
    with path.open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def fields(line):
    return dict(re.findall(r"(\w+)=(\S+)", line))


def describe(values):
    return dict(count=len(values), minimum=min(values) if values else None,
                median=statistics.median(values) if values else None,
                maximum=max(values) if values else None, sum=sum(values))


def memfields(text):
    return {k: int(v) * 1024 for k, v in
            re.findall(r"^(\w+):\s*(\d+) kB$", text, re.M)}


class Audit:
    def __init__(self):
        self.checks = Counter()
        self.errors = []
        self.environment_errors = []
        self.measurement_gaps = []

    def check(self, condition, label, environment=False):
        self.checks["environment" if environment else "evidence"] += 1
        if not condition:
            target = self.environment_errors if environment else self.errors
            target.append(label)

    def protect(self, label, fn, environment=False):
        try:
            return fn()
        except (OSError, KeyError, ValueError, TypeError, sqlite3.Error) as error:
            self.check(False, label + ": " + repr(error), environment)
            return None

    def measured(self, condition, label):
        self.checks["measurement"] += 1
        if not condition:
            self.measurement_gaps.append(label)


def verify_manifest(audit, manifest, base=None):
    changed = []
    for name, expected in manifest.items():
        path = Path(name) if base is None else base / name
        if base is not None and (Path(name).is_absolute() or ".." in Path(name).parts):
            changed.append(name)
            audit.check(False, "unsafe sealed path: " + name)
            continue
        actual = sha(path) if path.is_file() else None
        audit.check(actual == expected, "content identity: " + str(path))
        if actual != expected:
            changed.append(name)
    return dict(checked=len(manifest), changed=changed)


def audit_identities(audit, plan, plan_path, run, state):
    expected = EXPECTED_PLAN_SHA256
    audit.check(sha(plan_path) == expected, "frozen plan hash")
    audit.check(state.get("plan_sha256") == expected, "driver bound plan hash")
    audit.check(sha(run / "plan.json") == expected, "raw run plan copy")
    result = {"plan_sha256": sha(plan_path)}
    result["frozen_files"] = verify_manifest(audit, plan["files"])
    seal = read(run / "files.json")
    result["raw_seal_sha256"] = sha(run / "files.json")
    result["raw_files"] = verify_manifest(audit, seal, run)
    actual_names = {str(p.relative_to(run)) for p in run.rglob("*")
                    if p.is_file() and p.name != "files.json"}
    audit.check(set(seal) == actual_names, "raw seal inventory exact")
    result["unsealed_files"] = sorted(actual_names - set(seal))
    audit.check(sha(Path(plan["candidate"])) == plan["candidate_sha256"],
                "actual candidate program")
    audit.check(sha(Path(plan["parent"])) == plan["parent_sha256"],
                "frozen parent program")
    audit.check(sha(Path(plan["artifact"]) / "manifest.json") ==
                plan["subset_manifest_sha256"], "subset manifest identity")
    metadata_changed = []
    for name, expected_metadata in plan["model_file_metadata"].items():
        s = Path(name).stat()
        actual = dict(size=s.st_size, mtime_ns=s.st_mtime_ns,
                      device=s.st_dev, inode=s.st_ino)
        audit.check(actual == expected_metadata, "model metadata: " + name)
        if actual != expected_metadata:
            metadata_changed.append(name)
    result["model_metadata"] = dict(checked=len(plan["model_file_metadata"]),
                                     changed=metadata_changed)
    snapshot = Path(plan["source_snapshot"])
    sources = [p for p in snapshot.rglob("*") if p.is_file()]
    changed = []
    auxiliary = {}
    for source in sources:
        relative = source.relative_to(snapshot)
        if str(relative) == "worktree.patch":
            # Snapshot metadata, not a file expected at checkout/worktree.patch.
            # Executable source identities are bound individually in plan.files.
            auxiliary[str(relative)] = sha(source)
            continue
        current = ROOT / relative
        equal = current.is_file() and sha(source) == sha(current)
        audit.check(equal, "source snapshot: " + str(relative))
        if not equal:
            changed.append(str(relative))
    result["source_snapshot"] = dict(checked=len(sources) - len(auxiliary),
                                      changed=changed, auxiliary=auxiliary)
    reused = []
    for record in plan["reused_evidence"]["records"]:
        same = sha(Path(record["path"])) == record["sha256"]
        audit.check(same, "reused evidence: " + record["path"])
        reused.append(dict(record, identity_matches=same))
    result["reuse_records"] = reused
    static = read(BASE / "audit-runtime-static.json")
    audit.check(static["source_and_build_flags_equal"], "static dependency audit")
    audit.check(not static["blocking_findings"], "static runtime findings")
    for name, identity in static["key_dependency_identity"].items():
        audit.check(sha(ROOT / name) == identity["current"],
                    "unchanged reused dependency: " + name)
    result["static_audit_sha256"] = sha(BASE / "audit-runtime-static.json")
    result["reuse_boundary"] = static["reuse_decision"]
    return result


def audit_environment(audit, plan, state, lease):
    exit_record = read(lease / "exit.json")
    events = list(jsonl(lease / "events.jsonl"))
    audit.check(state.get("status") != "ENV_ABORT", "driver ENV_ABORT", True)
    audit.check(not state.get("monitor_failures"), "resource monitor failure", True)
    audit.check(not state.get("cleanup_errors"), "owned process cleanup error", True)
    audit.check(exit_record.get("status") not in
                ("ENV_ABORT", "LEASE_BUSY", "RECOVERY_REQUIRED"),
                "outer guard invalid environment", True)
    audit.check(not exit_record.get("cleanup_error"), "outer cleanup failure", True)
    audit.measured(exit_record.get("status") == "COMPLETED" and
                   exit_record.get("child_returncode") == 0,
                   "outer guard/driver clean exit")
    audit.check(exit_record.get("compute_backend") == "nvml", "NVML backend")
    acquired = [e for e in events if e["event"] == "lease_acquired"]
    released = [e for e in events if e["event"] == "lease_released"]
    started = [e for e in events if e["event"] == "child_started"]
    audit.check(len(acquired) == len(released) == len(started) == 1,
                "one continuous shared exclusive lease")
    monitor = [e for e in events if e["event"] in ("preflight", "monitor")]
    audit.check(bool(monitor), "missing environment monitor", True)
    for event in monitor:
        audit.check(not event.get("conflicts"), "competing workload", True)
    nvml_times, used_bytes, nvml_count = [], [], 0
    for row in jsonl(lease / "nvml-queries.jsonl"):
        nvml_count += 1
        audit.check(row.get("status") == "OK" and row.get("returncode") == 0,
                    "NVML query failure: " + str(nvml_count), True)
        messages = [json.loads(line) for line in row.get("stdout", "").splitlines()
                    if line.strip().startswith("{")]
        times = [r["monotonic"] for r in messages if "monotonic" in r]
        audit.check(bool(times), "missing NVML timestamps", True)
        if times:
            nvml_times.append(min(times))
        used_bytes.extend(r["used_bytes"] for r in row.get("process_memory", []) or []
                          if r.get("used_bytes") is not None)
    audit.check(nvml_count > 0, "no NVML samples", True)
    gaps = [b - a for a, b in zip(nvml_times, nvml_times[1:])]
    audit.check(all(v >= 0 for v in gaps), "NVML timestamps reversed", True)
    return dict(exit=exit_record, nvml_queries=nvml_count,
                nvml_query_start_gap_seconds=describe(gaps),
                nvml_process_used_bytes=describe(used_bytes),
                monitor_samples=len(monitor),
                lease_begin=acquired[0] if acquired else None,
                lease_end=released[0] if released else None,
                identities={name: sha(lease / name) for name in
                            ["exit.json", "events.jsonl", "nvml-queries.jsonl"]},
                boundary="Polling cannot exclude sub-interval or invisible interference.")


def audit_resources(audit, plan, state, run, server_pids):
    times, wall_times, globals_ = [], [], {}
    process_max = {}
    for row in jsonl(run / "unified-memory.jsonl"):
        times.append(row["monotonic"])
        wall_times.append(row["unix_seconds"])
        values = memfields(row["meminfo"])
        for key in ["MemAvailable", "MemFree", "Cached", "Buffers", "SwapFree"]:
            globals_.setdefault(key, []).append(values[key])
        for pid, process in row["processes"].items():
            if int(pid) not in server_pids:
                continue
            values = memfields(process["status"])
            info = process_max.setdefault(pid, dict(samples=0))
            info["samples"] += 1
            for key in ["VmRSS", "VmHWM", "VmPin", "VmLck"]:
                if key in values:
                    info[key] = max(info.get(key, 0), values[key])
    audit.check(bool(times), "resource sample stream missing", True)
    gaps = [b - a for a, b in zip(times, times[1:])]
    limit = plan["resource_sample_max_age_seconds"]
    audit.check(all(0 <= gap <= limit for gap in gaps),
                "resource sampling reversed/stale gap", True)
    if times:
        audit.check(abs(wall_times[0] - state["started"]) <= limit,
                    "resource start coverage", True)
        audit.check(abs(state["finished"] - wall_times[-1]) <= limit,
                    "resource end coverage", True)
    audit.measured(set(map(int, process_max)) == set(server_pids),
                   "every server observed by unified RAM sampler; short failed starts may be missed")
    return dict(samples=len(times), gap_seconds=describe(gaps),
                system_bytes={k: describe(v) for k, v in globals_.items()},
                server_process_max_bytes=process_max,
                boundary="System, RSS and NVML memory overlap; do not sum their peaks. "
                         "Sampling can miss transients and does not isolate gate RAM delta.")


def read_responses(audit, folder):
    responses = {}
    for path in folder.rglob("responses.json"):
        for row in read(path):
            request = row["response_id"]
            audit.check(request not in responses, "duplicate response ID: " + request)
            responses[request] = row
    return responses


def partial_server(step, folder):
    """Retain a failed/aborted arm without inventing missing completion data."""
    log = folder / "server.log"
    text = log.read_text() if log.is_file() else ""
    raw_count = 0
    for database in folder.rglob("benchmark_data.db"):
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            raw_count += db.execute("SELECT COUNT(*) FROM result").fetchone()[0]
    if step["kind"] == "lifecycle":
        # Submitted payloads can exceed responses when cancellation/failure
        # occurs; server admissions are authoritative for actual generation.
        raw_count = len(list(folder.glob("suffix-*-request.json")))
    pid_file = folder / "server.pid"
    return dict(kind=step["kind"], head=step.get("head"), suffix=step["suffix"],
                complete=False, admitted=len(re.findall(r"event=admitted ", text)),
                raw_generation_requests=raw_count,
                pid=int(pid_file.read_text()) if pid_file.is_file() else None,
                table=[], requests=[],
                exit=read(folder / "exit.json") if (folder / "exit.json").is_file() else None)


def audit_server(audit, plan, step, folder):
    name = step["name"]
    text = (folder / "server.log").read_text()
    argv = read(folder / "server-command.json")["argv"]
    expected_program = plan["parent"] if name == "performance-parent-plain" else plan["candidate"]
    audit.check(argv[0] == expected_program, name + ": actual program path")
    suffix = "--mtp-suffix" in argv
    audit.check(suffix == step["suffix"], name + ": actual suffix mode")
    audit.check(("--mtp-subset-head" in argv) == (step.get("head") == "subset"),
                name + ": actual head mode")
    exit_record = read(folder / "exit.json")
    audit.check(exit_record["server"] == 0 and
                exit_record["http_output_checks_passed"], name + ": clean HTTP exit")
    admitted = len(re.findall(r"event=admitted ", text))
    audit.check(admitted == step["http_requests"], name + ": admitted budget")
    final = [json.loads(line.split("] ", 1)[1]) for line in text.splitlines()
             if line.startswith("[startup-reservation] ") and
             '"event":"finalized"' in line]
    audit.check(len(final) == 1 and final[0].get("status") == "PASS" and
                final[0].get("release_unknown", 0) == 0, name + ": ownership finalization")
    for marker in ["MTP init failed", "MTP trunk alloc failed", "MTP load failed",
                   "MTP allocation failed", "plain decode for this request",
                   "retained after failed drain", " disabled="]:
        audit.check(marker not in text, name + ": unexpected fallback: " + marker)
    db_rows, db_success, db_files = 0, 0, []
    for database in folder.rglob("benchmark_data.db"):
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            count, success = db.execute("SELECT COUNT(*), SUM(success) FROM result").fetchone()
        db_rows += count
        db_success += success or 0
        db_files.append(str(database.relative_to(folder)))
    responses = read_responses(audit, folder)
    lifecycle = step["kind"] == "lifecycle"
    if lifecycle:
        payloads = list(folder.glob("suffix-*-request.json"))
        audit.check(len(payloads) == 3 and db_rows == 0, name + ": lifecycle inference count")
        raw_count = len(payloads)
        result = read(folder / "results.json")
        audit.check(result["requests"] == 3 and result["control"] == result["recovery"],
                    name + ": control/recovery equality")
        audit.check(read(folder / "cancel-control-response.json")["status"] == 202,
                    name + ": explicit cancellation acknowledged")
        for label in ["after-cancel-health.json", "after-recovery-health.json"]:
            health = read(folder / label)
            audit.check(health["seq_slots_free"] == health["seq_slots_total"] == 1 and
                        health["gpu_healthy"], name + ": slot recovery " + label)
    else:
        raw_count = db_rows
        audit.check(db_rows == db_success == len(responses) == step["http_requests"],
                    name + ": raw database/response count and success")
    table = [fields(line) for line in text.splitlines()
             if line.startswith("[suffix-classes] ")]
    suffix_rows, accepts = [], {}
    for line in text.splitlines():
        if line.startswith("[mtp-accept] "):
            values = fields(line)
            request = values["request"]
            audit.check(request not in accepts, name + ": duplicate MTP histogram")
            accepts[request] = list(map(int, values["draft_counts"].split(",")))
        if not line.startswith("[suffix] "):
            continue
        values = fields(line)
        row = {key: int(values[key]) for key in COUNTERS}
        row["request"] = values["request"]
        for key in ["accepted", "neural_accepted"]:
            row[key] = list(map(int, values[key].split(",")))
        suffix_rows.append(row)
    if suffix:
        init = [r for r in table if r.get("event") == "init"]
        release = [r for r in table if r.get("event") == "release"]
        audit.check(len(table) == 2 and len(init) == len(release) == 1,
                    name + ": exactly one shared table lifecycle")
        if len(init) == len(release) == 1:
            audit.check(int(init[0]["bytes"]) == int(release[0]["bytes"]) == 248320 and
                        int(init[0]["cap_bytes"]) == 262144 and
                        int(init[0]["unknown"]) == 243,
                        name + ": full-model tokenizer/table domain")
            audit.check(int(init[0]["init_ns"]) >= 0 and
                        int(release[0]["release_ns"]) >= 0, name + ": table costs")
            positions = [text.find(marker) for marker in
                         ["[suffix-classes] event=init", "serving on port",
                          "shutdown complete (0 in-flight remaining)",
                          "[suffix-classes] event=release"]]
            audit.check(all(p >= 0 for p in positions) and positions == sorted(positions),
                        name + ": table lifetime encloses drained handlers")
        audit.check(len(suffix_rows) == admitted, name + ": suffix request summaries")
        audit.check("numeric_gate=1" in text, name + ": numeric gate enabled")
    else:
        audit.check(not table and not suffix_rows, name + ": OFF allocated no table/index")
    capacity = int(argv[argv.index("--max-len") + 1])
    for row in suffix_rows:
        request = row["request"]
        label = name + ": " + request
        audit.check(all(row[k] >= 0 for k in COUNTERS), label + ": negative counter")
        audit.check(row["invalid"] == row["exhausted"] == 0, label + ": disabled index")
        audit.check(row["queries"] == sum(row[k] for k in
                    ["hits", "no_match", "incomplete", "d0_mismatch", "numeric_rejected"]),
                    label + ": query outcome partition")
        audit.check(row["numeric_checks"] == row["hits"] + row["numeric_rejected"],
                    label + ": numeric admission partition")
        audit.check(3 * row["numeric_checks"] <= row["classified_tokens"] <=
                    64 * row["numeric_checks"], label + ": bounded classification")
        audit.check(row["numeric_ns"] <= row["query_ns"], label + ": nested timing")
        audit.check(row["peak_bytes"] == 1048576 + capacity * 4 and
                    row["peak_bytes"] <= 2 * 1024 * 1024 and row["object_bytes"] > 0,
                    label + ": bounded owned index payload")
        audit.check(row["candidates"] <= 4 * row["queries"] and
                    row["compared"] <= 64 * row["candidates"], label + ": search bounds")
        matched_queries = sum(row[k] for k in
                              ["hits", "incomplete", "d0_mismatch", "numeric_rejected"])
        audit.check(3 * matched_queries <= row["matched"] <= 64 * matched_queries,
                    label + ": all-match denominator and bounds")
        audit.check(len(row["accepted"]) == len(row["neural_accepted"]) == 4,
                    label + ": acceptance bins")
        total = [a + b for a, b in zip(row["accepted"], row["neural_accepted"])]
        audit.check(total == accepts.get(request), label + ": source/MTP histograms")
        audit.check(sum(total) == row["queries"] and
                    sum(row["accepted"]) == row["hits"], label + ": queries/hits/steps")
        committed = sum((i + 1) * count for i, count in enumerate(total))
        response = responses.get(request)
        prompt = response["actual_input"] if response else next(
            c["input_tokens"] for c in plan["development"] if c["id"] == "old-repeat-code")
        audit.check(response is not None or lifecycle, label + ": observed prompt identity")
        audit.check(row["indexed"] == prompt + committed,
                    label + ": index follows committed prefix, not socket delivery")
        if response:
            tail = response["actual_output"] - committed
            audit.check(0 <= tail <= 4, label + ": plain/EOS output tail")
        row["committed_prefix_tokens"] = committed
        row["matched_queries"] = matched_queries
    return dict(kind=step["kind"], head=step.get("head"), suffix=suffix, complete=True,
                admitted=admitted, raw_generation_requests=raw_count,
                raw_database_rows=db_rows, raw_databases=db_files,
                pid=int((folder / "server.pid").read_text()),
                table=table, requests=suffix_rows, exit=exit_record)


def aggregate(groups):
    rows = [r for group in groups.values() for r in group["requests"]]
    result = {key: sum(r[key] for r in rows) for key in COUNTERS
              if key not in ("indexed", "peak_bytes", "object_bytes")}
    result["requests"] = len(rows)
    for key in ["accepted", "neural_accepted"]:
        result[key] = [sum(r[key][i] for r in rows) for i in range(4)]
    matched_queries = sum(r["matched_queries"] for r in rows)
    result["mean_length_all_matched_queries"] = (
        result["matched"] / matched_queries if matched_queries else None)
    result["hit_rate"] = result["hits"] / result["queries"] if result["queries"] else None
    for key in ["peak_bytes", "object_bytes", "init_ns", "append_ns", "query_ns",
                "release_ns", "numeric_ns", "classified_tokens"]:
        result[key + "_per_request"] = describe([r[key] for r in rows])
    result["shared_table_init_ns"] = describe([
        int(t["init_ns"]) for group in groups.values() for t in group["table"]
        if t["event"] == "init"])
    result["shared_table_release_ns"] = describe([
        int(t["release_ns"]) for group in groups.values() for t in group["table"]
        if t["event"] == "release"])
    return result


def audit_contracts(audit, plan, run, completed):
    result = {}
    if "host-contracts" in completed:
        log = (run / "host-contracts-driver.log").read_text()
        summary = re.search(r"(\d+) tests, (\d+) passed, (\d+) failed, (\d+) skipped", log)
        audit.check(summary is not None and summary[1] == summary[2] and
                    summary[3] == summary[4] == "0", "host contracts no failure/skip")
        source = (Path(plan["source_snapshot"]) / "tests/suffix_drafter_test.cpp").read_text()
        required = re.findall(r"Q4T_TEST\((suffix_numeric_\w+)\)", source)
        audit.check(len(required) == 8, "eight frozen new numeric host contracts")
        for name in required:
            audit.check("[PASS] " + name in log, "new host contract: " + name)
        result["host_summary"] = summary[0] if summary else None
    if "numeric-stage-contracts" in completed:
        log = (run / "numeric-stage-contracts-driver.log").read_text()
        audit.check(log.strip().endswith("OK") and "skipped=" not in log,
                    "numeric stage audit host contracts")
    if "numeric-development-replay" in completed:
        lines = (run / "numeric-development-replay-driver.log").read_text().splitlines()
        rows = [json.loads(line) for line in lines if line.startswith("{")]
        audit.check(len(rows) == 1, "one development replay result")
        if len(rows) == 1:
            result["development_replay"] = rows[0]
            expected = dict(source_requests=6, source_hits_per_request=15,
                            numeric_rejected_per_request=8, retained_per_request=7,
                            unknown_ids=243, total_rejected=48,
                            new_gpu_requests=0, new_http_requests=0)
            audit.check(all(rows[0].get(k) == v for k, v in expected.items()),
                        "development-only replay fixed contract")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path,
                        default=BASE / "acceptance-v1/frozen-plan.json")
    parser.add_argument("--output", type=Path, default=BASE / "audit-evidence.json")
    args = parser.parse_args()
    plan = read(args.plan)
    run, lease = Path(plan["output"]), Path(plan["lease"])
    state = read(run / "state.json")
    if state.get("status") == "RUNNING" or not (lease / "exit.json").is_file():
        raise SystemExit("Refusing to audit an active stage or active outer guard.")
    if args.output.resolve().is_relative_to(run) or args.output.resolve().is_relative_to(lease):
        raise SystemExit("Audit output must not mutate sealed run or lease directories.")
    audit = Audit()
    audit.check(plan["http_requests"] == 238 and plan["http_loads"] == 18,
                "frozen request/load budget")
    expected_steps = [s["name"] for s in plan["steps"]]
    completed = state.get("completed", [])
    audit.check(completed == expected_steps[:len(completed)],
                "frozen HTTP-first order including stopped-stage prefix")
    audit.measured(state.get("status") == "MEASURED_REQUIRES_REVIEW", "completed stage status")
    audit.measured(completed == expected_steps, "all steps completed")
    audit.measured(state.get("requests_completed") == 238 and state.get("loads_started") == 18,
                   "driver request/load totals")
    audit.check(state.get("requests_completed", 0) <= 238 and
                state.get("loads_started", 0) <= 18, "no automatic expansion")
    audit.measured(state["elapsed_seconds"] <= plan["stage_timeout_seconds"] == 14400,
                   "four-hour wall clock budget")
    audit.measured(state["contract_seconds"] <= plan["direct_contract_timeout_seconds"] == 1200,
                   "direct contract budget")
    identities = audit.protect("identities", lambda: audit_identities(
        audit, plan, args.plan, run, state))
    environment = audit.protect("environment", lambda: audit_environment(
        audit, plan, state, lease), environment=True)
    contracts = audit.protect("contracts", lambda: audit_contracts(
        audit, plan, run, completed))
    groups = {}
    for step in plan["steps"]:
        command_file = run / (step["name"] + "-command.json")
        if command_file.is_file():
            audit.check(read(command_file) == step, step["name"] + ": exact frozen command")
        elif step["name"] in completed:
            audit.check(False, step["name"] + ": missing completed-step command")
        if not step["http_loads"]:
            continue
        folder = run / step["name"]
        if step["name"] not in completed:
            audit.measured(False, step["name"] + ": no completed arm")
            if not folder.is_dir():
                continue
            group = audit.protect(step["name"], lambda step=step: partial_server(
                step, run / step["name"]))
        else:
            group = audit.protect(step["name"], lambda step=step: audit_server(
                audit, plan, step, run / step["name"]))
        if group is not None:
            groups[step["name"]] = group
    admitted = sum(g["admitted"] for g in groups.values())
    raw_count = sum(g["raw_generation_requests"] for g in groups.values())
    pids = {g["pid"] for g in groups.values() if g["pid"] is not None}
    audit.check(admitted <= 238 and raw_count <= 238 and len(pids) <= 18,
                "actual request/load upper bounds")
    audit.measured(admitted == raw_count == 238, "independent raw/admitted generation budget")
    audit.measured(len(groups) == len(pids) == 18, "actual unique server load count")
    resources = audit.protect("resources", lambda: audit_resources(
        audit, plan, state, run, pids), environment=True)
    complete = not audit.measurement_gaps
    validity = "ENV_ABORT" if audit.environment_errors else (
        "INVALID" if audit.errors else "VALID")
    result = dict(created_utc=datetime.now(timezone.utc).isoformat(),
                  audit_script_sha256=sha(Path(__file__)),
                  evidence_validity=validity, measurement_complete=complete,
                  statistical_conclusion="NOT_EVALUATED_BY_INTEGRITY_AUDIT",
                  engineering_decision=("ENV_ABORT_NO_CANDIDATE_PERFORMANCE_DECISION"
                      if audit.environment_errors else
                      "PENDING_FROZEN_GOAL_AND_PROTECTION_REVIEW" if complete else
                      "STOPPED_PER_FROZEN_PLAN_NO_DELIVERY"),
                  checks=dict(audit.checks), errors=audit.errors,
                  environment_errors=audit.environment_errors,
                  measurement_gaps=audit.measurement_gaps,
                  requests=dict(admitted=admitted, raw_generation=raw_count,
                                loads=len(groups), unique_server_pids=len(pids),
                                scope="generation HTTP; lifecycle health/cancel control calls excluded"),
                  state=state, identities=identities, environment=environment,
                  resources=resources, contracts=contracts,
                  groups=groups, counters=aggregate(groups),
                  limits=[
                      "No new GPU/HTTP or tests are run by this audit.",
                      "Old state evidence is two controlled target layers and actual MTP; no arbitrary-input or whole-model equality claim.",
                      "Numeric rejection changes proposal selection and later step boundaries; source identity alone does not prove new quality/performance.",
                      "numeric_ns is included in query_ns; shared table bytes are counted once outside request payload.",
                      "Matched mean uses hits+incomplete+d0_mismatch+numeric_rejected; it is not hit-only match length.",
                      "Init/append/query/release host timings measure operations, not physical RAM returned to OS.",
                      "Raw sampling, model metadata and content-hash coverage are separate identities; large files are content-hashed only where frozen in plan.files.",
                      "Environment abort takes priority and cannot be classified as candidate performance failure."])
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: result[k] for k in
                      ["evidence_validity", "measurement_complete", "statistical_conclusion",
                       "engineering_decision", "checks", "errors", "environment_errors",
                       "measurement_gaps", "requests"]},
                     indent=2, ensure_ascii=False))
    return 0 if validity == "VALID" and complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
