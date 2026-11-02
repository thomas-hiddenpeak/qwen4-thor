"""Freeze/deliver SRV-01b under one lease and a bounded original deadline."""
import argparse
import copy
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_observability import cpu
from tools.serve_admission import stage as previous

h, a = previous.h, previous.a
BASE, PACKAGE, OUTPUT = cpu.BASE, cpu.BASE / "package", cpu.BASE / "execution"
DEFAULT_SHA = "c16caf0573ed2ecdf23237229a54a23a0d4ead46e86f6b25e7a44436d3caafe0"
SIDECAR_SHA = "be4cd02115f97a232aa885b487fa5e42bf044e9ccfa1bca8be31701a0c8d4463"
LIMITS = dict(generations=10, negatives=20, starts=3, seconds=1800)
PLANNED = dict(generations=8, negatives=16, starts=2)


def build_gate():
    current = cpu.sources()
    h.require(current == h.read(BASE / "cpu/build-sources.json") ==
              h.read(BASE / "fault-build/sources.json"), "runtime changed since builds")
    normal, fault = (previous.cache(BASE / name / "CMakeCache.txt")
                     for name in ("engine", "fault-engine"))
    keys = {k for k in normal if k.startswith("Q4T_")} | {
        "CMAKE_BUILD_TYPE", "CMAKE_CXX_COMPILER", "CMAKE_CUDA_COMPILER",
        "CMAKE_CUDA_HOST_COMPILER", "CMAKE_CXX_FLAGS", "CMAKE_CUDA_FLAGS",
        "CMAKE_CXX_FLAGS_RELEASE", "CMAKE_CUDA_FLAGS_RELEASE",
        "CMAKE_EXE_LINKER_FLAGS", "FETCHCONTENT_SOURCE_DIR_Q4T_XGRAMMAR"}
    h.require(normal["Q4T_SERVE_FAULT_INJECTION"] == "OFF" and
              fault["Q4T_SERVE_FAULT_INJECTION"] == "ON", "fault/production identity")
    h.require(all(normal[k] == fault[k] for k in keys-{"Q4T_SERVE_FAULT_INJECTION"}),
              "build flags differ beyond validation macro")
    for name, record, expected in (("engine", "cpu/build-result.json", False),
                                    ("fault-engine", "fault-build/result.json", True)):
        result = h.read(BASE / record)
        h.require(result["passed"] and result["zero_warnings"] and
                  result["binary_sha256"] == h.sha(BASE / name / "q4t"), "build gate")
        commands = h.read(BASE / name / "compile_commands.json")
        server = [r for r in commands if r["output"].startswith("CMakeFiles/q4t_server.dir/")]
        h.require(server and all(("-DQ4T_SERVE_FAULT_INJECTION=1" in r["command"]) is expected
                                for r in server), "target macro mismatch")
    for name in ("chat_protocol_identity.h", "chat_protocol_sources.json"):
        relative = Path("generated/q4t/server") / name
        h.require(h.sha(BASE / "engine" / relative) == h.sha(BASE / "fault-engine" / relative),
                  "fault/production protocol identity differs")
    a.verify_hashes(ROOT, h.read(BASE / "engine/generated/q4t/server/chat_protocol_sources.json"))
    return current


def cpu_gate():
    folder = BASE / "cpu"
    for name, count in (("host-off", 214), ("host-on", 223),
                        ("native-observation", 2), ("backend-gate", 9)):
        h.require(f"{count} tests, {count} passed, 0 failed, 0 skipped" in
                  (folder / (name+".log")).read_text(), "CPU gate: " + name)
    # Original full run passed 180; merged stderr split one PASS identity.
    original = h.read(folder / "host-gate/result.json")
    corrected = h.read(folder / "host-gate-corrected/result.json")
    h.require(original["exit"] == 0 and not original["passed"] and
              corrected["passed"] and len(corrected["required_tests"]) == 180 and
              original["binary_sha256"] == corrected["binary_sha256"] ==
              h.sha(BASE / "engine/q4t_host_tests"), "corrected prerequisite gate")
    for name in ("driver-result.json", "driver-failure-capture-result.json", "planned-native/result.json"):
        h.require(h.read(folder / name)["passed"], "driver/input CPU gate")
    subprocess.run(["/usr/bin/python3", "-B", str(ROOT / "tools/serve_protocol/build_contract.py"),
                    "--check"], cwd=ROOT, timeout=30, check=True)
    subprocess.run(["/usr/bin/python3", "-B", str(ROOT / "tools/serve_audit/check_inventory.py"),
                    "--self-check"], cwd=ROOT, timeout=30, check=True)
    build_gate()


