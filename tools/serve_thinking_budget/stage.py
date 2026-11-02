"""Identity-bound thinking-budget delivery using the existing guarded pair/rollback path."""
import argparse
import copy
import hashlib
import importlib.util
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


a = module("thinking_application_delivery", ROOT / "tools/application_service/stage.py")
h = a.h
BASE = ROOT / ".q4t-work/serve-thinking-budget-20261103"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
LIMITS = dict(starts=4, generations=22, negatives=12, seconds=1800)
OLD_SHA = "ecd5d21f7b3a9adb221b17a352499df5a5803020f15149ed6cef978c916f8dcc"
SCHEMAS = ("docs/serve-transport-v1.schema.json",
           "docs/serve-message-profile-v2.schema.json",
           "docs/serve-reasoning-v1.schema.json",
           "docs/serve-thinking-v1.schema.json")


def cpu_gate():
    cpu = BASE / "cpu"
    for name in ("host-gate/result.json", "template-reuse.json", "cache-probe-result.json",
                 "startup-preflight/result.json",
                 "driver-result.json", "client-result.json"):
        h.require(h.read(cpu / name)["passed"], "CPU gate failed: " + name)
    for log, total in (("host-on.log", 188), ("host-off.log", 179),
                       ("backend-gate.log", 9)):
        h.require(f"{total} tests, {total} passed, 0 failed, 0 skipped" in
                  (cpu / log).read_text(), "incomplete CPU gate: " + log)
    for log in ("build-on.log", "build-off.log", "build-engine.log"):
        text = (cpu / log).read_text()
        h.require("warning:" not in text and "error:" not in text, "build diagnostics")


def prepare():
    h.require(not PACKAGE.exists(), "package already frozen")
    old = h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json", OLD_SHA)
    cpu_gate()
    cache = BASE / "engine/CMakeCache.txt"
    h.require("Q4T_SERVE_FAULT_INJECTION:BOOL=OFF" in cache.read_text(),
              "faults in delivery binary")
    generated = BASE / "engine/generated/q4t/server"
    protocol = h.read(generated / "chat_protocol_sources.json")
    a.verify_hashes(ROOT, protocol)
    # A source commit is the recovery anchor; do not bundle unrelated dirt.
    h.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
              "commit the reviewed stage before freezing")
    PACKAGE.mkdir()
    for name in ("previous", "candidate"):
        (PACKAGE / name).mkdir()
    shutil.copytree(BASE / "cpu", PACKAGE / "cpu")
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT / "build" / name), PACKAGE / "previous" / name)
    h.copy_checked(h.identity(old["build_cache"]), PACKAGE / "previous/CMakeCache.txt")
    for name in ("q4t", "CMakeCache.txt", "compile_commands.json"):
        h.copy_checked(h.identity(BASE / "engine" / name), PACKAGE / "candidate" / name)
    preflight = h.read(BASE / "cpu/startup-preflight/result.json")["binary"]
    h.copy_checked(preflight, PACKAGE / "candidate/startup_preflight")
    for name in ("chat_protocol_identity.h", "chat_protocol_sources.json"):
        h.copy_checked(h.identity(generated / name), PACKAGE / "candidate" / name)
    previous = h.read(old["tested_source_manifest"])
    runtime_paths = set(previous) | {"src/server/thinking_budget.cpp",
                                     "include/q4t/server/thinking_budget.h"}
    current = {name: h.sha(ROOT / name) for name in sorted(runtime_paths)}
    changed = [name for name in previous if previous[name] != current[name]]
    h.require(all(name.startswith(("src/server/", "include/q4t/server/"))
                  for name in changed), "model/numerical sources changed")
    h.save(PACKAGE / "candidate/runtime-source.json", current)
    h.save(PACKAGE / "numerical-reuse.json", dict(changed=changed,
        added=sorted(runtime_paths - previous.keys()),
        previous_manifest=old["tested_source_manifest"],
        unchanged_model_and_numerical_sources=True, performance_acceptance=False))
    sources = {name: h.sha(ROOT / name) for name in a.acceptance.source_paths()}
    sources.update({name: h.sha(ROOT / name) for name in SCHEMAS})
    h.save(PACKAGE / "sources.json", sources)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                     text=True).strip()
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "10b3442..HEAD"], cwd=ROOT, check=True)
    prior = h.read(ROOT / ".q4t-work/serve-stage-a-final-20261102/package/plan.json")
    model = Path(prior["model_dir"])
    plan = dict(schema_version=1, source_commit=commit,
        candidate=str(PACKAGE / "candidate/q4t"),
        candidate_sha256=h.sha(PACKAGE / "candidate/q4t"),
        old_hashes={n: h.sha(PACKAGE / "previous" / n) for n in a.NAMES},
        previous_binary_sha256=OLD_SHA,
        protocol_sha256=hashlib.sha256("".join(f"{p}:{protocol[p]}\n"
            for p in sorted(protocol)).encode()).hexdigest(),
        model_dir=str(model), model_inputs={n: h.sha(model / n) for n in prior["model_inputs"]},
        sdk_version="3.42.1", limits=LIMITS,
        source_manifest_sha256=h.sha(PACKAGE / "sources.json"),
        planned=dict(starts=3, generations=19, negatives=12),
        startup_preflight=h.identity(PACKAGE / "candidate/startup_preflight"),
        cache_seed_tokens=h.read(BASE / "cpu/cache-probe-result.json")["tokens"])
    if PACKAGE.name == "package-correction":
        original = BASE / "execution"
        h.verify_seal(BASE / "package")
        failed = h.read(original / "business/result.json")
        h.require(failed["status"] == "FAILED" and failed["request_attempts"] == 0 and
                  failed["negative_requests"] == 0 and failed["starts"] == 1 and
                  h.read(original / "lease/exit.json")["status"] == "COMMAND_FAILED",
                  "correction only covers the one pre-model CLI startup failure")
        h.require("state caches require text k=3, explicit slots" in
                  (original / "business/candidate-mtp/server.log").read_text(),
                  "unexpected original failure")
        h.require(plan["candidate_sha256"] ==
                  h.read(BASE / "package/plan.json")["candidate_sha256"],
                  "CLI correction must not change runtime binary")
        plan.update(original_started=h.read(original / "begin.json")["started"],
                    prior_counts=dict(starts=1, generations=0, negatives=0),
                    prior_execution=str(original))
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
    v = module("thinking_live_checks", Path(__file__).with_name("verify.py"))
    v.Run(plan, OUTPUT / ("health" if installed else "business"), installed=installed).run()


