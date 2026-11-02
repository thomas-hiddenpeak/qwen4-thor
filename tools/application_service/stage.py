"""Freeze, verify and deliver a bounded application service stage.

Preparation never changes the installed pair. Execute/rollback need the shared
GPU lease. Unknown identities and foreign recovery markers are not overwritten.
"""
import argparse
import ast
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
sys.path[:0] = [str(ROOT / "tools/verify/startup_delivery"),
               str(ROOT / "tools/response_protocol"),
               str(ROOT / "tools/evalscope")]
import deploy as h
import acceptance
import nvml_process_query

BASE = ROOT / ".q4t-work/application-service-20261102"
PACKAGE = BASE / "package"
OUTPUT = BASE / "execution"
MARKER = ROOT / "build/application-service-recovery.json"
NAMES = ("q4t", "q4t.release.json")


def verify_hashes(base, values):
    for name, digest in values.items():
        h.require(h.sha(Path(base) / name) == digest, "identity changed: " + name)


def state_pair(build, previous, candidate, old_hashes, new_hashes):
    # All source/destination checks precede either replacement.
    verify_hashes(previous, old_hashes)
    verify_hashes(candidate, new_hashes)
    state = {}
    for name in NAMES:
        h.require_regular(build / name)
        digest = h.sha(build / name)
        h.require(digest in {old_hashes[name], new_hashes[name]},
                  "unknown installed identity: " + name)
        state[name] = digest
    return state


def restore_pair(build, previous, candidate, old_hashes, new_hashes):
    state = state_pair(build, previous, candidate, old_hashes, new_hashes)
    for name in NAMES:
        if state[name] != old_hashes[name]:
            h.replace_checked(previous / name, build / name, state[name])
    verify_hashes(build, old_hashes)


def prepare():
    h.require(not PACKAGE.exists(), "stage already frozen; no implicit overwrite")
    old_hashes = {name: h.sha(BASE / "previous" / name) for name in NAMES}
    verify_hashes(ROOT / "build", old_hashes)
    old = h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json",
                        old_hashes["q4t"])
    h.require(all(row["returncode"] == 0 for row in
                  h.read(BASE / "cpu/result.json")), "CPU contracts failed")
    h.require(all(row["returncode"] == 0 for row in
                  h.read(BASE / "cpu/driver-result.json")), "driver contracts failed")
    h.require("warning:" not in (BASE / "build.log").read_text(),
              "build warning")
    h.require(not list((ROOT / "build").glob("*recovery*.json")),
              "pending recovery marker")
    PACKAGE.mkdir()
    for name in ("previous", "candidate"):
        (PACKAGE / name).mkdir()
    for name in NAMES:
        h.copy_checked(h.identity(BASE / "previous" / name),
                       PACKAGE / "previous" / name)
    h.copy_checked(h.identity(old["build_cache"]), PACKAGE / "previous/CMakeCache.txt")
    for source, target in [("q4t-candidate", "q4t"),
                           ("candidate-CMakeCache.txt", "CMakeCache.txt"),
                           ("chat_protocol_identity.h", "chat_protocol_identity.h"),
                           ("chat_protocol_sources.json", "chat_protocol_sources.json")]:
        h.copy_checked(h.identity(BASE / source), PACKAGE / "candidate" / target)
    previous_runtime = h.read(old["tested_source_manifest"])
    runtime = {name: h.sha(ROOT / name) for name in previous_runtime}
    h.save(PACKAGE / "candidate/runtime-source.json", runtime)
    protocol = h.read(BASE / "chat_protocol_sources.json")
    protocol_sha = hashlib.sha256("".join(f"{p}:{protocol[p]}\n"
        for p in sorted(protocol)).encode()).hexdigest()
    sources = {name: h.sha(ROOT / name) for name in acceptance.source_paths()}
    h.save(PACKAGE / "sources.json", sources)
    for source in ["build.log", "link.log", "link-command.json"]:
        shutil.copy2(BASE / source, PACKAGE / source)
    shutil.copytree(BASE / "cpu", PACKAGE / "cpu")
    # Keep the driver and its reusable helper dependencies at this source anchor.
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                     cwd=ROOT, text=True).strip()
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "460ffd0..HEAD"], cwd=ROOT, check=True)
    model_dir = str(Path.home() / "models/dev/llm/garnermccloud/"
                   "Qwen3.8-Flash-Next-NVFP4-SSD-Stream")
    prior_plan = h.read(ROOT / "docs/evidence/chat-business-runtime-20261102/"
                              "corrected-plan.json")
    model_inputs = {name: h.sha(Path(model_dir) / name)
                    for name in prior_plan["model_inputs"]}
    plan = dict(schema_version=1, source_commit=commit,
        candidate=str(PACKAGE / "candidate/q4t"),
        candidate_sha256=h.sha(PACKAGE / "candidate/q4t"),
        previous_binary_sha256=old_hashes["q4t"], old_hashes=old_hashes,
        protocol_sha256=protocol_sha, model_dir=model_dir,
        model_inputs=model_inputs, sdk_version="3.42.1",
        limits=dict(starts=3, generations=16, negatives=12, seconds=1800),
        source_manifest_sha256=h.sha(PACKAGE / "sources.json"))
    if (BASE / "correction.json").exists():
        correction = h.read(BASE / "correction.json")
        original = ROOT / ".q4t-work/application-service-20261102"
        h.verify_seal(original / "package")
        h.verify_seal(original / "execution")
        old_plan = h.read(original / "package/plan.json")
        h.require(old_plan["candidate_sha256"] == plan["candidate_sha256"] and
                  old_plan["protocol_sha256"] == plan["protocol_sha256"],
                  "cannot reuse HTTP evidence for changed runtime")
        # Reuse only SDK paths whose implementation is identical, not the
        # failed project query or an overall PASS that does not exist.
        old_source = subprocess.check_output(["git", "show",
            old_plan["source_commit"] + ":tools/application_service/verify.py"],
            cwd=ROOT, text=True)
        new_source = (ROOT / "tools/application_service/verify.py").read_text()
        def methods(source):
            tree = ast.parse(source)
            cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and
                       n.name == "Run")
            return {n.name: ast.dump(n, include_attributes=False)
                    for n in cls.body if isinstance(n, ast.FunctionDef)}
        old_methods, new_methods = methods(old_source), methods(new_source)
        for name in ("sdk", "sdk_chat", "request_body"):
            h.require(old_methods[name] == new_methods[name], "SDK path changed")
        h.require(correction["status"] == "PASS" and
                  h.read(original / "execution/lease/exit.json")["status"] ==
                  "COMMAND_FAILED", "correction prerequisites")
        plan.update(reuse_sdk_http=str(original / "execution"),
                    original_started=h.read(original / "execution/execution.json")["started"],
                    prior_counts=dict(starts=1, requests=8, negatives=9))
        plan["limits"] = dict(starts=4, generations=20, negatives=12, seconds=1800)
        shutil.copy2(BASE / "correction.json", PACKAGE / "correction.json")
    h.save(PACKAGE / "plan.json", plan)
    h.seal(PACKAGE)
    print("PREPARED", plan["candidate_sha256"], flush=True)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE / "plan.json")
    h.require(h.sha(PACKAGE / "sources.json") == plan["source_manifest_sha256"],
              "source manifest changed")
    verify_hashes(ROOT, h.read(PACKAGE / "sources.json"))
    verify_hashes(plan["model_dir"], plan["model_inputs"])
    h.require(h.sha(plan["candidate"]) == plan["candidate_sha256"],
              "candidate changed")
    return plan


