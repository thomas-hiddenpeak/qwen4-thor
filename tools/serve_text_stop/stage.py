"""Identity-bound text-stop delivery using the existing guarded pair/rollback path."""
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


a = module("stop_application_delivery", ROOT / "tools/application_service/stage.py")
h = a.h
BASE = ROOT / ".q4t-work/serve-text-stop-20261103"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
LIMITS = dict(starts=4, generations=22, negatives=16, seconds=1800)
OLD_SHA = "31ffc41efefe7e407a0848041e03673906e8965865b9464019aa54a99fd5dee6"
SCHEMAS = ("docs/serve-transport-v1.schema.json",
           "docs/serve-message-profile-v2.schema.json",
           "docs/serve-reasoning-v1.schema.json",
           "docs/serve-thinking-v1.schema.json",
           "docs/serve-stop-v1.schema.json")
RELEASE_VERSION = "serve-text-stop-20261103"


def cpu_gate():
    cpu = BASE / "cpu"
    for name in ("host-gate/result.json", "template-reuse.json",
                 "startup-preflight/result.json", "driver-launch-result.json",
                 "client-result.json"):
        h.require(h.read(cpu / name)["passed"], "CPU gate failed: " + name)
    for mode, count in (("on", 197), ("off", 188)):
        log = (cpu / ("host-" + mode + ".log")).read_text()
        h.require(f"{count} tests, {count - 1} passed, 1 failed, 0 skipped" in log
                  and [line for line in log.splitlines() if line.startswith("[FAIL]")]
                  == ["[FAIL] text_stop_incomplete_tool_never_succeeds"],
                  "unexpected original host failure")
        h.require("1 tests, 1 passed, 0 failed, 0 skipped" in
                  (cpu / ("host-" + mode + "-fixture.log")).read_text(),
                  "corrected native prompt fixture failed")
    h.require("9 tests, 9 passed, 0 failed, 0 skipped" in
              (cpu / "backend-gate.log").read_text(), "backend gate incomplete")
    for log in ("build-on-corrected.log", "build-off-corrected.log",
                "build-on-fixture.log", "build-off-fixture.log",
                "build-engine.log", "build-engine-final.log", "build-engine-format.log"):
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
    runtime_paths = set(previous) | {"src/server/text_stop.cpp",
                                     "include/q4t/server/text_stop.h"}
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
        planned=dict(starts=3, generations=21, negatives=16),
        startup_preflight=h.identity(PACKAGE / "candidate/startup_preflight"))
    if PACKAGE.name == "package-absolute":
        prior_output = BASE / "execution"
        prior_package = BASE / "package"
        h.verify_seal(prior_package)
        h.require(set(p.name for p in prior_output.iterdir()) == {"lease"},
                  "guard retry requires zero model/request execution")
        failure = h.read(prior_output / "lease/exit.json")
        h.require(failure["status"] == "COMMAND_FAILED" and
                  "no matching deployment guard ancestor" in
                  (BASE / "launch.log").read_text(), "unexpected guard failure")
        first = h.read(prior_package / "plan.json")
        h.require(plan["candidate_sha256"] == first["candidate_sha256"],
                  "guard retry cannot change runtime")
        events = [json.loads(line) for line in
                  (prior_output / "lease/events.jsonl").read_text().splitlines()]
        plan["original_started"] = datetime.datetime.fromisoformat(
            events[0]["utc"]).timestamp()
        plan["prior_invocation"] = dict(generations=0, negatives=0, starts=0,
            guard_attempts=1, output=str(prior_output),
            exit=h.identity(prior_output / "lease/exit.json"),
            log=h.identity(BASE / "launch.log"))
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
    v = module("stop_live_checks", Path(__file__).with_name("verify.py"))
    v.Run(plan, OUTPUT / ("health" if installed else "business"), installed=installed).run()


