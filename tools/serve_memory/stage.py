"""Freeze/deliver bounded memory admission; one lease, no memory exhaustion."""
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
from tools.serve_memory import cpu
from tools.serve_admission import stage as previous

h, a = previous.h, previous.a
BASE, PACKAGE, OUTPUT = cpu.BASE, cpu.BASE / "package", cpu.BASE / "execution"
DEFAULT_SHA = "ed672a65b586f8445e51e3ba6d63659b4a098386e87e1ed38e3c8893a352a139"
SIDECAR_SHA = "67897c817dfccd6db3d4e2c0d0af3cfc70fb97a2c840887905c03fbf0bd6be23"
LIMITS = dict(generations=6, negatives=6, starts=3, seconds=1800)
PLANNED = dict(generations=5, negatives=4, starts=2)


def build_gate():
    current = cpu.sources()
    h.require(current == h.read(BASE / "cpu/build-sources.json"), "runtime changed")
    result = h.read(BASE / "cpu/build-result.json")
    h.require(result["passed"] and result["zero_warnings"] and
              result["binary_sha256"] == h.sha(BASE / "engine/q4t"), "build gate")
    cache = previous.cache(BASE / "engine/CMakeCache.txt")
    h.require(cache["Q4T_SERVE_FAULT_INJECTION"] == "OFF", "production only")
    commands = h.read(BASE / "engine/compile_commands.json")
    server = [r for r in commands if r["output"].startswith("CMakeFiles/q4t_server.dir/")]
    h.require(server and all("-DQ4T_SERVE_FAULT_INJECTION=1" not in r["command"]
                            for r in server), "production has fault macro")
    a.verify_hashes(ROOT, h.read(BASE / "engine/generated/q4t/server/chat_protocol_sources.json"))
    return current