def verifier(plan, installed=False):
    spec = importlib.util.spec_from_file_location("application_checks",
        ROOT / "tools/application_service/verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.Run(plan, OUTPUT / ("health" if installed else "business"),
               installed=installed).run()


def release(plan):
    old = h.read(PACKAGE / "previous/q4t.release.json")
    metadata = copy.deepcopy(old)
    caps = json.loads(h.read(OUTPUT / "business/candidate-mtp/capabilities.json")["raw"])
    result = OUTPUT / "business/result.json"
    h.require(h.read(result)["status"] == "PASS_BOUNDED_APPLICATION_SERVICE",
              "no accepted business evidence")
    metadata.update(version="application-service-20261102",
        binary_sha256=plan["candidate_sha256"], source_commit=plan["source_commit"],
        source_note="Serving contracts updated; numerical/model sources unchanged. "
                    "Runtime manifest and actual build cache preserved.",
        tested_source_manifest=str(PACKAGE / "candidate/runtime-source.json"),
        tested_source_manifest_sha256=h.sha(PACKAGE / "candidate/runtime-source.json"),
        build_cache=str(PACKAGE / "candidate/CMakeCache.txt"),
        build_cache_sha256=h.sha(PACKAGE / "candidate/CMakeCache.txt"),
        acceptance="ACCEPT_BOUNDED_APPLICATION_SERVICE",
        acceptance_decision=str(result), acceptance_decision_sha256=h.sha(result),
        previous_binary=str(PACKAGE / "previous/q4t"),
        previous_release=str(PACKAGE / "previous/q4t.release.json"),
        tool_source_commit=plan["source_commit"],
        tool_acceptance=str(result), tool_acceptance_sha256=h.sha(result))
    # Old proofs remain under an explicit inherited boundary, never relabelled
    # as acceptance of the new candidate's application code.
    inherited = {}
    for key in ("acceptance_snapshot", "acceptance_snapshot_sha256",
                "acceptance_audit", "acceptance_audit_sha256"):
        if key in metadata:
            inherited[key] = metadata.pop(key)
    metadata["inherited_acceptance"] = inherited
    metadata["chat_protocol"].update(
        protocol_sha256=caps["protocol_source_sha256"],
        messages_parse_response_default=caps["chat"]["messages_parse_response_default"],
        sdk_interoperability_acceptance={"python": plan["sdk_version"],
            "scope": "models, Chat JSON/SSE, function loop, validation errors"},
        token_limit_fields=caps["chat"]["token_limit_fields"], errors=caps["errors"])
    metadata["implementation_capabilities"] = caps
    directory = OUTPUT / "release"
    directory.mkdir()
    h.save(directory / "q4t.release.json", metadata)
    h.save(directory / "binding.json", dict(old=plan["old_hashes"], new={
        "q4t": plan["candidate_sha256"],
        "q4t.release.json": h.sha(directory / "q4t.release.json")}))
    h.seal(directory)
    return directory


