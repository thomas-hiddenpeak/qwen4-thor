"""Frozen SRV-02b reliability delivery; one lease and bounded original budget."""
import argparse
import copy
import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_protocol import stage as prior

h, a = prior.h, prior.a
BASE = ROOT / ".q4t-work/serve-admission-20261103"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
LIMITS = dict(generations=12, negatives=8, starts=3, seconds=1800)
PLANNED = dict(generations=10, negatives=4, starts=2)


def cache(path):
    return {match[1]: match[2] for line in Path(path).read_text().splitlines()
            if (match := re.match(r"([^/#][^:]*):[^=]+=(.*)$", line))}


def runtime_sources():
    old = h.read(ROOT / "build/q4t.release.json")
    paths = set(h.read(old["tested_source_manifest"])) | {
        "include/q4t/server/request_admission.h", "include/q4t/server/sequence_admission.h",
        "src/server/chat_admission.cpp"}
    return {p: h.sha(ROOT / p) for p in sorted(paths)}


def build_gate():
    snapshot = h.read(BASE / "cpu/build-sources.json")
    # Snapshot format is a direct map from repository path to digest.
    current = runtime_sources()
    h.require(all(snapshot.get(p) == digest for p, digest in current.items()),
              "runtime changed since production/fault builds")
    old, fault = (cache(BASE / directory / "CMakeCache.txt")
                  for directory in ("engine", "fault-engine"))
    keys = {k for k in old if k.startswith("Q4T_")} | {
        "CMAKE_BUILD_TYPE", "CMAKE_CXX_COMPILER", "CMAKE_CUDA_COMPILER",
        "CMAKE_CUDA_HOST_COMPILER", "CMAKE_CXX_FLAGS", "CMAKE_CUDA_FLAGS",
        "CMAKE_CXX_FLAGS_RELEASE", "CMAKE_CUDA_FLAGS_RELEASE",
        "CMAKE_EXE_LINKER_FLAGS", "FETCHCONTENT_SOURCE_DIR_Q4T_XGRAMMAR"}
    h.require(old["Q4T_SERVE_FAULT_INJECTION"] == "OFF" and
              fault["Q4T_SERVE_FAULT_INJECTION"] == "ON", "fault/production identity")
    h.require(all(old[k] == fault[k] for k in keys - {"Q4T_SERVE_FAULT_INJECTION"}),
              "build flags differ beyond validation macro")
    for folder, name in (("cpu", "engine"), ("fault-build", "fault-engine")):
        result = h.read(BASE / folder / ("build-result.json" if folder == "cpu" else "result.json"))
        h.require(result["passed"] and result["zero_warnings"] and
                  result["binary_sha256"] == h.sha(BASE / name / "q4t"), "binary build gate")
    for engine, expected in (("engine", False), ("fault-engine", True)):
        commands = h.read(BASE / engine / "compile_commands.json")
        server = [row for row in commands if row["output"].startswith("CMakeFiles/q4t_server.dir/")]
        h.require(server and all(("-DQ4T_SERVE_FAULT_INJECTION=1" in row["command"]) is expected
                                for row in server), "target compile macro mismatch")
    for name in ("chat_protocol_identity.h", "chat_protocol_sources.json"):
        relative = Path("generated/q4t/server") / name
        h.require(h.sha(BASE / "engine" / relative) == h.sha(BASE / "fault-engine" / relative),
                  "fault/production protocol identity differs")
    a.verify_hashes(ROOT, h.read(BASE / "engine/generated/q4t/server/chat_protocol_sources.json"))
    return current


