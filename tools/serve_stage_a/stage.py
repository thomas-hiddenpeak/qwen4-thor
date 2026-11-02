"""Freeze, run, install and recover a bounded serve transport/readiness release.

Reuse the accepted identity-bound pair replacement and rollback implementation.
GPU work and installation require the same whole-stage lease. No auto retry.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


a = module("application_delivery", ROOT / "tools/application_service/stage.py")
h = a.h
BASE = ROOT / ".q4t-work/serve-stage-a-20261102"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
LIMITS = dict(starts=3, generations=12, negatives=16, seconds=1800)


def prepare():
    h.require(not PACKAGE.exists(), "package already frozen")
    old = h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json",
        "c8c412c4ab0f24c238d83e3e5def78baf4b25c99883eda382b0da7f560ed15d4")
    cpu = BASE / "cpu"
    h.require(h.read(cpu / "host-gate/result.json")["passed"], "host gate failed")
    for log, terminal in [("host-on.log", "169 tests, 169 passed, 0 failed, 0 skipped"),
            ("host-off.log", "162 tests, 162 passed, 0 failed, 0 skipped"),
            ("backend-gate.log", "7 tests, 7 passed, 0 failed, 0 skipped")]:
        h.require(terminal in (cpu / log).read_text(), "incomplete CPU gate: " + log)
    for log in ("build-on.log", "build-off.log", "build-engine.log",
                "build-fault.log", "build-fault-final.log", "build-final.log",
                "build-comment.log"):
        text = (cpu / log).read_text()
        h.require("warning:" not in text and "error:" not in text, "build diagnostics")
    h.require(h.read(cpu / "driver-result.json")["passed"], "driver contracts failed")
    # Check required build records before creating even a partial package.
    for name in ("q4t", "CMakeCache.txt", "compile_commands.json",
                 "q4t_http_lifecycle_tests"):
        h.identity(BASE / "engine" / name)
    h.require(h.read(BASE / "cpu-http/lease/exit.json")["status"] == "COMPLETED",
              "native HTTP gate failed")
    production = h.read(BASE / "engine/generated/q4t/server/chat_protocol_sources.json")
    fault = h.read(BASE / "fault-engine/generated/q4t/server/chat_protocol_sources.json")
    h.require(production == fault, "validation/production source identity differs")
    a.verify_hashes(ROOT, production)
    h.require("Q4T_SERVE_FAULT_INJECTION:BOOL=OFF" in
              (BASE / "engine/CMakeCache.txt").read_text(), "faults in delivery binary")
    h.require("Q4T_SERVE_FAULT_INJECTION:BOOL=ON" in
              (BASE / "fault-engine/CMakeCache.txt").read_text(), "missing validation faults")
    PACKAGE.mkdir()
    for name in ("previous", "candidate"):
        (PACKAGE / name).mkdir()
    shutil.copytree(cpu, PACKAGE / "cpu")
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT / "build" / name), PACKAGE / "previous" / name)
    h.copy_checked(h.identity(old["build_cache"]), PACKAGE / "previous/CMakeCache.txt")
    for name in ("q4t", "CMakeCache.txt", "compile_commands.json"):
        h.copy_checked(h.identity(BASE / "engine" / name), PACKAGE / "candidate" / name)
    for name in ("q4t", "CMakeCache.txt", "compile_commands.json"):
        h.copy_checked(h.identity(BASE / "fault-engine" / name),
                       PACKAGE / "candidate" / ("fault-" + name))
    generated = BASE / "engine/generated/q4t/server"
    for name in ("chat_protocol_identity.h", "chat_protocol_sources.json"):
        shutil.copy2(generated / name, PACKAGE / "candidate" / name)
    runtime = h.read(old["tested_source_manifest"])
    names = set(runtime) | {str(p.relative_to(ROOT)) for directory in
        (ROOT / "src/server", ROOT / "include/q4t/server") for p in directory.iterdir()
        if p.suffix in (".cpp", ".h")}
    current = {name: h.sha(ROOT / name) for name in sorted(names)}
    changed = [name for name, digest in runtime.items() if current[name] != digest]
    h.require(all(name.startswith(("src/server/", "include/q4t/server/"))
                  for name in changed), "numerical/model source changed")
    h.save(PACKAGE / "candidate/runtime-source.json", current)
    h.save(PACKAGE / "numerical-reuse.json", dict(changed=changed,
        previous_manifest=old["tested_source_manifest"],
        unchanged_model_and_numerical_sources=True, performance_acceptance=False))
    sources = {name: h.sha(ROOT / name) for name in a.acceptance.source_paths()}
    sources.update({str(p.relative_to(ROOT)): h.sha(p) for p in
        (ROOT / "tools/serve_stage_a").glob("*.py")})
    sources["docs/serve-transport-v1.schema.json"] = h.sha(ROOT / "docs/serve-transport-v1.schema.json")
    h.save(PACKAGE / "sources.json", sources)
    protocol = h.read(generated / "chat_protocol_sources.json")
    protocol_sha = hashlib.sha256("".join(f"{p}:{protocol[p]}\n"
        for p in sorted(protocol)).encode()).hexdigest()
    prior = h.read(ROOT / ".q4t-work/application-service-20261102/package/plan.json")
    model = Path(prior["model_dir"])
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                     text=True).strip()
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "4a3f498..HEAD"], cwd=ROOT, check=True)
    plan = dict(schema_version=1, source_commit=commit,
        candidate=str(PACKAGE / "candidate/q4t"),
        candidate_sha256=h.sha(PACKAGE / "candidate/q4t"),
        fault_candidate=h.identity(PACKAGE / "candidate/fault-q4t"),
        old_hashes={n: h.sha(PACKAGE / "previous" / n) for n in a.NAMES},
        previous_binary_sha256=old["binary_sha256"], protocol_sha256=protocol_sha,
        model_dir=str(model), model_inputs={n: h.sha(model / n) for n in prior["model_inputs"]},
        sdk_version="3.42.1", limits=LIMITS,
        http_contract=h.identity(BASE / "engine/q4t_http_lifecycle_tests"),
        source_manifest_sha256=h.sha(PACKAGE / "sources.json"))
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
    h.require(h.identity(plan["http_contract"]["path"]) == plan["http_contract"],
              "HTTP contract binary changed")
    h.require(h.identity(plan["fault_candidate"]["path"]) == plan["fault_candidate"],
              "fault validation binary changed")
    if (OUTPUT / "begin.json").exists():
        plan["started"] = h.read(OUTPUT / "begin.json")["started"]
        plan["original_started"] = plan["started"]
    return plan


def verifier(plan, installed=False):
    v = module("transport_wire_checks", Path(__file__).with_name("verify.py"))
    v.Run(plan, OUTPUT / ("health" if installed else "business"), installed=installed).run()


def release(plan):
    result = OUTPUT / "business/result.json"
    h.require(h.read(result)["status"] == "PASS_BOUNDED_SERVE_STAGE_A", "business gate")
    caps = json.loads(h.read(OUTPUT / "business/candidate-mtp/capabilities.json")["raw"])
    h.require(caps["structured_output"]["available"] is True, "backend absent")
    metadata = copy.deepcopy(h.read(PACKAGE / "previous/q4t.release.json"))
    inherited = copy.deepcopy({k: metadata[k] for k in ("acceptance", "acceptance_decision",
        "acceptance_decision_sha256", "chat_protocol", "deployment_completion", "scope") if k in metadata})
    # The previous immutable sidecar is the authority for the older chain;
    # do not mutate or relabel historical protocol snapshots through aliases.
    inherited["release"] = h.identity(PACKAGE / "previous/q4t.release.json")
    metadata.pop("deployment_completion", None)
    metadata.update(version="serve-stage-a-20261102",
        binary_sha256=plan["candidate_sha256"], source_commit=plan["source_commit"],
        source_note="Request tracing, readiness/control reserve and serialized SSE heartbeat accepted within recorded bounds. Numerical/model sources unchanged.",
        tested_source_manifest=str(PACKAGE / "candidate/runtime-source.json"),
        tested_source_manifest_sha256=h.sha(PACKAGE / "candidate/runtime-source.json"),
        build_cache=str(PACKAGE / "candidate/CMakeCache.txt"),
        build_cache_sha256=h.sha(PACKAGE / "candidate/CMakeCache.txt"),
        acceptance="ACCEPT_BOUNDED_SERVE_STAGE_A",
        acceptance_decision=str(result), acceptance_decision_sha256=h.sha(result),
        previous_binary=str(PACKAGE / "previous/q4t"),
        previous_release=str(PACKAGE / "previous/q4t.release.json"),
        tool_source_commit=plan["source_commit"], tool_acceptance=str(result),
        tool_acceptance_sha256=h.sha(result), inherited_acceptance=inherited,
        implementation_capabilities=caps,
        scope=dict(hardware="local Thor", heads=["full"], max_seq=1, max_len=8192,
                   max_prefill=2048, ordinary_decode="default MTP k3",
                   constrained_decode="plain_host_masked_greedy",
                   performance_acceptance=False,
                   numerical_evidence=str(PACKAGE / "numerical-reuse.json")))
    metadata["chat_protocol"].update(protocol_sha256=plan["protocol_sha256"],
        tool_choice=caps["tools"]["tool_choice"],
        constrained_decoding=caps["tools"]["constrained_decoding"],
        stream=caps["chat"],
        structured_output=caps["structured_output"], tool_control=caps["tools"],
        input_contract=caps["chat"]["input_contract"],
        null_as_default=caps["chat"]["null_as_default"], cancellation=caps["cancellation"],
        observability=caps["observability"], http=caps["http"],
        response_schema=str(ROOT / "docs/serve-transport-v1.schema.json"),
        response_schema_sha256=h.sha(ROOT / "docs/serve-transport-v1.schema.json"),
        constrained_stream_payload="buffered_until_validated")
    directory = OUTPUT / "release"
    directory.mkdir()
    h.save(directory / "q4t.release.json", metadata)
    h.save(directory / "binding.json", dict(old=plan["old_hashes"], new=dict(
        q4t=plan["candidate_sha256"], **{"q4t.release.json": h.sha(directory / "q4t.release.json")})))
    h.seal(directory)
    return directory


def bind():
    # Accepted install/rollback code retains its own guards and foreign-state checks.
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT / "build/serve-stage-a-recovery.json"
    a.check, a.verifier, a.release = check, verifier, release


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["prepare", "execute", "finalize", "rollback"])
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    bind()
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