def prepare():
    h.require(not PACKAGE.exists(), "preserve frozen package")
    h.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
              "commit reviewed stage before freezing")
    old = h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json", DEFAULT_SHA)
    h.require(h.sha(ROOT / "build/q4t.release.json") == SIDECAR_SHA, "default sidecar changed")
    h.require(not list((ROOT / "build").glob("*recovery*.json")), "recovery pending")
    cpu_gate()
    inherited = h.read(previous.PACKAGE / "plan.json")
    a.verify_hashes(inherited["model_dir"], inherited["model_inputs"])
    sdk = previous.prior.sdk_identity()
    PACKAGE.mkdir()
    for name in ("cpu", "fault-build"):
        shutil.copytree(BASE / name, PACKAGE / name)
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
    current = cpu.sources()
    h.save(PACKAGE / "candidate/runtime-source.json", current)
    old_sources = h.read(old["tested_source_manifest"])
    changed = [p for p in old_sources if current[p] != old_sources[p]]
    h.require(all(p.startswith(("src/server/", "include/q4t/server/")) or p in (
        "CMakeLists.txt", "cmake/ChatProtocolIdentity.cmake", "cmake/HostTestSources.cmake")
        for p in changed), "numerical source changed")
    h.save(PACKAGE / "numerical-reuse.json", dict(changed=changed,
        added=sorted(current.keys()-old_sources.keys()),
        previous_manifest=h.identity(old["tested_source_manifest"]),
        unchanged_model_and_numerical_sources=True, performance_acceptance=False,
        text_contract_reuse="Input validation, template/tool history, tokenizer, grammar and model math unchanged. Transport observations retested; protocol hash changes intentionally."))
    paths = set(a.acceptance.source_paths())
    paths.update(str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("serve-*.json"))
    paths.update(("docs/SERVE_OBSERVABILITY_2026-11-03.md", "docs/MODEL.md"))
    h.save(PACKAGE / "sources.json", {p:h.sha(ROOT / p) for p in sorted(paths)})
    h.save(PACKAGE / "sdk.json", sdk)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "7f50d6f4..HEAD"], cwd=ROOT, timeout=60, check=True)
    protocol = h.read(PACKAGE / "candidate/generated/q4t/server/chat_protocol_sources.json")
    plan = dict(schema_version=1, root=str(ROOT), limits=LIMITS, planned=PLANNED,
        source_commit=commit, candidate=str(PACKAGE / "candidate/q4t"),
        candidate_sha256=h.sha(PACKAGE / "candidate/q4t"),
        binary=h.identity(PACKAGE / "candidate/q4t"), fault_binary=h.identity(PACKAGE / "fault/q4t"),
        old_hashes={n:h.sha(PACKAGE / "previous" / n) for n in a.NAMES},
        previous_binary_sha256=DEFAULT_SHA, source_manifest_sha256=h.sha(PACKAGE / "sources.json"),
        protocol_sha256=hashlib.sha256("".join(f"{p}:{protocol[p]}\n" for p in sorted(protocol)).encode()).hexdigest(),
        model_dir=inherited["model_dir"], model_inputs=inherited["model_inputs"],
        sdk_directory=sdk["sdk_directory"], port=18103, performance_acceptance=False,
        lifecycle_probe=h.identity(PACKAGE / "candidate/q4t_http_lifecycle_tests"))
    h.save(PACKAGE / "plan.json", plan)
    h.seal(PACKAGE)
    print("PREPARED", plan["candidate_sha256"], flush=True)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE / "plan.json")
    h.require(plan["root"] == str(ROOT) and plan["limits"] == LIMITS and plan["planned"] == PLANNED,
              "frozen scope/budget changed")
    h.require(plan["source_manifest_sha256"] == h.sha(PACKAGE / "sources.json"), "source manifest changed")
    a.verify_hashes(ROOT, h.read(PACKAGE / "sources.json"))
    a.verify_hashes(plan["model_dir"], plan["model_inputs"])
    for key in ("binary", "fault_binary", "lifecycle_probe"):
        h.check_identity(plan[key])
    h.require(previous.prior.sdk_identity() == h.read(PACKAGE / "sdk.json"), "pinned SDK changed")
    if (OUTPUT / "begin.json").exists():
        plan["started"] = plan["original_started"] = h.read(OUTPUT / "begin.json")["started"]
    return plan