def cpu_gate():
    folder = BASE / "cpu"
    for label, count in (("host-off", 224), ("host-on", 233), ("backend", 9)):
        h.require(f"{count} tests, {count} passed, 0 failed, 0 skipped" in
                  (folder / (label+".log")).read_text(), "CPU gate: " + label)
    required = h.read(folder / "host-gate/result.json")
    h.require(required["passed"] and len(required["required_tests"]) == 190 and
              required["binary_sha256"] == h.sha(BASE / "engine/q4t_host_tests"),
              "required host gate")
    for name in ("config-schema/result.json", "memory-config/result.json", "driver-result.json"):
        h.require(h.read(folder / name)["passed"], "CPU configuration/driver gate")
    native = h.read(folder / "native-receipt.json")
    guard = h.read(BASE / "native-lease/exit.json")
    h.require(native["passed"] and native["observed_tests"] == 19 and
              native["binary_sha256"] == h.sha(BASE / "engine/q4t_http_lifecycle_tests") and
              guard["status"] == "COMPLETED" and guard["child_returncode"] == 0,
              "native HTTP/queue evidence")
    # The original inventory drift rejection is retained; only reviewed metadata changed.
    h.require(not h.read(folder / "test-result.json")["commands"]["inventory"],
              "original inventory failure missing")
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
    for name in ("cpu", "native-lease"):
        shutil.copytree(BASE / name, PACKAGE / name)
    for name in ("candidate", "previous"):
        (PACKAGE / name).mkdir()
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT / "build" / name), PACKAGE / "previous" / name)
    h.copy_checked(h.identity(old["build_cache"]), PACKAGE / "previous/CMakeCache.txt")
    for target, engine in (("candidate", "engine"),):
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
        "CMakeLists.txt", "cmake/ChatProtocolIdentity.cmake", "cmake/HostTestSources.cmake",
        "src/main.cpp", "include/q4t/runtime/memory_budget.h")
        for p in changed), "numerical source changed")
    h.save(PACKAGE / "numerical-reuse.json", dict(changed=changed,
        added=sorted(current.keys()-old_sources.keys()),
        previous_manifest=h.identity(old["tested_source_manifest"]),
        unchanged_model_and_numerical_sources=True, performance_acceptance=False,
        text_contract_reuse="Template/tool history, tokenizer, grammar and model math unchanged. Memory admission changes request acceptance and unknown MTP optional allocation fallback; protocol hash intentionally changes."))
    paths = set(a.acceptance.source_paths())
    paths.update(str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("serve-*.json"))
    paths.update(("docs/SERVE_MEMORY_ADMISSION_2026-11-03.md", "docs/MODEL.md"))
    h.save(PACKAGE / "sources.json", {p:h.sha(ROOT / p) for p in sorted(paths)})
    h.save(PACKAGE / "sdk.json", sdk)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "e67ad06e..HEAD"], cwd=ROOT, timeout=60, check=True)
    protocol = h.read(PACKAGE / "candidate/generated/q4t/server/chat_protocol_sources.json")
    plan = dict(schema_version=1, root=str(ROOT), limits=LIMITS, planned=PLANNED,
        source_commit=commit, candidate=str(PACKAGE / "candidate/q4t"),
        candidate_sha256=h.sha(PACKAGE / "candidate/q4t"),
        binary=h.identity(PACKAGE / "candidate/q4t"),
        old_hashes={n:h.sha(PACKAGE / "previous" / n) for n in a.NAMES},
        previous_binary_sha256=DEFAULT_SHA, source_manifest_sha256=h.sha(PACKAGE / "sources.json"),
        protocol_sha256=hashlib.sha256("".join(f"{p}:{protocol[p]}\n" for p in sorted(protocol)).encode()).hexdigest(),
        model_dir=inherited["model_dir"], model_inputs=inherited["model_inputs"],
        sdk_directory=sdk["sdk_directory"], port=18105, performance_acceptance=False,
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
    for key in ("binary", "lifecycle_probe"):
        h.check_identity(plan[key])
    h.require(previous.prior.sdk_identity() == h.read(PACKAGE / "sdk.json"), "pinned SDK changed")
    if (OUTPUT / "begin.json").exists():
        plan["started"] = plan["original_started"] = h.read(OUTPUT / "begin.json")["started"]
    return plan


def verifier(plan, installed=False):
    if installed:
        from tools.serve_memory.installed_health import run
        run(plan, OUTPUT)
        return
    # Reuse the identical native executable's 19 passing checks, no duplicate run.
    from tools.serve_memory.verify import Run
    Run(plan, OUTPUT / "business").run()


def release(plan):
    result = OUTPUT / "business/result.json"
    h.require(h.read(result)["status"] == "PASS_BOUNDED_MEMORY_ADMISSION", "business gate")
    caps = json.loads(h.read(OUTPUT / "business/production-mtp/capabilities.json")["raw"])
    metadata = h.read(PACKAGE / "previous/q4t.release.json")
    inherited = copy.deepcopy({k:metadata[k] for k in ("acceptance", "acceptance_decision",
        "acceptance_decision_sha256", "chat_protocol", "deployment_completion", "scope") if k in metadata})
    inherited["release"] = h.identity(PACKAGE / "previous/q4t.release.json")
    metadata.pop("deployment_completion", None)
    metadata.update(version="serve-memory-20261103", binary_sha256=plan["candidate_sha256"],
        source_commit=plan["source_commit"], tool_source_commit=plan["source_commit"],
        source_note="Early global memory admission, recovery hysteresis and unknown MTP observation declines optional allocation. No physical RAM hard-limit/performance acceptance claim.",
        tested_source_manifest=str(PACKAGE / "candidate/runtime-source.json"),
        tested_source_manifest_sha256=h.sha(PACKAGE / "candidate/runtime-source.json"),
        build_cache=str(PACKAGE / "candidate/CMakeCache.txt"),
        build_cache_sha256=h.sha(PACKAGE / "candidate/CMakeCache.txt"),
        acceptance="ACCEPT_BOUNDED_MEMORY_ADMISSION", acceptance_decision=str(result),
        acceptance_decision_sha256=h.sha(result), tool_acceptance=str(result), tool_acceptance_sha256=h.sha(result),
        previous_binary=str(PACKAGE / "previous/q4t"), previous_release=str(PACKAGE / "previous/q4t.release.json"),
        inherited_acceptance=inherited, implementation_capabilities=caps,
        scope=dict(hardware="local Thor", heads=["full"], production="MTP k3, single8192/inflight8/queued2",
            validation="Same production binary: high-floor early refusal, normal JSON/SSE/schema and real project tool. Native simulated observation proves dynamic recovery; no physical exhaustion or same-process model pressure recovery test",
            observations="Linux MemAvailable estimate, fixed state/rejection counters; no byte reservation. Native stdout/stderr observed in tool transcript, not archived raw",
            installation_health="CLI/worker plus same-ELF candidate HTTP reuse; no installed model start",
            performance_acceptance=False, numerical_evidence=str(PACKAGE / "numerical-reuse.json")))
    metadata["chat_protocol"].update(protocol_sha256=plan["protocol_sha256"],
        template=caps["template"], observability=caps["observability"],
        memory_admission=caps["memory_admission"],
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
    a.MARKER = ROOT / "build/serve-memory-recovery.json"
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