def cpu_gate():
    cpu = BASE / "cpu"
    for mode, count in (("off", 206), ("on", 215)):
        h.require(f"{count} tests, {count} passed, 0 failed, 0 skipped" in
                  (cpu / f"host-{mode}.log").read_text(), "host mode gate")
    required = h.read(cpu / "host-gate/result.json")
    h.require(required["passed"] and len(required["required_tests"]) == 172 and
              required["binary_sha256"] == h.sha(BASE / "engine/q4t_host_tests"), "required host gate")
    for name, count in (("native-admission", 6), ("backend-gate", 9)):
        h.require(f"{count} tests, {count} passed, 0 failed, 0 skipped" in
                  (cpu / (name+".log")).read_text(), "native contract gate")
    original = h.read(cpu / "config-probe/result.json")
    corrected = h.read(cpu / "config-corrected/result.json")
    h.require(original["fixtures"] == 15 and len(original["failures"]) == 5 and
              corrected["passed"] and corrected["fixtures"] == 5 and corrected["reused"] == 10 and
              original["binary_sha256"] == corrected["binary_sha256"], "native configuration gate")
    original_driver = h.read(cpu / "driver-result.json")
    h.require(not original_driver["passed"] and
              "FAILED (errors=1)" in (cpu / "driver-tests.log").read_text() and
              not h.read(cpu / "driver-corrected-result.json")["passed"] and
              h.read(cpu / "driver-final-result.json")["passed"],
              "driver CPU correction gate; ten unchanged checks reused")
    h.require(h.read(cpu / "planned-native/result.json")["passed"],
              "planned HTTP input did not pass the production native contract")
    h.require(h.read(cpu / "driver-wire-result.json")["passed"], "SSE role/error contract gate")
    subprocess.run(["/usr/bin/python3", "-B", str(ROOT / "tools/serve_protocol/build_contract.py"),
                    "--check"], cwd=ROOT, timeout=30, check=True)
    build_gate()


def prepare():
    h.require(not PACKAGE.exists(), "preserve frozen package")
    h.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
              "commit reviewed stage before freezing")
    old = h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json", prior.DEFAULT_SHA)
    h.require(h.sha(ROOT / "build/q4t.release.json") == prior.SIDECAR_SHA, "default sidecar changed")
    h.require(not list((ROOT / "build").glob("*recovery*.json")), "recovery pending")
    cpu_gate()
    sdk = prior.sdk_identity()
    inherited_plan = h.read(prior.PACKAGE / "plan.json")
    a.verify_hashes(inherited_plan["model_dir"], inherited_plan["model_inputs"])
    PACKAGE.mkdir()
    shutil.copytree(BASE / "cpu", PACKAGE / "cpu")
    shutil.copytree(BASE / "fault-build", PACKAGE / "fault-build")
    for name in ("candidate", "fault", "previous"):
        (PACKAGE / name).mkdir()
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT / "build" / name), PACKAGE / "previous" / name)
    h.copy_checked(h.identity(old["build_cache"]), PACKAGE / "previous/CMakeCache.txt")
    for target, engine in (("candidate", "engine"), ("fault", "fault-engine")):
        for name in ("q4t", "CMakeCache.txt", "compile_commands.json"):
            h.copy_checked(h.identity(BASE / engine / name), PACKAGE / target / name)
        shutil.copytree(BASE / engine / "generated", PACKAGE / target / "generated")
    h.copy_checked(h.identity(BASE / "engine/q4t_http_lifecycle_tests"),
                   PACKAGE / "candidate/q4t_http_lifecycle_tests")
    current = runtime_sources()
    h.save(PACKAGE / "candidate/runtime-source.json", current)
    old_sources = h.read(old["tested_source_manifest"])
    changed = [p for p in old_sources if current[p] != old_sources[p]]
    h.require(all(p.startswith(("src/server/", "include/q4t/server/")) or
                  p in ("src/main.cpp", "CMakeLists.txt", "cmake/ChatProtocolIdentity.cmake",
                        "cmake/HostTestSources.cmake") for p in changed), "numerical source changed")
    h.save(PACKAGE / "numerical-reuse.json", dict(changed=changed,
        added=sorted(current.keys()-old_sources.keys()),
        previous_manifest=h.identity(old["tested_source_manifest"]),
        unchanged_model_and_numerical_sources=True, performance_acceptance=False,
        text_contract_reuse="Input validation, renderer/tool history and tokenizer unchanged. Configuration formatting and admission semantics are retested; protocol hash changes intentionally."))
    paths = set(a.acceptance.source_paths())
    paths.update(str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("serve-*.json"))
    paths.update(("docs/SERVE_ADMISSION_2026-11-03.md", "docs/MODEL.md"))
    h.save(PACKAGE / "sources.json", {p: h.sha(ROOT / p) for p in sorted(paths)})
    h.save(PACKAGE / "sdk.json", sdk)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "6ee442ed..HEAD"], cwd=ROOT, check=True, timeout=60)
    protocol = h.read(PACKAGE / "candidate/generated/q4t/server/chat_protocol_sources.json")
    plan = dict(schema_version=1, root=str(ROOT), limits=LIMITS, planned=PLANNED,
        source_commit=commit, candidate=str(PACKAGE / "candidate/q4t"),
        candidate_sha256=h.sha(PACKAGE / "candidate/q4t"),
        binary=h.identity(PACKAGE / "candidate/q4t"), fault_binary=h.identity(PACKAGE / "fault/q4t"),
        old_hashes={n: h.sha(PACKAGE / "previous" / n) for n in a.NAMES},
        previous_binary_sha256=prior.DEFAULT_SHA,
        source_manifest_sha256=h.sha(PACKAGE / "sources.json"),
        protocol_sha256=hashlib.sha256("".join(f"{p}:{protocol[p]}\n" for p in sorted(protocol)).encode()).hexdigest(),
        model_dir=inherited_plan["model_dir"], model_inputs=inherited_plan["model_inputs"],
        sdk_directory=sdk["sdk_directory"], port=18101, performance_acceptance=False,
        lifecycle_probe=h.identity(PACKAGE / "candidate/q4t_http_lifecycle_tests"))
    h.save(PACKAGE / "plan.json", plan)
    h.seal(PACKAGE)
    print("PREPARED", plan["candidate_sha256"], flush=True)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE / "plan.json")
    h.require(plan["limits"] == LIMITS and plan["planned"] == PLANNED and plan["root"] == str(ROOT),
              "frozen budget/scope changed")
    h.require(plan["source_manifest_sha256"] == h.sha(PACKAGE / "sources.json"), "source manifest changed")
    a.verify_hashes(ROOT, h.read(PACKAGE / "sources.json"))
    a.verify_hashes(plan["model_dir"], plan["model_inputs"])
    for key in ("binary", "fault_binary", "lifecycle_probe"):
        h.check_identity(plan[key])
    h.require(prior.sdk_identity() == h.read(PACKAGE / "sdk.json"), "pinned SDK changed")
    if (OUTPUT / "begin.json").exists():
        plan["started"] = plan["original_started"] = h.read(OUTPUT / "begin.json")["started"]
    return plan