def verifier(plan, installed=False):
    if installed:
        from tools.serve_observability.installed_health import run
        run(plan, OUTPUT)
        return
    with (OUTPUT / "http-lifecycle.log").open("xb") as log:
        result = subprocess.run([plan["lifecycle_probe"]["path"]], stdout=log, stderr=log,
                                cwd=ROOT, timeout=60)
    h.require(result.returncode == 0 and "16 tests, 16 passed, 0 failed, 0 skipped" in
              (OUTPUT / "http-lifecycle.log").read_text(), "HTTP lifecycle failed")
    from tools.serve_observability.verify import Run
    Run(plan, OUTPUT / "business").run()


def release(plan):
    result = OUTPUT / "business/result.json"
    h.require(h.read(result)["status"] == "PASS_BOUNDED_OBSERVABILITY", "business gate")
    caps = json.loads(h.read(OUTPUT / "business/production-mtp/capabilities.json")["raw"])
    metadata = h.read(PACKAGE / "previous/q4t.release.json")
    inherited = copy.deepcopy({k:metadata[k] for k in ("acceptance", "acceptance_decision",
        "acceptance_decision_sha256", "chat_protocol", "deployment_completion", "scope") if k in metadata})
    inherited["release"] = h.identity(PACKAGE / "previous/q4t.release.json")
    metadata.pop("deployment_completion", None)
    metadata.update(version="serve-observability-20261103", binary_sha256=plan["candidate_sha256"],
        source_commit=plan["source_commit"], tool_source_commit=plan["source_commit"],
        source_note="First fixed receive/write failure observations and body-capacity error. No numerical/performance acceptance claim.",
        tested_source_manifest=str(PACKAGE / "candidate/runtime-source.json"),
        tested_source_manifest_sha256=h.sha(PACKAGE / "candidate/runtime-source.json"),
        build_cache=str(PACKAGE / "candidate/CMakeCache.txt"),
        build_cache_sha256=h.sha(PACKAGE / "candidate/CMakeCache.txt"),
        acceptance="ACCEPT_BOUNDED_OBSERVABILITY", acceptance_decision=str(result),
        acceptance_decision_sha256=h.sha(result), tool_acceptance=str(result), tool_acceptance_sha256=h.sha(result),
        previous_binary=str(PACKAGE / "previous/q4t"), previous_release=str(PACKAGE / "previous/q4t.release.json"),
        inherited_acceptance=inherited, implementation_capabilities=caps,
        scope=dict(hardware="local Thor", heads=["full"], production="MTP k3, single8192/inflight8/queued2",
            validation="Same-source fault plain; ordinary incremental TCP EAGAIN cancel/write deadline; no strict long-payload acceptance",
            observations="first receive/write per connection; heartbeat joined before publication; local progress is not peer receipt",
            installation_health="CLI/worker plus same-ELF candidate HTTP reuse; no installed model start",
            performance_acceptance=False, numerical_evidence=str(PACKAGE / "numerical-reuse.json")))
    metadata["chat_protocol"].update(protocol_sha256=plan["protocol_sha256"],
        template=caps["template"], observability=caps["observability"],
        sdk_interoperability_acceptance=dict(node="24.18.0", openai_javascript="7.47.0",
            scope="Strict real project read plus final answer over SSE; no automatic retry"))
    directory = OUTPUT / "release"
    directory.mkdir()
    h.save(directory / "q4t.release.json", metadata)
    h.save(directory / "binding.json", dict(old=plan["old_hashes"], new=dict(q4t=plan["candidate_sha256"],
        **{"q4t.release.json":h.sha(directory / "q4t.release.json")})))
    h.seal(directory)
    return directory


def bind():
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT / "build/serve-observability-recovery.json"
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
    h.require(time.time()-h.read(OUTPUT / "begin.json")["started"] <= LIMITS["seconds"],
              "original finalization deadline")
    bind()
    a.finalize()
    h.seal(OUTPUT)


def rollback_plan():
    # Current development does not invalidate a known sealed rollback pair.
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE / "plan.json")
    h.require(plan["root"] == str(ROOT) and plan["limits"] == LIMITS and
              plan["previous_binary_sha256"] == DEFAULT_SHA and
              plan["old_hashes"]["q4t"] == DEFAULT_SHA, "foreign rollback package")
    h.check_identity(plan["binary"])
    h.verify_pair(PACKAGE / "previous/q4t", PACKAGE / "previous/q4t.release.json",
                  DEFAULT_SHA, PACKAGE / "previous/CMakeCache.txt")
    return plan


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
        a.check = rollback_plan
        a.restore(args.output.resolve())
        return
    globals()[args.action]()


if __name__ == "__main__":
    main()
