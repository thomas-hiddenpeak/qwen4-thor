"""Independent copy-conditioned strict transport acceptance; no installation."""
import argparse
import datetime
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_strict_transport import build
from tools.serve_observability import stage as previous

h, a = previous.h, previous.a
BASE, PACKAGE, OUTPUT = build.BASE, build.BASE / "package", build.BASE / "execution"
LIMITS = dict(generations=6, negatives=4, starts=2, seconds=1800)
PLANNED = dict(generations=6, negatives=4, starts=2)
DEFAULT_SHA = "ed672a65b586f8445e51e3ba6d63659b4a098386e87e1ed38e3c8893a352a139"
SIDECAR_SHA = "67897c817dfccd6db3d4e2c0d0af3cfc70fb97a2c840887905c03fbf0bd6be23"


def build_gate():
    result = h.read(BASE / "fault-build/result.json")
    h.require(result["passed"] and result["zero_warnings"] and
              result["binary_sha256"] == h.sha(BASE / "fault-engine/q4t"), "fault build gate")
    current = build.sources()
    h.require(current == h.read(BASE / "fault-build/sources.json"), "fault sources changed")
    prior = h.read(previous.PACKAGE / "candidate/runtime-source.json")
    changed = {p for p in prior if current[p] != prior[p]}
    h.require(changed == {"src/server/chat_generation.cpp", "include/q4t/server/serve_fault.h"},
              "changes exceed validation-only token observation")
    cache = previous.previous.cache
    old = cache(previous.BASE / "engine/CMakeCache.txt")
    new = cache(BASE / "fault-engine/CMakeCache.txt")
    keys = {k for k in old if k.startswith("Q4T_")} | {
        "CMAKE_BUILD_TYPE", "CMAKE_CXX_COMPILER", "CMAKE_CUDA_COMPILER",
        "CMAKE_CUDA_HOST_COMPILER", "CMAKE_CXX_FLAGS", "CMAKE_CUDA_FLAGS",
        "CMAKE_CXX_FLAGS_RELEASE", "CMAKE_CUDA_FLAGS_RELEASE",
        "CMAKE_EXE_LINKER_FLAGS", "FETCHCONTENT_SOURCE_DIR_Q4T_XGRAMMAR"}
    h.require(old["Q4T_SERVE_FAULT_INJECTION"] == "OFF" and
              new["Q4T_SERVE_FAULT_INJECTION"] == "ON" and
              all(old[k] == new[k] for k in keys-{"Q4T_SERVE_FAULT_INJECTION"}),
              "unexpected build flags")
    commands = h.read(BASE / "fault-engine/compile_commands.json")
    server = [r for r in commands if r["output"].startswith("CMakeFiles/q4t_server.dir/")]
    h.require(server and all("-DQ4T_SERVE_FAULT_INJECTION=1" in r["command"] for r in server),
              "validation macro is not enabled for server")
    a.verify_hashes(ROOT, h.read(BASE / "fault-engine/generated/q4t/server/chat_protocol_sources.json"))
    return current