def verifier(plan, installed=False):
    if installed:
        from tools.serve_admission.installed_health import run
        run(plan, OUTPUT)
        return
    # These tests include actual local HTTP sockets, hence run under this lease.
    probe = plan["lifecycle_probe"]["path"]
    with (OUTPUT / "http-lifecycle.log").open("xb") as log:
        result = subprocess.run([probe], stdout=log, stderr=log, cwd=ROOT, timeout=60)
    h.require(result.returncode == 0 and "13 tests, 13 passed, 0 failed, 0 skipped" in
              (OUTPUT / "http-lifecycle.log").read_text(), "HTTP lifecycle failed")
    from tools.serve_admission.verify import Run
    Run(plan, OUTPUT / "business").run()


def release(plan):
    result = OUTPUT / "business/result.json"
    h.require(h.read(result)["status"] == "PASS_BOUNDED_ADMISSION", "business gate")
    caps = json.loads(h.read(OUTPUT / "business/production-mtp/capabilities.json")["raw"])
    metadata = h.read(PACKAGE / "previous/q4t.release.json")
    inherited = copy.deepcopy({k: metadata[k] for k in ("acceptance", "acceptance_decision",
        "acceptance_decision_sha256", "chat_protocol", "deployment_completion", "scope") if k in metadata})
    inherited["release"] = h.identity(PACKAGE / "previous/q4t.release.json")
    metadata.pop("deployment_completion", None)
    metadata.update(version="serve-admission-20261103", binary_sha256=plan["candidate_sha256"],
        source_commit=plan["source_commit"], tool_source_commit=plan["source_commit"],
        source_note="Configurable shared chat/preflight inflight bound and capacity FIFO slot queue. No numerical or performance acceptance claim.",
        tested_source_manifest=str(PACKAGE / "candidate/runtime-source.json"),
        tested_source_manifest_sha256=h.sha(PACKAGE / "candidate/runtime-source.json"),
        build_cache=str(PACKAGE / "candidate/CMakeCache.txt"),
        build_cache_sha256=h.sha(PACKAGE / "candidate/CMakeCache.txt"),
        acceptance="ACCEPT_BOUNDED_ADMISSION", acceptance_decision=str(result),
        acceptance_decision_sha256=h.sha(result), tool_acceptance=str(result), tool_acceptance_sha256=h.sha(result),
        previous_binary=str(PACKAGE / "previous/q4t"), previous_release=str(PACKAGE / "previous/q4t.release.json"),
        inherited_acceptance=inherited, implementation_capabilities=caps,
        scope=dict(hardware="local Thor", heads=["full"], production="MTP k3, one uniform slot 8192, inflight1/queued0",
            validation="Same-source fault binary, plain slots8192/2048 inflight8/queued2, bounded response pause",
            admission="capacity_fifo_best_fit after preparation; no tenant/completion-time guarantee",
            installation_health="CLI/worker plus same-ELF candidate HTTP reuse, no additional installed model start",
            strict_backpressure="SRV-03b remains unproven", performance_acceptance=False,
            numerical_evidence=str(PACKAGE / "numerical-reuse.json")))
    metadata["chat_protocol"].update(protocol_sha256=plan["protocol_sha256"], template=caps["template"],
        config_schema=str(ROOT / "docs/serve-config-v1.schema.json"),
        config_schema_sha256=h.sha(ROOT / "docs/serve-config-v1.schema.json"),
        admission=dict(config_endpoint="/v1/config", queue_policy="capacity_fifo_best_fit",
            queue_order="slot_admission_arrival", inflight_error="capacity_exhausted",
            queue_error="queue_capacity_exhausted", whole_server_ram_limit=False),
        sdk_interoperability_acceptance=dict(node="24.18.0", openai_javascript="7.47.0",
            scope="Strict real project read plus final answer over SSE; no automatic retry",
            previous_client_evidence=str(prior.OUTPUT / "finalized.json")))
    directory = OUTPUT / "release"
    directory.mkdir()
    h.save(directory / "q4t.release.json", metadata)
    h.save(directory / "binding.json", dict(old=plan["old_hashes"], new=dict(
        q4t=plan["candidate_sha256"], **{"q4t.release.json": h.sha(directory / "q4t.release.json")})))
    h.seal(directory)
    return directory