def restore(output):
    h.require_guard(output)
    plan = check()
    h.verify_seal(OUTPUT / "release")
    binding = h.read(OUTPUT / "release/binding.json")
    if MARKER.exists():
        h.require(h.read(MARKER).get("stage") == str(PACKAGE), "foreign marker")
    else:
        h.save(MARKER, dict(stage=str(PACKAGE), action="rollback"))
        h.fsync_directory(MARKER.parent)
    # The two new files live in separate immutable locations. Stage copies
    # into a private pair directory once, and never accept changed copies.
    restore_pair(ROOT / "build", PACKAGE / "previous", OUTPUT / "pair",
                 binding["old"], binding["new"])
    h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json",
                  plan["previous_binary_sha256"])
    MARKER.unlink()
    h.fsync_directory(MARKER.parent)


def execute():
    h.require_guard(OUTPUT)
    plan = check()
    verify_hashes(ROOT / "build", plan["old_hashes"])
    h.require(not list((ROOT / "build").glob("*recovery*.json")), "recovery pending")
    h.require(not (OUTPUT / "execution.json").exists(), "no automatic retry")
    acceptance.install_interrupt_handlers()
    start = plan.get("original_started", time.time())
    state = dict(status="RUNNING", started=time.time(), original_started=start)
    installed = False
    try:
        verifier(plan)
        check()
        h.require(time.time() < start + 1500, "installation time reserve")
        directory = release(plan)
        pair = OUTPUT / "pair"
        pair.mkdir()
        h.copy_checked(h.identity(plan["candidate"]), pair / "q4t")
        h.copy_checked(h.identity(directory / "q4t.release.json"),
                       pair / "q4t.release.json")
        binding = h.read(directory / "binding.json")
        states = state_pair(ROOT / "build", PACKAGE / "previous", pair,
                            binding["old"], binding["new"])
        h.require(states == binding["old"], "default changed")
        h.save(MARKER, dict(stage=str(PACKAGE), action="install"))
        h.fsync_directory(MARKER.parent)
        installed = True  # Includes failure between the two replacements.
        for name in NAMES:
            h.replace_checked(pair / name, ROOT / "build" / name, states[name])
        h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json",
                      plan["candidate_sha256"])
        verifier(plan, installed=True)
        final = nvml_process_query.query(timeout=5)
        h.save(OUTPUT / "final-gpu.json", final)
        h.require(final["status"] == "OK" and not final["pids"], "GPU not released")
        h.require(time.time() < start + 1800, "overall deadline")
        state["status"] = "HEALTH_CHECKED_PENDING_GUARD"
    except BaseException as error:
        state.update(status="FAILED", error=repr(error))
        if installed:
            try:
                restore(OUTPUT)
                state["rollback"] = "RESTORED"
            except BaseException as recovery_error:
                state["rollback_error"] = repr(recovery_error)
        raise
    finally:
        state["finished"] = time.time()
        h.save(OUTPUT / "execution.json", state)


def finalize():
    plan = check()
    h.require(h.read(OUTPUT / "lease/exit.json")["status"] == "COMPLETED" and
        h.read(OUTPUT / "execution.json")["status"] ==
        "HEALTH_CHECKED_PENDING_GUARD", "guard/health did not pass")
    h.verify_seal(OUTPUT / "release")
    binding = h.read(OUTPUT / "release/binding.json")
    verify_hashes(ROOT / "build", binding["new"])
    h.require(h.read(MARKER).get("stage") == str(PACKAGE), "foreign marker")
    h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json",
                  plan["candidate_sha256"])
    h.save(OUTPUT / "finalized.json", dict(status="DELIVERED", time=time.time(),
        installed=binding["new"], source_commit=plan["source_commit"]))
    MARKER.unlink()
    h.fsync_directory(MARKER.parent)
    print("DELIVERED", binding["new"], flush=True)


def main():
    global BASE, PACKAGE, OUTPUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "finalize", "rollback"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--correction", action="store_true")
    args = parser.parse_args()
    if args.correction:
        BASE = ROOT / ".q4t-work/application-service-correction-20261102"
        PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
    if args.action == "rollback":
        h.require(args.output is not None, "rollback needs a fresh lease output")
        restore(args.output.resolve())
    else:
        globals()[args.action]()


if __name__ == "__main__":
    main()