def prepare():
    h.require(not PACKAGE.exists(), "preserve frozen stage")
    h.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
              "commit reviewed implementation/driver before freezing")
    h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json", DEFAULT_SHA)
    h.require(h.sha(ROOT / "build/q4t.release.json") == SIDECAR_SHA, "default metadata drift")
    h.require(not list((ROOT / "build").glob("*recovery*.json")), "recovery marker present")
    runtime = build_gate()
    cpu = h.read(BASE / "cpu/result.json")
    h.require(cpu["passed"] and not cpu["actual_model_generation_proven"] and
              cpu["production_sha256"] == DEFAULT_SHA, "CPU prerequisite")
    h.require(h.read(BASE / "diagnostic/result.json")["passed"], "diagnostic prerequisite")
    original = h.read(BASE / "driver-result.json")
    corrected = h.read(BASE / "driver-corrected-result.json")
    h.require(not original["passed"] and corrected["passed"] and
              "Ran 9 tests" in (BASE / "driver-tests.log").read_text() and
              "NameError: name 'socket' is not defined" in
                  (BASE / "driver-tests.log").read_text() and
              "Ran 1 test" in (BASE / "driver-corrected-tests.log").read_text(),
              "preserve original failure and affected rerun; eight checks reused")
    a.verify_hashes(ROOT, h.read(BASE / "driver-corrected-sources.json"))
    old_plan = h.read(previous.PACKAGE / "plan.json")
    a.verify_hashes(old_plan["model_dir"], old_plan["model_inputs"])
    h.verify_seal(previous.PACKAGE)
    h.verify_seal(previous.OUTPUT)
    h.require(h.read(previous.OUTPUT / "finalized.json")["status"] == "DELIVERED",
              "production reuse not accepted")
    PACKAGE.mkdir()
    for name in ("cpu", "diagnostic", "fault-build", "driver-first-failure"):
        shutil.copytree(BASE / name, PACKAGE / name)
    (PACKAGE / "payload").mkdir()
    for name in ("json-request.json", "sse-request.json", "expected-content.json"):
        h.copy_checked(h.identity(BASE / "cpu" / name), PACKAGE / "payload" / name)
    for name in ("driver-result.json", "driver-sources.json", "driver-tests.log",
                 "driver-corrected-result.json", "driver-corrected-sources.json",
                 "driver-corrected-tests.log", "planned-native.json"):
        h.copy_checked(h.identity(BASE / name), PACKAGE / name)
    (PACKAGE / "candidate").mkdir()
    for name in ("q4t", "CMakeCache.txt", "compile_commands.json"):
        h.copy_checked(h.identity(BASE / "fault-engine" / name), PACKAGE / "candidate" / name)
    shutil.copytree(BASE / "fault-engine/generated", PACKAGE / "candidate/generated")
    h.save(PACKAGE / "runtime-source.json", runtime)
    h.save(PACKAGE / "reuse.json", dict(default_sha256=DEFAULT_SHA,
        changed=[p for p in runtime if runtime[p] != h.read(BASE / "cpu/runtime-sources.json")[p]],
        model_template_tokenizer_grammar_parser_unchanged=True,
        validation_only_generated_ids=True, production_replaced=False,
        old_experiment_restarted=False, performance_acceptance=False))
    names = set(a.acceptance.source_paths()) | {"docs/SERVE_STRICT_TRANSPORT_2026-11-03.md"}
    names.update(str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("serve-*.json"))
    h.save(PACKAGE / "sources.json", {name:h.sha(ROOT / name) for name in sorted(names)})
    generation = h.read(Path(old_plan["model_dir"]) / "generation_config.json")
    model_inputs = dict(old_plan["model_inputs"])
    model_inputs["generation_config.json"] = h.sha(Path(old_plan["model_dir"]) / "generation_config.json")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"), "57e0f28c..HEAD"],
                   cwd=ROOT, check=True, timeout=60)
    h.save(PACKAGE / "plan.json", dict(schema_version=1, root=str(ROOT),
        source_commit=commit, limits=LIMITS, planned=PLANNED,
        binary=h.identity(PACKAGE / "candidate/q4t"),
        default=h.identity(ROOT / "build/q4t"), sidecar=h.identity(ROOT / "build/q4t.release.json"),
        source_manifest=h.identity(PACKAGE / "sources.json"),
        model_dir=old_plan["model_dir"], model_inputs=model_inputs,
        stop_ids=generation["eos_token_id"], tokenizers_version=importlib.metadata.version("tokenizers"),
        port=18104, default_replacement=False, performance_acceptance=False))
    h.seal(PACKAGE)
    print("PREPARED copy-conditioned strict transport; no automatic retries", flush=True)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE / "plan.json")
    h.require(plan["root"] == str(ROOT) and plan["limits"] == LIMITS and
              plan["planned"] == PLANNED, "changed frozen scope")
    for name in ("binary", "default", "sidecar", "source_manifest"):
        h.check_identity(plan[name])
    a.verify_hashes(ROOT, h.read(plan["source_manifest"]["path"]))
    a.verify_hashes(plan["model_dir"], plan["model_inputs"])
    h.require(importlib.metadata.version("tokenizers") == plan["tokenizers_version"],
              "tokenizer evidence decoder changed")
    return plan


def guard_command():
    return ["/usr/bin/python3", "-B", str(ROOT / "tools/evalscope/gpu_exclusive.py"),
            "--output", str(OUTPUT / "lease"), "--", "/usr/bin/python3", "-B",
            str(Path(__file__).resolve()), "execute"]


def execute():
    h.require_guard(OUTPUT)
    plan = check()
    events = [json.loads(line) for line in (OUTPUT / "lease/events.jsonl").read_text().splitlines()]
    plan["started"] = datetime.datetime.fromisoformat(next(
        row["utc"] for row in events if row["event"] == "lease_acquired")).timestamp()
    h.save(OUTPUT / "begin.json", dict(started=plan["started"], limits=LIMITS))
    a.require_idle_gpu(OUTPUT / "gpu-before.json")
    from tools.serve_strict_transport.verify import Run
    Run(plan, OUTPUT / "business").run()
    a.require_idle_gpu(OUTPUT / "gpu-after.json")
    check()


def finalize():
    plan = check()
    guard = h.read(OUTPUT / "lease/exit.json")
    h.require(guard["status"] == "COMPLETED" and guard["child_returncode"] == 0,
              "guard failed")
    result = h.read(OUTPUT / "business/result.json")
    h.require(result["status"] == "PASS_BOUNDED_STRICT_COPY_TRANSPORT" and
              result["counts"] == PLANNED, "business acceptance failed")
    seconds = time.time()-h.read(OUTPUT / "begin.json")["started"]
    h.require(seconds <= LIMITS["seconds"], "original independent stage deadline")
    h.save(OUTPUT / "finalized.json", dict(status="ACCEPT_BOUNDED_STRICT_COPY_TRANSPORT",
        seconds=seconds, binary=plan["binary"], default=plan["default"], sidecar=plan["sidecar"],
        default_replaced=False, performance_acceptance=False,
        scope="Copy-conditioned fixed >12KiB strict content; real small-window TCP SSE cancellation and JSON write deadline; owned resources/queued ordinary/strict recovery. Not arbitrary payload/network or an explanation of the old failure."))
    h.seal(OUTPUT)
    print("ACCEPT_BOUNDED_STRICT_COPY_TRANSPORT; default unchanged", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "launch", "execute", "finalize"))
    args = parser.parse_args()
    if args.action == "launch":
        check()
        OUTPUT.mkdir(exist_ok=False)
        raise SystemExit(subprocess.call(guard_command(), cwd=ROOT))
    globals()[args.action]()


if __name__ == "__main__":
    main()