def bind():
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT / "build/serve-admission-recovery.json"
    a.check, a.verifier, a.release = check, verifier, release


def guard_command():
    return ["/usr/bin/python3", "-B", str(ROOT / "tools/evalscope/gpu_exclusive.py"),
            "--output", str(OUTPUT / "lease"), "--", "/usr/bin/python3", "-B",
            str(Path(__file__).resolve()), "execute"]


def execute():
    h.require_guard(OUTPUT)
    events = [json.loads(line) for line in (OUTPUT / "lease/events.jsonl").read_text().splitlines()]
    started = datetime.datetime.fromisoformat(next(row["utc"] for row in events
        if row["event"] == "lease_acquired")).timestamp()
    h.save(OUTPUT / "begin.json", dict(started=started, limits=LIMITS))
    bind()
    a.execute()


def finalize():
    check()
    h.require(time.time() - h.read(OUTPUT / "begin.json")["started"] <= LIMITS["seconds"],
              "original finalization deadline")
    bind()
    a.finalize()
    h.seal(OUTPUT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "launch", "execute", "finalize", "rollback"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "launch":
        check()
        OUTPUT.mkdir(exist_ok=False)
        raise SystemExit(subprocess.call(guard_command(), cwd=ROOT))
    if args.action == "rollback":
        h.require(args.output is not None, "fresh lease output required")
        bind()
        a.restore(args.output.resolve())
        return
    globals()[args.action]()


if __name__ == "__main__":
    main()
