"""Frozen preflight delivery, guarded pair replacement, and identity rollback."""
import argparse
import copy
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


a = module("preflight_pair_delivery", ROOT / "tools/application_service/stage.py")
h = a.h
previous = module("preflight_previous_stage", ROOT / "tools/serve_text_stop/stage.py")
sdk_environment = previous.sdk_environment
SDK_PYTHON = previous.SDK_PYTHON
BASE = ROOT / ".q4t-work/serve-preflight-20261103"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
LIMITS = dict(starts=3, generations=12, negatives=20, preflights=32, seconds=1800)
OLD_SHA = "a3fd64a34a67f619bf2978faf152d6f455652d048a1e7a2cdc48caf784898184"
SCHEMAS = (*previous.SCHEMAS, "docs/serve-preflight-v1.schema.json",
           "docs/serve-config-v1.schema.json")


def cpu_gate():
    cpu = BASE / "cpu"
    driver = "driver-corrected-result.json" if PACKAGE.name == "package-corrected" else "driver-result.json"
    for name in ("host-gate/result.json", "template-diff/result.json",
                 "startup-preflight/result.json", driver):
        h.require(h.read(cpu / name)["passed"], "CPU gate failed: " + name)
    for mode, count in (("off", 198), ("on", 207)):
        initial = (cpu / f"host-{mode}.log").read_text()
        h.require(f"{count} tests, {count-1} passed, 1 failed, 0 skipped" in initial
            and [r for r in initial.splitlines() if r.startswith("[FAIL]")] ==
            ["[FAIL] chat_plan_output_spelling_and_capacity_cap"], "initial host failure")
        h.require("1 tests, 1 passed, 0 failed, 0 skipped" in
            (cpu / f"host-{mode}-fixture.log").read_text(), "fixture not repaired")
    h.require("9 tests, 9 passed, 0 failed, 0 skipped" in
              (cpu / "backend-gate.log").read_text(), "grammar backend gate")
    h.require("Ran 31 tests" in (cpu / "client.log").read_text() and
              (cpu / "client.log").read_text().endswith("OK\n"), "client contracts")
    for name in ("build-engine-corrected.log", "build-off.log", "build-on.log",
                 "build-off-fixture.log", "build-on-fixture.log"):
        log = (cpu / name).read_text()
        h.require("error:" not in log and "warning:" not in log, "build diagnostics")


