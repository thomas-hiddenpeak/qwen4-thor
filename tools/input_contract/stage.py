"""Freeze, run, install and recover a bounded serve input-contract release.

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
BASE = ROOT / ".q4t-work/input-contract-20261102"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
LIMITS = dict(starts=3, generations=10, negatives=24, seconds=1800)


def prepare():
    h.require(not PACKAGE.exists(), "package already frozen")
    old = h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json",
        "957211c7807a5189e5c124b13d52c4fe5a95a4213dfbce087007783cb479d5fc")
    cpu = BASE / "cpu"
    h.require(h.read(cpu / "host-gate/result.json")["passed"], "host gate failed")
    for log, terminal in [("host-on.log", "152 tests, 152 passed, 0 failed, 0 skipped"),
            ("host-off.log", "147 tests, 147 passed, 0 failed, 0 skipped"),
            ("backend-gate.log", "5 tests, 5 passed, 0 failed, 0 skipped"),
            ("template.log", "cases=21 failed=0")]:
        h.require(terminal in (cpu / log).read_text(), "incomplete CPU gate: " + log)
    for log in ("build-on.log", "build-off.log", "build-engine-repair.log", "build-template.log"):
        text = (cpu / log).read_text()
        h.require("warning:" not in text and "error:" not in text, "build diagnostics")
    original = (cpu / "build-engine.log").read_text()
    h.require("warning:" not in original and "[100%] Built target q4t" in original
              and "[100%] Built target q4t_host_tests" in original,
              "engine/host build missing; original HTTP-only link failure retained")
    h.require(h.read(cpu / "driver-result.json")["passed"], "driver contracts failed")
    # Check required build records before creating even a partial package.
    for name in ("q4t", "CMakeCache.txt", "compile_commands.json",
                 "q4t_http_lifecycle_tests"):
        h.identity(BASE / "engine" / name)
    PACKAGE.mkdir()
    for name in ("previous", "candidate"):
        (PACKAGE / name).mkdir()
    shutil.copytree(cpu, PACKAGE / "cpu")
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT / "build" / name), PACKAGE / "previous" / name)
    h.copy_checked(h.identity(old["build_cache"]), PACKAGE / "previous/CMakeCache.txt")
    for name in ("q4t", "CMakeCache.txt", "compile_commands.json"):
        h.copy_checked(h.identity(BASE / "engine" / name), PACKAGE / "candidate" / name)
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
    h.save(PACKAGE / "sources.json", sources)
    protocol = h.read(generated / "chat_protocol_sources.json")
    protocol_sha = hashlib.sha256("".join(f"{p}:{protocol[p]}\n"
        for p in sorted(protocol)).encode()).hexdigest()
    prior = h.read(ROOT / ".q4t-work/application-service-20261102/package/plan.json")
    model = Path(prior["model_dir"])
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                     text=True).strip()
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "795d980..HEAD"], cwd=ROOT, check=True)
    plan = dict(schema_version=1, source_commit=commit,
        candidate=str(PACKAGE / "candidate/q4t"),
        candidate_sha256=h.sha(PACKAGE / "candidate/q4t"),
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
    if (OUTPUT / "begin.json").exists():
        plan["started"] = h.read(OUTPUT / "begin.json")["started"]
        plan["original_started"] = plan["started"]
    return plan


def verifier(plan, installed=False):
    v = module("input_wire_checks", Path(__file__).with_name("verify.py"))
    v.Run(plan, OUTPUT / ("health" if installed else "business"), installed=installed).run()


def release(plan):
    result = OUTPUT / "business/result.json"
    h.require(h.read(result)["status"] == "PASS_BOUNDED_INPUT_CONTRACT", "business gate")
    caps = json.loads(h.read(OUTPUT / "business/candidate-mtp/capabilities.json")["raw"])
    h.require(caps["structured_output"]["available"] is True, "backend absent")
    metadata = copy.deepcopy(h.read(PACKAGE / "previous/q4t.release.json"))
    inherited = {k: metadata[k] for k in ("acceptance", "acceptance_decision",
        "acceptance_decision_sha256", "chat_protocol", "inherited_acceptance") if k in metadata}
    metadata.update(version="input-contract-20261102",
        binary_sha256=plan["candidate_sha256"], source_commit=plan["source_commit"],
        source_note="Serve input contracts repaired; real JSON/SSE project tool loops accepted. Numerical sources unchanged.",
        tested_source_manifest=str(PACKAGE / "candidate/runtime-source.json"),
        tested_source_manifest_sha256=h.sha(PACKAGE / "candidate/runtime-source.json"),
        build_cache=str(PACKAGE / "candidate/CMakeCache.txt"),
        build_cache_sha256=h.sha(PACKAGE / "candidate/CMakeCache.txt"),
        acceptance="ACCEPT_BOUNDED_INPUT_CONTRACT",
        acceptance_decision=str(result), acceptance_decision_sha256=h.sha(result),
        previous_binary=str(PACKAGE / "previous/q4t"),
        previous_release=str(PACKAGE / "previous/q4t.release.json"),
        tool_source_commit=plan["source_commit"], tool_acceptance=str(result),
        tool_acceptance_sha256=h.sha(result), inherited_acceptance=inherited,
        implementation_capabilities=caps)
    metadata["chat_protocol"].update(protocol_sha256=plan["protocol_sha256"],
        structured_output=caps["structured_output"], tool_control=caps["tools"],
        input_contract=caps["chat"]["input_contract"],
        null_as_default=caps["chat"]["null_as_default"], cancellation=caps["cancellation"],
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
    a.MARKER = ROOT / "build/input-contract-recovery.json"
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
        plan = check()
        with (OUTPUT / "http-contract.log").open("x") as log:
            subprocess.run([plan["http_contract"]["path"]], cwd=ROOT, check=True,
                           stdout=log, stderr=subprocess.STDOUT, timeout=30)
        h.require("5 tests, 5 passed, 0 failed, 0 skipped" in
                  (OUTPUT / "http-contract.log").read_text(), "HTTP gate incomplete")
        a.execute()
    elif args.action == "finalize":
        a.finalize()
    else:
        h.require(args.output is not None, "fresh lease output required")
        a.restore(args.output.resolve())


if __name__ == "__main__":
    main()