def release(plan):
    result = OUTPUT / "business/result.json"
    h.require(h.read(result)["status"] == "PASS_BOUNDED_THINKING_BUDGET", "business gate")
    import json
    caps = json.loads(h.read(OUTPUT / "business/candidate-mtp/capabilities.json")["raw"])
    h.require(caps["structured_output"]["available"] and
              caps["template"]["message_profile"] == "qwen3.8-native-v2" and
              caps["reasoning"]["budget_profile"] == "q4t-thinking-v1", "capability gate")
    metadata = copy.deepcopy(h.read(PACKAGE / "previous/q4t.release.json"))
    inherited = copy.deepcopy({k: metadata[k] for k in ("acceptance", "acceptance_decision",
        "acceptance_decision_sha256", "chat_protocol", "deployment_completion", "scope") if k in metadata})
    inherited["release"] = h.identity(PACKAGE / "previous/q4t.release.json")
    metadata.pop("deployment_completion", None)
    metadata.update(version="serve-thinking-budget-20261103",
        binary_sha256=plan["candidate_sha256"], source_commit=plan["source_commit"],
        source_note="True thinking token admission and finite forced closure accepted for the frozen HTTP cases. MTP reserves a natural pending token before host-tail fallback; strict selection intersects grammar and forced token. Hidden output retains usage. Model/numerical sources unchanged; this is not a performance acceptance.",
        tested_source_manifest=str(PACKAGE / "candidate/runtime-source.json"),
        tested_source_manifest_sha256=h.sha(PACKAGE / "candidate/runtime-source.json"),
        build_cache=str(PACKAGE / "candidate/CMakeCache.txt"),
        build_cache_sha256=h.sha(PACKAGE / "candidate/CMakeCache.txt"),
        acceptance="ACCEPT_BOUNDED_THINKING_BUDGET",
        acceptance_decision=str(result), acceptance_decision_sha256=h.sha(result),
        previous_binary=str(PACKAGE / "previous/q4t"),
        previous_release=str(PACKAGE / "previous/q4t.release.json"),
        tool_source_commit=plan["source_commit"], tool_acceptance=str(result),
        tool_acceptance_sha256=h.sha(result), inherited_acceptance=inherited,
        implementation_capabilities=caps,
        scope=dict(hardware="local Thor", heads=["full"], max_seq=1, max_len=[8192, 16384],
            session_cache="private SSD snapshot >=8192; real restore and fresh-answer comparison",
            max_prefill=2048, ordinary_decode=["default MTP k3", "plain"],
            constrained_decode="plain_host_masked_greedy", performance_acceptance=False,
            numerical_evidence=str(PACKAGE / "numerical-reuse.json")))
    metadata["chat_protocol"].update(protocol_sha256=plan["protocol_sha256"],
        template=caps["template"], message_profile="qwen3.8-native-v2",
        message_schema=str(ROOT / SCHEMAS[1]), message_schema_sha256=h.sha(ROOT / SCHEMAS[1]),
        reasoning=caps["reasoning"], reasoning_schema=str(ROOT / SCHEMAS[2]),
        reasoning_schema_sha256=h.sha(ROOT / SCHEMAS[2]),
        thinking_schema=str(ROOT / SCHEMAS[3]),
        thinking_schema_sha256=h.sha(ROOT / SCHEMAS[3]))
    directory = OUTPUT / "release"
    directory.mkdir()
    h.save(directory / "q4t.release.json", metadata)
    h.save(directory / "binding.json", dict(old=plan["old_hashes"], new=dict(
        q4t=plan["candidate_sha256"], **{"q4t.release.json": h.sha(directory / "q4t.release.json")})))
    h.seal(directory)
    return directory


def bind():
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT / "build/serve-thinking-budget-recovery.json"
    a.check, a.verifier, a.release = check, verifier, release


def main():
    global PACKAGE, OUTPUT
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["prepare", "execute", "finalize", "rollback"])
    ap.add_argument("--output", type=Path)
    ap.add_argument("--correction", action="store_true",
                    help="one retained pre-model CLI startup failure; original limits apply")
    args = ap.parse_args()
    if args.correction:
        PACKAGE, OUTPUT = BASE / "package-correction", BASE / "execution-correction"
    bind()
    if args.action == "execute":
        h.require(not (BASE / "retired.json").exists(),
                  "phase closed without HTTP acceptance; preserve evidence and plan a fresh bounded phase")
    if args.action == "prepare":
        prepare()
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