def prepare():
    h.require(not PACKAGE.exists(), "package already frozen")
    old = h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json", OLD_SHA)
    cpu_gate()
    sdk = sdk_environment()  # Construct the real pinned client before any lease.
    engine = BASE / "engine"
    h.require("Q4T_SERVE_FAULT_INJECTION:BOOL=OFF" in
              (engine / "CMakeCache.txt").read_text(), "faults in production")
    generated = engine / "generated/q4t/server"
    protocol = h.read(generated / "chat_protocol_sources.json")
    a.verify_hashes(ROOT, protocol)
    h.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
              "commit the reviewed stage before freezing")
    PACKAGE.mkdir()
    for name in ("previous", "candidate"):
        (PACKAGE / name).mkdir()
    shutil.copytree(BASE / "cpu", PACKAGE / "cpu")
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT / "build" / name), PACKAGE / "previous" / name)
    h.copy_checked(h.identity(old["build_cache"]), PACKAGE / "previous/CMakeCache.txt")
    for name in ("q4t", "CMakeCache.txt", "compile_commands.json",
                 "q4t_preflight_probe", "q4t_http_lifecycle_tests"):
        h.copy_checked(h.identity(engine / name), PACKAGE / "candidate" / name)
    h.copy_checked(h.read(BASE / "cpu/startup-preflight/result.json")["binary"],
                   PACKAGE / "candidate/startup_preflight")
    for name in ("chat_protocol_identity.h", "chat_protocol_sources.json"):
        h.copy_checked(h.identity(generated / name), PACKAGE / "candidate" / name)
    old_sources = h.read(old["tested_source_manifest"])
    paths = set(old_sources) | {"src/server/chat_plan.cpp", "src/server/chat_prompt.cpp",
        "src/server/chat_preflight.cpp", "include/q4t/server/chat_plan.h",
        "include/q4t/server/chat_prompt.h"}
    current = {p: h.sha(ROOT / p) for p in sorted(paths)}
    changed = [p for p in old_sources if old_sources[p] != current[p]]
    h.require(all(p.startswith(("src/server/", "include/q4t/server/")) for p in changed),
              "model/numerical source changed")
    h.save(PACKAGE / "candidate/runtime-source.json", current)
    h.save(PACKAGE / "numerical-reuse.json", dict(changed=changed,
        added=sorted(paths - old_sources.keys()),
        previous_manifest=old["tested_source_manifest"],
        unchanged_model_and_numerical_sources=True, performance_acceptance=False))
    sources = {p: h.sha(ROOT / p) for p in a.acceptance.source_paths()}
    sources.update({p: h.sha(ROOT / p) for p in SCHEMAS})
    h.save(PACKAGE / "sources.json", sources)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                     text=True).strip()
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "10b3442..HEAD"], cwd=ROOT, check=True)
    inherited = h.read(previous.BASE / "package-fixture/plan.json")
    model = Path(inherited["model_dir"])
    plan = dict(schema_version=1, source_commit=commit, sdk_environment=sdk,
        candidate=str(PACKAGE / "candidate/q4t"),
        candidate_sha256=h.sha(PACKAGE / "candidate/q4t"),
        old_hashes={n: h.sha(PACKAGE / "previous" / n) for n in a.NAMES},
        previous_binary_sha256=OLD_SHA,
        protocol_sha256=hashlib.sha256("".join(f"{p}:{protocol[p]}\n"
            for p in sorted(protocol)).encode()).hexdigest(),
        model_dir=str(model), model_inputs={p: h.sha(model / p) for p in inherited["model_inputs"]},
        sdk_version="3.42.1", limits=LIMITS,
        source_manifest_sha256=h.sha(PACKAGE / "sources.json"),
        planned=dict(starts=3, generations=12, preflights=29, negatives=16),
        startup_preflight=h.identity(PACKAGE / "candidate/startup_preflight"),
        prompt_probe=h.identity(PACKAGE / "candidate/q4t_preflight_probe"),
        lifecycle_probe=h.identity(PACKAGE / "candidate/q4t_http_lifecycle_tests"))
    if PACKAGE.name == "package-corrected":
        prior = BASE / "execution"
        h.verify_seal(BASE / "package")
        failure = h.read(prior / "business/result.json")
        h.require(failure["request_attempts"] == 8 and failure["negative_requests"] == 15
            and failure["preflight_requests"] == 23 and failure["starts"] == 1 and
            failure["error"] == 'AttributeError("\'NoneType\' object has no attribute \'strip\'")',
            "unexpected long-history failure")
        folder = prior / "business/candidate-mtp"
        observed = h.read(folder / "long-history-result.json")
        h.require(observed["finish_reason"] == "tool_calls" and
            observed["message"]["tool_calls"][0]["function"] ==
                dict(name="first", arguments="{}") and
            observed["usage"]["prompt_tokens"] == 5660,
            "original long-history observation changed")
        h.require(h.read(folder / "shutdown.json")["clean"] and
            h.read(prior / "lease/exit.json")["status"] == "COMMAND_FAILED" and
            h.read(BASE / "package/plan.json")["candidate_sha256"] == plan["candidate_sha256"],
            "fixture correction requires drained identical runtime")
        event = json.loads((prior / "lease/events.jsonl").read_text().splitlines()[0])
        plan.update(original_started=datetime.datetime.fromisoformat(event["utc"]).timestamp(),
            prior_counts=dict(starts=1, generations=8, negatives=15, preflights=23),
            reuse_http=dict(files={p.name: h.identity(p) for p in folder.iterdir() if p.is_file()},
                failed_result=h.identity(prior / "business/result.json"),
                successful_comparisons=failure["comparisons"][:7]),
            planned_new=dict(business=2, health=2, starts=2, preflights=7, negatives=1),
            scope_note="Retain original auto-tool quality failure. Explicit tool_choice=none long-history retry also proves actual long-slot selection; exact 2049 eligibility is read-only, actual short-slot requirement is 2048. No repeated passed requests; cumulative 12 generations/30 preflights/16 negatives/3 starts, original 30 minute deadline.")
    h.save(PACKAGE / "plan.json", plan)
    h.seal(PACKAGE)
    print("PREPARED", plan["candidate_sha256"], flush=True)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE / "plan.json")
    h.require(plan["limits"] == LIMITS and plan["source_manifest_sha256"] ==
              h.sha(PACKAGE / "sources.json"), "frozen plan changed")
    a.verify_hashes(ROOT, h.read(PACKAGE / "sources.json"))
    a.verify_hashes(plan["model_dir"], plan["model_inputs"])
    h.require(h.sha(plan["candidate"]) == plan["candidate_sha256"], "candidate changed")
    if (OUTPUT / "begin.json").exists():
        plan["started"] = h.read(OUTPUT / "begin.json")["started"]
        plan.setdefault("original_started", plan["started"])
    return plan


def verifier(plan, installed=False):
    v = module("preflight_http_checks", Path(__file__).with_name("verify.py"))
    v.Run(plan, OUTPUT / ("health" if installed else "business"), installed=installed).run()