def release(plan):
    result = OUTPUT / "business/result.json"
    h.require(h.read(result)["status"] == "PASS_BOUNDED_TEXT_STOP", "business gate")
    caps = json.loads(h.read(OUTPUT / "business/candidate-mtp/capabilities.json")["raw"])
    h.require(caps["structured_output"]["available"] and
              caps["template"]["message_profile"] == "qwen3.8-native-v2" and
              caps["text_stop"]["profile"] == "q4t-text-stop-v1", "capability gate")
    metadata = copy.deepcopy(h.read(PACKAGE / "previous/q4t.release.json"))
    inherited = copy.deepcopy({k: metadata[k] for k in ("acceptance", "acceptance_decision",
        "acceptance_decision_sha256", "chat_protocol", "deployment_completion", "scope") if k in metadata})
    inherited["release"] = h.identity(PACKAGE / "previous/q4t.release.json")
    metadata.pop("deployment_completion", None)
    metadata.update(version=RELEASE_VERSION,
        binary_sha256=plan["candidate_sha256"], source_commit=plan["source_commit"],
        source_note="Bounded text stop on normalized generated text before parsing. Shared JSON/SSE holdback and logical token usage; MTP commits full KV then retires the stopped request. Incomplete tools fail; active stop with constraints or caches is rejected. Model/numerical sources unchanged; not performance acceptance.",
        tested_source_manifest=str(PACKAGE / "candidate/runtime-source.json"),
        tested_source_manifest_sha256=h.sha(PACKAGE / "candidate/runtime-source.json"),
        build_cache=str(PACKAGE / "candidate/CMakeCache.txt"),
        build_cache_sha256=h.sha(PACKAGE / "candidate/CMakeCache.txt"),
        acceptance="ACCEPT_BOUNDED_TEXT_STOP",
        acceptance_decision=str(result), acceptance_decision_sha256=h.sha(result),
        previous_binary=str(PACKAGE / "previous/q4t"),
        previous_release=str(PACKAGE / "previous/q4t.release.json"),
        tool_source_commit=plan["source_commit"], tool_acceptance=str(result),
        tool_acceptance_sha256=h.sha(result), inherited_acceptance=inherited,
        implementation_capabilities=caps,
        scope=dict(hardware="local Thor", heads=["full"], max_seq=1, max_len=[8192],
            text_stop="max 4 strings, 256 UTF-8 bytes each; constraints/cache excluded",
            max_prefill=2048, ordinary_decode=["default MTP k3", "plain"],
            constrained_decode="plain_host_masked_greedy", performance_acceptance=False,
            numerical_evidence=str(PACKAGE / "numerical-reuse.json")))
    metadata["chat_protocol"].update(protocol_sha256=plan["protocol_sha256"],
        template=caps["template"], message_profile="qwen3.8-native-v2",
        message_schema=str(ROOT / SCHEMAS[1]), message_schema_sha256=h.sha(ROOT / SCHEMAS[1]),
        reasoning=caps["reasoning"], reasoning_schema=str(ROOT / SCHEMAS[2]),
        reasoning_schema_sha256=h.sha(ROOT / SCHEMAS[2]),
        thinking_schema=str(ROOT / SCHEMAS[3]),
        thinking_schema_sha256=h.sha(ROOT / SCHEMAS[3]),
        text_stop=caps["text_stop"], stop_schema=str(ROOT / SCHEMAS[4]),
        stop_schema_sha256=h.sha(ROOT / SCHEMAS[4]))
    directory = OUTPUT / "release"
    directory.mkdir()
    h.save(directory / "q4t.release.json", metadata)
    h.save(directory / "binding.json", dict(old=plan["old_hashes"], new=dict(
        q4t=plan["candidate_sha256"], **{"q4t.release.json": h.sha(directory / "q4t.release.json")})))
    h.seal(directory)
    return directory


def bind():
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT / "build/serve-text-stop-recovery.json"
    a.check, a.verifier, a.release = check, verifier, release


def guard_command(retry=False):
    # The deployment guard validates exact absolute argv, not only the lock.
    return ["/usr/bin/python3", "-B", str(ROOT / "tools/evalscope/gpu_exclusive.py"),
            "--output", str(OUTPUT / "lease"), "--", "/usr/bin/python3", "-B",
            str(Path(__file__).resolve()), "execute", *(["--guard-retry"] if retry else [])]


def main():
    global PACKAGE, OUTPUT
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["prepare", "launch", "execute", "finalize", "rollback"])
    ap.add_argument("--output", type=Path)
    ap.add_argument("--guard-retry", action="store_true")
    args = ap.parse_args()
    if args.guard_retry:
        PACKAGE, OUTPUT = BASE / "package-absolute", BASE / "execution-absolute"
    bind()
    if args.action == "prepare":
        prepare()
    elif args.action == "launch":
        raise SystemExit(subprocess.run(guard_command(args.guard_retry), cwd=ROOT).returncode)
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
