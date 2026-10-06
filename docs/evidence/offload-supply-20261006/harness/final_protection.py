"""Read-only protection closure for the fixed decode-supply observer stage.

Only final-protection.json is written, exclusively. Missing terminal evidence
fails closed. No tests, models, weights, signals, route replay, raw-resource
reparse, systemd operation, or GPU query. The final checker owner's own terminal
receipt must be closed separately by delivery, after this process exits.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time


R = Path(__file__).resolve().parent
W = R / "source"
O = R / "observer-source"
EXPORT = R / "observer-export"
EVIDENCE = O / ".q4t-work/evidence"
MAIN = R.parent.parent
MODEL = MAIN.parent / "llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream"
BASE = "39b879d86124b0340749a83236d94cc8b9e6afb7"
SCOPE_SHA = "2bf0aa00fceebdc00d5b1e04ced774f3eb2ba4104d58e056a21d7c68205086e4"
ENTRY_SHA = "c99f4f8efd86c79f7a20caa66c34e5bb8305fb4f9dc0bf234aee1ba752393bbb"
MODEL_ENTRY_SHA = "6e75ab2eb8a73121eef88facf778fe542cdab62b59a54d5ee30fb81ab5b53eea"
OWN_LABEL = "final-protection-01"
OFFLINE_COMMIT = "8ea3d329a236e35dd0aba35ad619912e3e76cc28"
OBSERVER_COMMIT = "75d26ff22391a4fd7549b1c39bf2b9c0f2cbbf89"
PLAN_SHA = "907111ee1b731521c286e90df3718457c11c25c340a3f66fabc90a03a7584b24"
OFFLINE_PLAN_SHA = "ca89aedf626465a41fbdca823e3c402fed31499477e7b131a04af6cb806ce57f"
GROUPS = ("q01-quality-on", "s01-as-off", "s02-as-on", "s03-al-on", "s04-al-off")
# These terminal reports embed complete checked phase/layer data. The reused
# resource schema already has >32 MiB examples; keep a finite, exact whitelist.
LARGE_REPORTS = {R / "observer-resources.json", R / "observer-analysis.json"} | {
    R / (name + "-decision.json") for name in GROUPS}
EXPECTED_LABELS = {
    "input-extract-01", "bounds-contracts-01", "supply-contracts-01",
    "supply-analysis-01", "observer-configure-01", "observer-build-01",
    "observer-contracts-build-01", "observer-contracts-host-01",
    "observer-contracts-protocol-01", "observer-resource-audit-01",
    "observer-analysis-01",
} | {name + "-controller" for name in GROUPS} | {
    name + "-audit-01" for name in GROUPS}
RUNTIME_PATHS = ("src", "include", "CMakeLists.txt", "cmake")
SOURCES = {}
STATS = {}
REUSED = {}
PIDS = set()
PGIDS = set()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def stat_key(path):
    value = path.stat()
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def bind(path, expected=None):
    path = Path(path).resolve(strict=True)
    if path.is_relative_to(MODEL):
        require(path in (MODEL / "config.json",
                         MODEL / "model.safetensors.index.json"),
                "model payload content reads are forbidden")
    require(expected is None or re.fullmatch(r"[0-9a-f]{64}", expected),
            "invalid expected source SHA")
    before = stat_key(path)
    limit = (256 if path in LARGE_REPORTS else 128) << 20
    require(before[2] <= limit, "source exceeds its finite hash byte bound")
    name = str(path)
    if name in SOURCES:
        require(before == STATS[name], "bound source metadata changed: " + name)
        require(expected is None or SOURCES[name] == expected,
                "repeated source SHA differs: " + name)
        return SOURCES[name]
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    require(stat_key(path) == before, "source changed during hash: " + str(path))
    require(expected is None or digest == expected,
            "source SHA differs: " + str(path))
    require(name not in SOURCES or SOURCES[name] == digest,
            "source changed since first read: " + name)
    SOURCES[name], STATS[name] = digest, before
    return digest


def read(path, expected=None):
    path = Path(path).resolve(strict=True)
    limit = (256 if path in LARGE_REPORTS else 32) << 20
    require(path.stat().st_size <= limit, "JSON exceeds finite byte bound: " + str(path))
    bind(path, expected)
    value = json.loads(path.read_text())
    require(stat_key(path) == STATS[str(path)],
            "JSON changed while reading: " + str(path))
    return value


def text_file(path, expected=None):
    path = Path(path).resolve(strict=True)
    require(path.stat().st_size <= 32 << 20, "text exceeds 32 MiB")
    bind(path, expected)
    value = path.read_text()
    require(stat_key(path) == STATS[str(path)], "text changed while reading")
    return value


def bind_map(ledger, required=()):
    require(isinstance(ledger, dict) and 0 < len(ledger) <= 4096,
            "empty or unbounded source ledger")
    require(set(map(str, required)) <= set(ledger), "required source binding missing")
    for name, digest in ledger.items():
        require(Path(name).is_absolute() and re.fullmatch(r"[0-9a-f]{64}", digest),
                "malformed source ledger entry")
        if name in REUSED:
            require(REUSED[name]["sha256"] == digest and
                    stat_key(Path(name)) == REUSED[name]["stat"],
                    "audited raw/trace source no longer bound: " + name)
        else:
            bind(name, digest)


def expect_fields(value, expected, label):
    require(all(value.get(key) == item for key, item in expected.items()), label)


def runtime_identity(value, plan):
    expect_fields(value, {"plan_sha256": PLAN_SHA,
        "runtime_source_commit": plan["runtime_source_commit"],
        "runtime_binary_sha256": plan["runtime_binary_sha256"]},
        "runtime/execution plan identity differs")


def finite_time(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= time.time()


def git(path, *arguments):
    result = subprocess.run(
        ["git", "--no-pager", *arguments], cwd=path, capture_output=True,
        timeout=30, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    require(result.returncode == 0,
            "git read failed: " + repr(arguments) + ": "
            + result.stderr.decode(errors="replace"))
    return result.stdout


def immutable_protection(report):
    entry = read(R / "entry.json", ENTRY_SHA)
    model_entry = read(R / "model-entry.json", MODEL_ENTRY_SHA)
    scope = read(R / "scope-plan.json", SCOPE_SHA)
    require(entry["base"] == scope["base"] == BASE,
            "entry/scope base differs")
    require(scope["new_runtime_changes_initial_stage"] is False
            and scope["new_model_runs_initial_stage"] == 0
            and scope["new_HTTP_initial_stage"] == 0
            and scope["bench"] is False,
            "offline scope differs")

    head = git(MAIN, "rev-parse", "HEAD").decode().strip()
    status = git(MAIN, "status", "--porcelain=v1").decode().splitlines()
    require(len(entry["main_status"]) == 7 and head == entry["main_head"]
            and status == entry["main_status"], "MAIN HEAD/status changed")
    difference = git(MAIN, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    diff_sha = hashlib.sha256(difference).hexdigest()
    require(diff_sha == entry["main_diff_sha256"]
            == bind(R / "entry-main.diff"), "MAIN full binary diff changed")
    binary_sha = bind(entry["main_binary"]["path"],
                      entry["main_binary"]["sha256"])
    report["main"] = dict(head=head, status=status,
                          full_binary_diff_sha256=diff_sha,
                          main_binary_sha256=binary_sha)

    require(not git(W, "diff", "--binary", "--no-ext-diff", BASE, "--",
                    *RUNTIME_PATHS), "runtime/build source changed from base")
    runtime_status = git(W, "status", "--porcelain=v1", "--untracked-files=all",
                         "--", *RUNTIME_PATHS).decode().splitlines()
    require(not runtime_status, "runtime/build paths dirty or untracked")
    require(git(W, "branch", "--show-current").decode().strip()
            == entry["working_branch"], "working branch differs")
    git(W, "merge-base", "--is-ancestor", BASE, "HEAD")
    report["working_tree"] = dict(
        head=git(W, "rev-parse", "HEAD").decode().strip(),
        status=git(W, "status", "--porcelain=v1").decode().splitlines(),
        runtime_equal_to_base=BASE, runtime_paths=list(RUNTIME_PATHS),
        later_documentation_head_allowed=True,
        limit="Runtime/build paths and base ancestry; tools bound separately.")

    original = model_entry["files"]
    require(len(original) == 228 and model_entry["weight_payload_hashed"] is False,
            "model entry inventory contract differs")
    current = []
    for directory, _, names in os.walk(MODEL):
        for name in names:
            path = Path(directory) / name
            if path.is_file():
                value = path.stat()
                current.append(dict(path=str(path), size=value.st_size,
                    inode=value.st_ino, device=value.st_dev,
                    mtime_ns=value.st_mtime_ns))
    require(sorted(current, key=lambda row: row["path"])
            == sorted(original, key=lambda row: row["path"]),
            "model file inventory or metadata changed")
    metadata_paths = {str(MODEL / "config.json"),
                      str(MODEL / "model.safetensors.index.json")}
    require(set(model_entry["config_index_sha256"]) == metadata_paths,
            "model metadata hash scope differs")
    for path, expected in model_entry["config_index_sha256"].items():
        bind(path, expected)
    report["model"] = dict(files=228, inventory_and_metadata_equal=True,
        config_index_sha256=model_entry["config_index_sha256"],
        weight_payload_content_reads=0,
        limit="Path/size/device/inode/mtime plus config/index bytes only; "
              "no full weight-byte equality or transient-write proof.")

    observations = {}
    for name, path in (("main", MAIN), ("working_tree", W)):
        status = git(path, "status", "--porcelain=v1", "--untracked-files=no",
                     "--", "reference").decode().splitlines()
        require(not status, "tracked reference status changed")
        observations[name] = dict(tracked_status=status,
            tracked_paths=git(path, "ls-files", "--", "reference")
                .decode().splitlines())
    require(observations["main"]["tracked_status"]
            == entry["reference_tracked_status"], "reference entry differs")
    report["reference"] = dict(observations=observations,
        limit="Tracked Git status/paths only; ignored/untracked contents "
              "and transient writes unproved.")


def terminal(start, end):
    for key in ("started_t", "command", "cwd", "timeout_s"):
        require(start[key] == end[key], "owned start/exit identity differs")
    require(end["returncode"] == 0 and end["failure"] is None
            and end["cleanup_complete"] is True
            and end["automatic_retry"] is False and end["signals"] == [],
            "owned operation did not exit cleanly")
    first, last = end["started_t"], end["ended_t"]
    require(all(type(value) in (int, float) and math.isfinite(value)
                for value in (first, last)) and 0 < first <= last <= time.time(),
            "owned terminal times invalid")
    require(type(end["pid"]) is int and end["pid"] > 1
            and end["pgid"] == end["pid"], "owned PID/PGID invalid")
    final = end["group_after_cleanup"]
    require(final["absent"] is True and final["live_pids"] == []
            and final["zombie_pids"] == [] and final["errors"] == []
            and not end.get("cleanup_errors"), "owned final group not absent")


def owned_protection(report):
    starts, exits = {}, {}
    # Owned-helper receipts live directly in R. Service-nested receipts have
    # different schemas and are checked explicitly below, never by recursion.
    for suffix, ledger in (("-start.json", starts), ("-exit.json", exits)):
        for path in R.glob("*" + suffix):
            label = path.name[:-len(suffix)]
            if label != OWN_LABEL:
                ledger[label] = path
    require(set(starts) == set(exits) == EXPECTED_LABELS,
            "owned receipt inventory differs; recovery requires a new explicit scope")
    owned = {}
    for label in sorted(starts):
        start, end = read(starts[label]), read(exits[label])
        terminal(start, end)
        bind(R / (label + ".log"))
        owned[label] = end
        PIDS.add(end["pid"])
        PGIDS.add(end["pgid"])
    report["owned_operations"] = {label: dict(
        returncode=row["returncode"], cleanup_complete=row["cleanup_complete"],
        pid=row["pid"], pgid=row["pgid"]) for label, row in owned.items()}
    report["own_terminal_receipt"] = "Excluded while running; delivery must close final-protection-01."
    return owned


def process_protection(report):
    found, errors = [], []
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            raw = path.read_text()
            tail = raw[raw.rfind(")") + 2:].split()
            pid, group = int(path.parent.name), int(tail[2])
            if pid in PIDS or group in PGIDS:
                found.append(dict(pid=pid, pgid=group, state=tail[0]))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError) as error:
            errors.append(str(path) + ": " + str(error))
    report["owned_processes"] = dict(
        pids=sorted(PIDS), pgids=sorted(PGIDS), current_matches=found,
        scan_errors=errors, signals_sent_by_this_check=0,
        limit="Only recorded owned PID/PGIDs; PID reuse also fails closed. "
              "This does not establish GPU or global idle.")
    require(not found and not errors, "owned process present or scan uncertain")


def offline_protection(report, owned):
    require(git(W, "rev-parse", "HEAD").decode().strip() == OFFLINE_COMMIT and
            not git(W, "status", "--porcelain=v1"), "offline checkout changed")
    plan = read(R / "execution-plan.json", OFFLINE_PLAN_SHA)
    expect_fields(plan, {"runner_source_commit": OFFLINE_COMMIT,
        "fixed_requests": 16, "true_decode_plans": 195840,
        "actual_trace_passes": 1, "HTTP_requests": 0, "model_runs": 0,
        "no_runtime_candidate_admitted": True}, "offline scope differs")
    bind_map(plan["source_sha256"], [R / "supply-inputs.json",
        R / "validation-results.json", W / "tools/trace/decode_supply_bounds.py",
        W / "tools/trace/analyze_decode_supply.py"])
    require(bind(R / "supply-inputs.json") == plan["manifest_sha256"],
            "offline input manifest identity differs")
    validation = read(R / "validation-results.json",
        "9e9b1f7ca40c3add27cad45c3f5abfb459549ae2630921fd75dd2a66b0a4b060")
    expect_fields(validation, {"passed": True, "first_batch": True,
        "tests_passed": 41, "models_launched": 0, "HTTP_requests": 0,
        "real_trace_analyses": 0,
        "old_55_contracts_reused_with_unchanged_identity": True},
        "offline validation closure differs")
    bind_map(validation["source_sha256"])
    vp = read(R / "validation-plan.json", validation["validation_plan_sha256"])
    static = read(R / "tool-static-review.json", validation["tool_static_review_sha256"])
    require(static["passed"] is True and static["independent_review"] is True and
            not static["unresolved_blocking_findings"], "offline tool review failed")
    require(len(validation["batches"]) == len(vp["commands"]) == 2,
            "offline contract batch count differs")
    for batch, command in zip(validation["batches"], vp["commands"]):
        label = batch["label"]
        require(label == command["label"] and batch["tests"] == command["expected_tests"]
                and owned[label]["command"] == command["command"]
                and owned[label]["cwd"] == str(W), "offline contract identity differs")
        bind(R / (label + "-exit.json"), batch["exit_sha256"])
        log = text_file(R / (label + ".log"), batch["log_sha256"])
        require(re.search(r"Ran " + str(batch["tests"]) + r" tests in ", log)
                and log.rstrip().endswith("OK"), "offline test log incomplete")
    analysis = read(R / "supply-analysis.json",
        "42948b8f2cad32b887efb4c399595bd0f86a58f2f8496a36c2acd5cc4462ca5e")
    expect_fields(analysis, {"passed": True, "failure": None,
        "scope_sha256": SCOPE_SHA, "manifest_sha256": plan["manifest_sha256"],
        "execution_plan_sha256": OFFLINE_PLAN_SHA, "model_runs": 0,
        "HTTP_requests": 0, "performance_acceptance": False,
        "physical_IO_prediction": False,
        "direct_race_decision": "DIRECT_RACE_NOT_EXCLUDED_OBSERVER_APPENDIX_REQUIRED"},
        "offline report identity or decision differs")
    review = read(R / "result-independent-review.json",
        "3244e0702f17efffb3e53155bd129b0ae74dfd05a0deef6c05956f40dbd83c41")
    require(review["passed"] is True and review["independent_review"] is True
            and not review["blocking_findings"] and
            review["reviewed_report_sha256"] == bind(R / "supply-analysis.json"),
            "offline independent closure differs")
    require(review["counts"]["complete_requests"] == 16 and
            review["counts"]["GPU_endpoint_checked_flags"] == 16 and
            review["counts"]["full_iterator_exhausted_flags"] == 16 and
            review["phase_gate"]["conditional_observer_arm"] == "A",
            "offline complete request or observer trigger differs")
    bind_map(review["source_sha256"])
    require(owned["supply-analysis-01"]["started_t"] >= validation["recorded_t"]
            and owned["supply-analysis-01"]["ended_t"] <= review["recorded_t"],
            "offline result/validation time ordering differs")
    report["offline_evidence"] = dict(commit=OFFLINE_COMMIT,
        host_contracts=41, requests=16, independent_result_reused=True,
        old_GPU_contracts_reused=55, route_replays_by_this_check=0,
        scope="Finite frozen inputs/tools and immutable prior closure; no old trace reparse.")


def observer_source_protection(report, owned):
    plan = read(R / "observer-execution-plan.json", PLAN_SHA)
    expect_fields(plan, {"runtime_source_commit": OBSERVER_COMMIT,
        "runner_source_commit": OBSERVER_COMMIT, "service_count": 5,
        "http_count": 27, "group_ids": list(GROUPS),
        "expected_host_contracts": 23, "expected_protocol_contracts": 23,
        "performance_acceptance": False}, "observer execution scope differs")
    require(len(plan["frozen_files"]) == 312, "frozen dependency set differs")
    bind_map(plan["frozen_files"], [R / "observer-source-identity.json",
        R / "observer-build-identity.json", R / "observer-dependency-admission.json",
        R / "observer-independent-static-review-r2.json",
        R / "observer-resource-independent-static-review.json"])
    source = read(R / "observer-source-identity.json")
    build = read(R / "observer-build-identity.json")
    attempt = read(R / "observer-build-attempt.json")
    require(source["commit"] == build["source_commit"] == attempt["source_commit"]
            == OBSERVER_COMMIT and source["clean_export"] is True and
            source["source"] == build["source"] == str(EXPORT) and
            source["source_sha256"] == build["source_sha256"] and
            len(source["source_sha256"]) == 1621,
            "observer export/build source identity differs")
    files = {str(path) for path in EXPORT.rglob("*") if path.is_file()}
    require(files == set(source["source_sha256"]) and
            all(not path.is_symlink() for path in EXPORT.rglob("*")),
            "export file inventory or symlink differs")
    bind_map(source["source_sha256"])
    bind(R / "observer-source.tar", source["archive_sha256"])
    require(build["binary"] == plan["runtime_binary_path"] and
            build["binary_sha256"] == plan["runtime_binary_sha256"] and
            build["warnings"] == build["build_rc"] == 0 and
            build["tests_or_model_executed"] is False and attempt["failure"] is None,
            "build receipt is not successful and warning-free")
    bind(plan["runtime_binary_path"], plan["runtime_binary_sha256"])
    bind(R / "observer-build/CMakeCache.txt", build["cmake_cache_sha256"])
    bind(R / "observer-build/compile_commands.json", build["compile_commands_sha256"])
    rows = [owned[name] for name in ("observer-configure-01", "observer-build-01")]
    require(build["records"] == attempt["records"] == rows,
            "build identity/owned records differ")
    for name in ("observer-configure-01", "observer-build-01"):
        require(not re.search(r"warning\s*:|warning\s*#", text_file(R / (name + ".log")), re.I),
                "configure/build log contains warnings")
    git(O, "merge-base", "--is-ancestor", OBSERVER_COMMIT, "HEAD")
    require(git(O, "branch", "--show-current").decode().strip()
            == "codex/offload-supply-observer-20261006", "observer branch differs")
    require(not git(O, "status", "--porcelain=v1"), "observer checkout dirty")
    changed = git(O, "diff", "--name-only", "--no-renames", OBSERVER_COMMIT,
                  "HEAD", "--").decode().splitlines()
    require(all(name.startswith(("docs/", "evidence/")) for name in changed),
            "observer non-document tracked files changed after execution commit")
    dependency = read(R / "observer-dependency-admission.json")
    require(dependency["passed"] is True, "dependency admission failed")
    bind_map(dependency["source_sha256"])
    for path, target in dependency["required_path_resolutions"].items():
        require(str(Path(path).resolve(strict=True)) == target,
                "dependency path resolution changed")
    for name in ("observer-independent-static-review-r2.json",
                 "observer-resource-independent-static-review.json"):
        record = read(R / name)
        require(record["passed"] is True, "observer static review failed: " + name)
    admission = read(R / "observer-execution-admission.json")
    expect_fields(admission, {"execution_admitted": True,
        "execution_plan_sha256": PLAN_SHA,
        "runtime_binary_sha256": plan["runtime_binary_sha256"]},
        "actual execution admission missing")
    bind_map(admission["source_sha256"], [R / "observer-execution-independent-review.json"])
    execution_review = read(R / "observer-execution-independent-review.json")
    require(execution_review["passed"] is True and not execution_review["blocking_findings"]
            and execution_review["execution_plan_sha256"] == PLAN_SHA,
            "independent execution review failed")
    bind_map(execution_review["source_sha256"])
    require(execution_review["recorded_t"] <= admission["recorded_t"]
            <= owned[GROUPS[0] + "-controller"]["started_t"],
            "independent review/execution admission order differs")
    report["observer_source"] = dict(execution_commit=OBSERVER_COMMIT,
        delivery_head=git(O, "rev-parse", "HEAD").decode().strip(),
        later_document_paths=changed, source_export_files=1621,
        frozen_dependencies=312, binary_sha256=plan["runtime_binary_sha256"],
        build_warnings=0, dependency_limits=dependency["limits"])
    return plan


def resource_protection(report, owned, plan):
    resource = read(R / "observer-resources.json")
    runtime_identity(resource, plan)
    expect_fields(resource, {"schema": 1, "audit_complete": True,
        "raw_resources_read": True, "performance_acceptance": False,
        "http_request_count": 27, "client_envelope_count": 17,
        "physical_union_peak_bytes": None, "whole_physical_RAM_54GB": "INDETERMINATE",
        "status": "RAW_RESOURCE_EVIDENCE_REVIEWED; KNOWN_GAPS_RETAINED"},
        "resource audit incomplete or scope differs")
    require(not resource.get("failure") and resource["interpretation_limits"],
            "resource audit failure or missing limits")
    expect_fields(resource["method"], {"quality_resource_envelopes": 1,
        "diagnostic_resource_envelopes": 16, "raw_resource_parse_passes": 1,
        "model_payload_reads": False, "live_system_GPU_queries": False},
        "resource method differs")
    owner = owned["observer-resource-audit-01"]
    require(owner["started_t"] <= resource["started_t"] <= resource["ended_t"]
            <= owner["ended_t"], "resource audit time containment differs")
    require(owner["command"] == ["/usr/bin/python3", "-B",
            str(R / "observer_resource_audit.py"), "--plan-sha256", PLAN_SHA,
            "--output", str(R / "observer-resources.json")] and owner["cwd"] == str(O),
            "resource audit command differs")
    require(set(resource["groups"]) == set(resource["all_service_terminals"])
            == set(GROUPS), "resource group coverage differs")
    for name, group in resource["groups"].items():
        count, envelopes = (11, 1) if name == GROUPS[0] else (4, 4)
        require(group["request_count"] == len(group["http_request_windows"]) == count
                and group["client_envelope_count"] == len(group["client_envelopes"]) == envelopes
                and group["inspection"]["all_summary_checks_match"] is True
                and group["inspection"]["identity_contracts_passed"] is True,
                "resource per-service coverage incomplete")
    raw = resource["source_sha256"]
    traces = resource["trace_binary_source_bindings"]
    require(isinstance(raw, dict) and raw and len(raw) <= 512 and
            isinstance(traces, dict) and len(traces) == 24,
            "raw or 4x(4 request + command/environment) trace ledger differs")
    for name, item in raw.items():
        path = Path(name)
        require(path.is_absolute() and path.resolve(strict=True).is_relative_to(EVIDENCE)
                and not path.is_symlink(), "raw resource outside evidence scope")
        signature = item["stat_after"]
        require(item["complete_file"] is True and item["stat_before"] == signature
                and item["bytes_consumed"] == signature[2],
                "raw source was not completely audited")
        require(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) and
                stat_key(path) == signature, "audited raw resource changed")
        REUSED[name] = dict(sha256=item["sha256"], stat=signature,
                            kind="audited raw stream; metadata continuity only")
    require(sum(item["bytes_consumed"] for item in raw.values())
            == resource["raw_bytes_consumed"], "resource byte ledger differs")
    for name, item in traces.items():
        path = Path(name)
        require(path.is_absolute() and not path.is_symlink() and
                path.parent in {EVIDENCE / group / "http/trace" for group in GROUPS[1:]}
                and re.fullmatch(r"(request-[0-9]+|command|environment)\.bin", path.name)
                and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                and stat_key(path) == item["signature"], "trace binding changed")
        require(name not in REUSED, "raw/trace ledger overlap")
        REUSED[name] = dict(sha256=item["sha256"], stat=item["signature"],
                            kind="audited trace hash; metadata continuity only")
    metadata = resource["metadata_source_sha256"]
    require(isinstance(metadata, dict) and 0 < len(metadata) <= 4096,
            "resource metadata ledger invalid")
    for name, item in metadata.items():
        require(item["bytes"] == item["stat"][2] and
                stat_key(Path(name)) == item["stat"], "resource metadata source changed")
        bind(name, item["sha256"])
    require(all(metadata.get(name, {}).get("sha256") == digest
                for name, digest in plan["frozen_files"].items()),
            "resource frozen source coverage differs")
    report["resources"] = dict(http_requests=27, client_envelopes=17,
        source_raw_count=len(raw), trace_binary_count=len(traces),
        raw_resource_reparse_by_this_check=0, route_replays_by_this_check=0,
        whole_physical_RAM_54GB="INDETERMINATE",
        limits=resource["interpretation_limits"],
        integrity_limit="Raw/trace content hashes reused from terminal resource audit; "
            "final stat equality does not exclude same-metadata byte replacement.")
    return resource


def service_protection(report, owned, plan, resource):
    require({path.name for path in EVIDENCE.iterdir() if path.is_dir()} == set(GROUPS),
            "evidence service directory inventory differs")
    first = read(R / "observer-first-runtime-test-admission.json")
    expect_fields(first, {"plan_sha256": PLAN_SHA, "group": GROUPS[0],
        "runtime_binary_sha256": plan["runtime_binary_sha256"],
        "no_new_contracts_or_diagnostics_started": True},
        "quality-first runtime admission differs")
    require(finite_time(first["recorded_t"]) and
            owned["observer-build-01"]["ended_t"] <= first["recorded_t"]
            <= owned[GROUPS[0] + "-controller"]["started_t"],
            "quality-first admission time differs")
    decisions = {}
    for index, name in enumerate(GROUPS):
        quality = index == 0
        count = 11 if quality else 4
        enabled = name not in ("s01-as-off", "s04-al-off")
        directory = EVIDENCE / name
        owner = owned[name + "-controller"]
        audit = owned[name + "-audit-01"]
        stage = read(R / (name + "-stage.json"))
        decision = read(R / (name + "-decision.json"))
        runtime_identity(stage, plan)
        runtime_identity(decision, plan)
        command = read(R / (name + "-command.json"), plan["command_sha256"][name])
        require(owner["command"] == stage["command"] == command and
                owner["cwd"] == str(O) and stage["group_index"] == index and
                stage["group_id"] == decision["group_id"] == name and
                stage["first_runtime_test"] is quality,
                "service command or group identity differs")
        for key in ("started_t", "pid", "pgid", "returncode", "failure",
                    "cleanup_complete", "signals", "group_after_cleanup"):
            require(stage[key] == owner[key], "stage/owner receipt differs: " + key)
        expect_fields(stage, {"automatic_retry": False, "performance_acceptance": False},
                      "stage retry/performance scope differs")
        expect_fields(decision, {"passed": True, "request_count": count,
            "performance_acceptance": False,
            "status": "PASS_FIXED_HTTP_11" if quality else "PASS_GROUP_CONTRACTS"},
            "HTTP group decision incomplete")
        require(len(decision["metrics"]) == count and
                len({row["response_id"] for row in decision["metrics"]}) == count and
                decision["runtime_path"]["runtime_eligible"] is True and
                decision["all_layer_runtime"]["runtime_eligible"] is True,
                "request metric or runtime path closure differs")
        require(owner["ended_t"] <= stage["ended_t"] == decision["ended_t"]
                <= audit["started_t"] <= decision["recorded_t"] <= audit["ended_t"]
                <= owned["observer-resource-audit-01"]["started_t"],
                "service/audit/resource ordering differs")
        require(resource["all_service_terminals"][name] == stage["ended_t"],
                "resource terminal differs")
        audit_command = audit["command"]
        require(audit_command == ["/usr/bin/python3", "-B",
                str(R / "audit_observer_stage.py"), name, "--plan-sha256", PLAN_SHA]
                and audit["cwd"] == str(O),
                "HTTP audit command identity differs")
        if index:
            require(decisions[GROUPS[index - 1]]["recorded_t"] <= owner["started_t"],
                    "service prerequisite order differs")
            bind(R / "observer-sequences" / (name + ".json"),
                 plan["sequence_sha256"][name])
            require(len(decision["traces"]) == 4 and
                    decision["trace_manifest"]["requests_started"] ==
                    decision["trace_manifest"]["requests_published"] == 4,
                    "diagnostic trace completion differs")
        else:
            require(decision["traces"] == [] and decision["trace_manifest"] is None
                    and not (directory / "http/trace").exists(),
                    "unexpected quality route trace")
        needed = [R / (name + "-stage.json"), directory / "wrapper-exit.json",
            directory / "http/exit.json", directory / "runner-process-group.json",
            directory / "http/isolation/cleanup.json", directory / "protocol.json",
            directory / "http/server.log"]
        bind_map(decision["source_sha256"], needed + [Path(p) for p in plan["audit_sources"]])
        wrapper = read(directory / "wrapper-exit.json")
        http = read(directory / "http/exit.json")
        runner = read(directory / "runner-process-group.json")
        cleanup = read(directory / "http/isolation/cleanup.json")
        isolation = read(directory / "http/isolation/identity.json")
        protocol = read(directory / "protocol.json")
        require(wrapper["runner_rc"] == wrapper["monitor_rc"] == http["server"] == 0
                and wrapper["failure"] is http["failure"] is http["cleanup_failure"] is None
                and wrapper["cleanup_failed"] is False and
                wrapper["unit_after_cleanup"]["LoadState"] == "not-found",
                "HTTP wrapper did not terminate cleanly")
        require(http["completed"] == count and http["http_output_checks_passed"] is True
                and http["performance_acceptance"] is False,
                "HTTP completion count differs")
        require(runner["cleanup_complete"] is runner["runner_reaped"] is True
                and runner["returncode"] == 0 and runner["failure"] is None
                and runner["after_cleanup"]["absent"] is True
                and all(not runner["after_cleanup"][key] for key in
                        ("live_pids", "zombie_pids", "errors")),
                "nested runner cleanup incomplete")
        require(cleanup["stop_rc"] == 0 and cleanup["unit_removed"] is True
                and cleanup["properties_after"]["LoadState"] == "not-found"
                and cleanup["properties_after"]["MainPID"] == "0",
                "recorded service unit cleanup incomplete")
        require(owner["started_t"] <= wrapper["started_t"] <= runner["started_t"]
                < runner["ended_t"] <= wrapper["ended_t"] <= owner["ended_t"],
                "nested runner times not contained by owner")
        require(type(runner["runner_pid"]) is int and runner["runner_pid"] > 1
                and runner["pgid"] == runner["runner_pid"], "runner PID/PGID invalid")
        PIDS.add(runner["runner_pid"])
        PGIDS.add(runner["pgid"])
        server_pid = int(isolation["properties"]["MainPID"])
        require(server_pid > 1, "recorded server PID missing")
        PIDS.add(server_pid)
        expect_fields(protocol, {"policy_axis": "request-partition-log",
            "partition": 0, "request_partition": 0,
            "decode_partition_log_quiet": 0, "chunk_order": 0,
            "phase_diagnostics": True, "binary_sha256": plan["runtime_binary_sha256"],
            "host_cache_max_bytes": 16 << 30, "swap_max_bytes": 0,
            "mode": "quality" if quality else "performance"},
            "service protocol scope differs")
        require(protocol["effective_environment"]["Q4T_MOE_SUPPLY_OBSERVER"] == str(int(enabled)),
                "observer on/off setting differs")
        capacity = read(directory / "http/capacity.json")
        require(capacity["matches_requested"] is True and
                capacity["requested"] == capacity["effective"] ==
                {"max_len": 262144, "max_seq": 1, "max_prefill": 8192},
                "runtime capacity differs")
        require(text_file(directory / "http/binary.sha256").strip()
                == plan["runtime_binary_sha256"] and
                text_file(directory / "http/commit.txt").strip() == OBSERVER_COMMIT and
                text_file(directory / "http/worktree.patch") == "",
                "recorded HTTP build identity differs")
        bind(directory / "http/CMakeCache.txt",
             plan["frozen_files"][str(R / "observer-build/CMakeCache.txt")])
        windows = resource["groups"][name]["http_request_windows"]
        require([row["response_id"] for row in windows] ==
                [row["response_id"] for row in decision["metrics"]],
                "resource/HTTP request identities differ")
        decisions[name] = decision
    report["HTTP"] = dict(service_count=5, request_count=27,
        diagnostic_request_count=16, quality_requests=11,
        service_order=list(GROUPS), evidence_reused_without_new_HTTP=True,
        request_numerical_and_protocol_checks="Reused fixed audited group decisions; no re-analysis.")
    return decisions


def contracts_protection(report, owned, plan, decisions):
    contracts = read(R / "observer-contracts-decision.json")
    expect_fields(contracts, {"schema": 1, "passed": True, "plan_sha256": PLAN_SHA,
        "runtime_binary_sha256": plan["runtime_binary_sha256"],
        "host_contracts": 23, "protocol_contracts": 23,
        "total": 46, "first_batch": True}, "new contract closure differs")
    rows = plan["contract_commands"]
    require(len(rows) == 3, "contract command count differs")
    required = list(plan["contract_sources"])
    preceding = owned[GROUPS[0] + "-audit-01"]["ended_t"]
    require(decisions[GROUPS[0]]["recorded_t"] <= preceding,
            "quality decision outside audit")
    for row in rows:
        label = row["label"]
        owner = owned[label]
        require(owner["command"] == row["argv"] and owner["cwd"] == row["cwd"]
                and preceding <= owner["started_t"],
                "new contract command or quality-first order differs")
        preceding = owner["ended_t"]
        required.extend(str(R / (label + suffix))
                        for suffix in (".log", "-start.json", "-exit.json"))
    require(preceding <= contracts["recorded_t"]
            <= owned[GROUPS[1] + "-controller"]["started_t"],
            "contract completion does not precede first diagnostic")
    bind_map(contracts["source_sha256"], required)
    cpp = text_file(R / "observer-contracts-host-01.log")
    python = text_file(R / "observer-contracts-protocol-01.log")
    require(sum(line.startswith("PASS ") for line in cpp.splitlines()) == 23 and
            "23 contracts passed" in cpp and re.search(r"Ran 23 tests in ", python)
            and python.rstrip().endswith("OK"), "new host/protocol contract log incomplete")
    require(not re.search(r"warning\s*:|warning\s*#",
            text_file(R / "observer-contracts-build-01.log"), re.I),
            "host contract build warnings")
    report["observer_contracts"] = dict(host=23, protocol=23, total=46,
        quality_first=True, first_batch=True, executed_by_this_check=0)


def result_protection(report, owned, plan):
    script = R / "analyze_supply_observer.py"
    script_sha = bind(script,
        "0f0bda9217b6a6ec384ce50c94a04f6df949133cc98b66ee6597d7fee3f68574")
    static = read(R / "observer-analysis-static-review.json",
        "67fe3706691a2232fa3a3ed7898a467dd10ecb8a15930b6f8fd8e9bce34f9de9")
    require(static["passed"] is True, "analysis static review failed")
    bind_map(static["source_sha256"], [script])
    admission = read(R / "observer-analysis-admission.json")
    expect_fields(admission, {"execution_admitted": True,
        "plan_sha256": PLAN_SHA, "analysis_script_sha256": script_sha},
        "analysis execution admission differs")
    bind_map(admission["source_sha256"], [R / "observer-analysis-static-review.json"])
    owner = owned["observer-analysis-01"]
    require(owner["command"] == ["/usr/bin/python3", "-B", str(script),
            "--plan-sha256", PLAN_SHA] and owner["cwd"] == str(O),
            "analysis owned command differs")
    require(owned["observer-resource-audit-01"]["ended_t"] <= owner["started_t"],
            "analysis began before resource audit termination")
    analysis = read(R / "observer-analysis.json")
    runtime_identity(analysis, plan)
    expect_fields(analysis, {"schema": 1, "passed": True, "request_count": 27,
        "diagnostic_request_count": 16, "service_count": 5, "contract_count": 46,
        "resource_envelope_count": 17}, "observer analysis closure differs")
    require(isinstance(analysis["decision"], str) and analysis["decision"]
            and analysis["limits"] and not analysis.get("failure"),
            "analysis decision or evidence limits missing")
    required = [R / "observer-resources.json", R / "observer-contracts-decision.json",
                R / "observer-execution-plan.json", R / "observer-analysis-admission.json",
                script] + [R / (name + "-decision.json") for name in GROUPS]
    bind_map(analysis["source_sha256"], required)
    require(owner["started_t"] <= analysis["recorded_t"] <= owner["ended_t"],
            "analysis output time outside owned execution")
    review = read(R / "observer-result-independent-review.json")
    expect_fields(review, {"schema": 1, "passed": True, "plan_sha256": PLAN_SHA,
        "analysis_sha256": bind(R / "observer-analysis.json")},
        "observer independent result review missing or differs")
    require(review["limits"] and not review.get("blocking_findings") and
            finite_time(review["recorded_t"]) and
            owner["ended_t"] <= review["recorded_t"] <= report["started_t"],
            "independent review not terminal or limits missing")
    bind_map(review["source_sha256"], [R / "observer-analysis.json",
        R / "observer-resources.json", R / "observer-execution-plan.json"])
    report["observer_result"] = dict(analysis_sha256=bind(R / "observer-analysis.json"),
        independent_review_sha256=bind(R / "observer-result-independent-review.json"),
        decision=analysis["decision"], limits=analysis["limits"],
        independent_review_limits=review["limits"],
        count_arithmetic_recomputed_by_this_check=False)
    report["acceptance_limits"] = dict(performance_acceptance=False,
        runtime_optimization_implemented=False, observer_default_off=True,
        prior_NO_GO_unchanged=True, whole_physical_RAM_54GB="INDETERMINATE",
        global_GPU_idle_established=False, delivery_receipt_still_required=True)


def evidence_protection(report, owned):
    offline_protection(report, owned)
    plan = observer_source_protection(report, owned)
    resource = resource_protection(report, owned, plan)
    decisions = service_protection(report, owned, plan, resource)
    contracts_protection(report, owned, plan, decisions)
    result_protection(report, owned, plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-sha256", required=True)
    args = parser.parse_args()
    report = dict(schema=1, started_t=time.time(), passed=False, failure=None,
                  scope_sha256=SCOPE_SHA, source_sha256={})
    with (R / "final-protection.json").open("x") as destination:
        try:
            bind(Path(__file__), args.self_sha256)
            immutable_protection(report)
            owned = owned_protection(report)
            evidence_protection(report, owned)
            process_protection(report)
            for path, expected in STATS.items():
                require(stat_key(Path(path)) == expected,
                        "source metadata changed during protection: " + path)
            for path, item in REUSED.items():
                require(stat_key(Path(path)) == item["stat"],
                        "audited raw/trace metadata changed during protection: " + path)
            report.update(passed=True, status="FINAL_PROTECTION_PASS")
        except BaseException as error:
            report.update(status="FINAL_PROTECTION_FAILED",
                          failure=type(error).__name__ + ": " + str(error))
        report.update(source_sha256=SOURCES, audited_raw_trace_sha256_reused=REUSED,
                      ended_t=time.time())
        json.dump(report, destination, ensure_ascii=False, indent=2,
                  allow_nan=False)
        destination.write("\n")
    print(json.dumps({key: report[key] for key in ("status", "passed", "failure")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