def release(plan):
    result = OUTPUT / "business/result.json"
    h.require(h.read(result)["status"] == "PASS_BOUNDED_PREFLIGHT", "business gate")
    caps = json.loads(h.read(OUTPUT / "business/candidate-mtp/capabilities.json")["raw"])
    h.require(caps["preflight"]["profile"] == "q4t-preflight-v1", "capability gate")
    metadata = h.read(PACKAGE / "previous/q4t.release.json")
    inherited = copy.deepcopy({k: metadata[k] for k in ("acceptance", "acceptance_decision",
        "acceptance_decision_sha256", "chat_protocol", "deployment_completion", "scope") if k in metadata})
    inherited["release"] = h.identity(PACKAGE / "previous/q4t.release.json")
    metadata.pop("deployment_completion", None)
    metadata.update(version="serve-preflight-20261103",
        binary_sha256=plan["candidate_sha256"], source_commit=plan["source_commit"],
        source_note="Read-only text request preflight and effective configuration. Shared production contract, native prompt, tokenizer and context budget. No model numerical changes or performance acceptance.",
        tested_source_manifest=str(PACKAGE / "candidate/runtime-source.json"),
        tested_source_manifest_sha256=h.sha(PACKAGE / "candidate/runtime-source.json"),
        build_cache=str(PACKAGE / "candidate/CMakeCache.txt"),
        build_cache_sha256=h.sha(PACKAGE / "candidate/CMakeCache.txt"),
        acceptance="ACCEPT_BOUNDED_PREFLIGHT", acceptance_decision=str(result),
        acceptance_decision_sha256=h.sha(result),
        previous_binary=str(PACKAGE / "previous/q4t"),
        previous_release=str(PACKAGE / "previous/q4t.release.json"),
        tool_source_commit=plan["source_commit"], tool_acceptance=str(result),
        tool_acceptance_sha256=h.sha(result), inherited_acceptance=inherited,
        implementation_capabilities=caps,
        scope=dict(hardware="local Thor", heads=["full"], max_seq=[1, 2],
            max_len=[8192], explicit_capacities=[2048, 8192], max_prefill=2048,
            ordinary_decode=["default MTP k3", "plain"],
            constrained_decode="plain_host_masked_greedy",
            installation_health="actual build/q4t --no-mtp JSON/SSE and preflight",
            mtp_installation_evidence="same-SHA candidate HTTP before install",
            preflight="text only; no slot/lease/cache/grammar/model side effects",
            performance_acceptance=False,
            numerical_evidence=str(PACKAGE / "numerical-reuse.json")))
    if "scope_note" in plan:
        metadata["scope"]["fixture_correction_and_reuse"] = plan["scope_note"]
    metadata["chat_protocol"].update(protocol_sha256=plan["protocol_sha256"],
        template=caps["template"], preflight=caps["preflight"],
        preflight_schema=str(ROOT / SCHEMAS[-2]),
        preflight_schema_sha256=h.sha(ROOT / SCHEMAS[-2]),
        config_schema=str(ROOT / SCHEMAS[-1]), config_schema_sha256=h.sha(ROOT / SCHEMAS[-1]))
    directory = OUTPUT / "release"
    directory.mkdir()
    h.save(directory / "q4t.release.json", metadata)
    h.save(directory / "binding.json", dict(old=plan["old_hashes"], new=dict(
        q4t=plan["candidate_sha256"], **{"q4t.release.json": h.sha(directory / "q4t.release.json")})))
    h.seal(directory)
    return directory


def bind():
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT / "build/serve-preflight-recovery.json"
    a.check, a.verifier, a.release = check, verifier, release


def guard_command(corrected=False):
    return ["/usr/bin/python3", "-B", str(ROOT / "tools/evalscope/gpu_exclusive.py"),
        "--output", str(OUTPUT / "lease"), "--", str(SDK_PYTHON), "-B",
        str(Path(__file__).resolve()), "execute", *(["--corrected"] if corrected else [])]


def main():
    global PACKAGE, OUTPUT
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["prepare", "launch", "execute", "finalize", "rollback"])
    ap.add_argument("--output", type=Path)
    ap.add_argument("--corrected", action="store_true")
    args = ap.parse_args()
    if args.corrected:
        PACKAGE, OUTPUT = BASE / "package-corrected", BASE / "execution-corrected"
    bind()
    if args.action == "prepare":
        prepare()
    elif args.action == "launch":
        h.require(sdk_environment() == check()["sdk_environment"], "SDK drift")
        raise SystemExit(subprocess.run(guard_command(args.corrected), cwd=ROOT).returncode)
    elif args.action == "execute":
        h.require_guard(OUTPUT)
        h.save(OUTPUT / "begin.json", dict(started=time.time(), limits=LIMITS))
        a.execute()
    elif args.action == "finalize":
        a.finalize()
    else:
        h.require(args.output is not None, "fresh lease output required")
        a.restore(args.output.resolve())


if __name__ == "__main__":
    main()
