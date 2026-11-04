"""SRV-03b: same-source fault binary; never install or replace the default."""
import argparse
import datetime
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
BASE = ROOT / ".q4t-work/serve-backpressure-20261103"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
LIMITS = dict(generations=6, negatives=8, starts=2, seconds=1800)


def cache(path):
    return {match[1]: match[2] for line in Path(path).read_text().splitlines()
            if (match := re.match(r"([^/#][^:]*):[^=]+=(.*)$", line))}


def build_gate():
    result = h.read(BASE / "build-record/result.json")
    h.require(result["passed"] and result["zero_warnings"], "build failed")
    h.check_identity(result["binary"])
    a.verify_hashes(ROOT, h.read(BASE / "build-record/runtime-source.json"))
    old = cache(prior.BASE.parent / "serve-preflight-20261103/engine/CMakeCache.txt")
    new = cache(BASE / "engine/CMakeCache.txt")
    keys = {k for k in old if k.startswith("Q4T_")} | {
        "CMAKE_BUILD_TYPE", "CMAKE_CXX_COMPILER", "CMAKE_CUDA_COMPILER",
        "CMAKE_CUDA_HOST_COMPILER", "CMAKE_CXX_FLAGS", "CMAKE_CUDA_FLAGS",
        "CMAKE_CXX_FLAGS_RELEASE", "CMAKE_CUDA_FLAGS_RELEASE",
        "CMAKE_EXE_LINKER_FLAGS", "FETCHCONTENT_SOURCE_DIR_Q4T_XGRAMMAR"}
    h.require(new["Q4T_SERVE_FAULT_INJECTION"] == "ON" and
              old["Q4T_SERVE_FAULT_INJECTION"] == "OFF", "fault build switch")
    h.require(all(new[k] == old[k] for k in keys - {"Q4T_SERVE_FAULT_INJECTION"}),
              "build configuration drift")
    for name in ("configure", "build"):
        log = (BASE / f"build-record/{name}.log").read_text().lower()
        h.require("warning:" not in log and "cmake warning" not in log,
                  "build diagnostic")
    generated = BASE / "engine/generated/q4t/server"
    original = prior.BASE.parent / "serve-preflight-20261103/engine/generated/q4t/server"
    for path in original.iterdir():
        if path.is_file():
            h.require(h.sha(path) == h.sha(generated / path.name),
                      "generated protocol identity drift")
    commands = h.read(BASE / "engine/compile_commands.json")
    server = [row for row in commands if
              row["output"].startswith("CMakeFiles/q4t_server.dir/")]
    h.require(server and all("-DQ4T_SERVE_FAULT_INJECTION=1" in row["command"]
                            for row in server), "fault macro missing")
    return result


def prepare():
    h.require(not PACKAGE.exists(), "preserve existing package")
    h.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
              "commit reviewed driver before freezing")
    result = build_gate()
    meta = h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json",
                         prior.DEFAULT_SHA)
    h.require(h.sha(ROOT / "build/q4t.release.json") == prior.SIDECAR_SHA,
              "default sidecar changed")
    runtime = h.read(meta["tested_source_manifest"])
    a.verify_hashes(ROOT, runtime)
    h.require(runtime == h.read(BASE / "build-record/runtime-source.json"),
              "validation build is not same source")
    payload = h.read(BASE / "payload-array/result.json")
    h.require(payload["passed"] and payload["content_bytes"] == 12443 and
              payload["canonical_tokens"] == 243, "payload planning gate")
    for key in ("planner", "probe"):
        h.require(h.sha(payload[key]["path"]) == payload[key]["sha256"],
                  "payload dependency changed: " + key)
    test_log = (BASE / "cpu/driver-tests.log").read_text()
    h.require("Ran 8 tests" in test_log and test_log.endswith("FAILED (errors=1)\n") and
              "RuntimeError: fault macro missing" in test_log, "preserve first CPU result")
    repaired = (BASE / "cpu/build-gate-repaired.log").read_text()
    h.require("Ran 1 test" in repaired and repaired.endswith("OK\n"),
              "build-target repair missing; seven unaffected checks reused")
    prior_plan = h.read(prior.PACKAGE / "plan.json")
    h.verify_seal(prior.PACKAGE)
    h.verify_seal(prior.OUTPUT)
    h.require(h.read(prior.OUTPUT / "finalized.json")["status"] ==
              "ACCEPT_BOUNDED_PROTOCOL_CLIENT", "production reuse unavailable")
    a.verify_hashes(prior_plan["model_dir"], prior_plan["model_inputs"])
    PACKAGE.mkdir()
    for name in ("build-record", "payload", "payload-array", "cpu"):
        shutil.copytree(BASE / name, PACKAGE / name)
    candidate = PACKAGE / "candidate"
    candidate.mkdir()
    for name in ("q4t", "CMakeCache.txt", "compile_commands.json"):
        h.copy_checked(h.identity(BASE / "engine" / name), candidate / name)
    shutil.copytree(BASE / "engine/generated", candidate / "generated")
    h.save(PACKAGE / "runtime-source.json", runtime)
    paths = set(a.acceptance.source_paths())
    paths.update(str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("serve-*.json"))
    paths.add("docs/SERVE_BACKPRESSURE_2026-11-03.md")
    h.save(PACKAGE / "sources.json", {p: h.sha(ROOT / p) for p in sorted(paths)})
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "507fba63..HEAD"], cwd=ROOT, check=True, timeout=60)
    plan = dict(schema_version=1, limits=LIMITS, root=str(ROOT),
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        binary=h.identity(candidate / "q4t"),
        default=h.identity(ROOT / "build/q4t"), sidecar=h.identity(ROOT / "build/q4t.release.json"),
        model_dir=prior_plan["model_dir"], model_inputs=prior_plan["model_inputs"],
        protocol_sha256=prior_plan["protocol_sha256"], port=18100,
        prior_acceptance=h.identity(prior.OUTPUT / "finalized.json"),
        source_manifest=h.identity(PACKAGE / "sources.json"),
        default_replacement=False, performance_acceptance=False)
    h.save(PACKAGE / "plan.json", plan)
    h.seal(PACKAGE)
    print("PREPARED same runtime; validation binary " + result["binary"]["sha256"], flush=True)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE / "plan.json")
    h.require(plan["limits"] == LIMITS and plan["root"] == str(ROOT), "changed scope")
    for key in ("binary", "default", "sidecar", "source_manifest", "prior_acceptance"):
        h.check_identity(plan[key])
    a.verify_hashes(ROOT, h.read(plan["source_manifest"]["path"]))
    a.verify_hashes(plan["model_dir"], plan["model_inputs"])
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
    from tools.serve_backpressure.verify import Run
    Run(plan, OUTPUT / "business").run()
    a.require_idle_gpu(OUTPUT / "gpu-after.json")
    check()


def finalize():
    plan = check()
    status = h.read(OUTPUT / "lease/exit.json")
    h.require(status["status"] == "COMPLETED" and status["child_returncode"] == 0,
              "guard failed")
    result = h.read(OUTPUT / "business/result.json")
    h.require(result["status"] == "PASS_BOUNDED_STRICT_BACKPRESSURE", "business gate")
    seconds = time.time() - h.read(OUTPUT / "begin.json")["started"]
    h.require(seconds <= LIMITS["seconds"], "original stage deadline")
    h.save(OUTPUT / "finalized.json", dict(status="ACCEPT_BOUNDED_STRICT_BACKPRESSURE",
        seconds=seconds, binary=plan["binary"], default=plan["default"], sidecar=plan["sidecar"],
        default_replaced=False, performance_acceptance=False,
        scope="Same-source validation binary, actual small-window TCP EAGAIN after strict validation; SSE cancel and JSON write timeout, queued ordinary/strict recovery. No production-network or arbitrary-payload guarantee."))
    h.seal(OUTPUT)
    print("ACCEPT_BOUNDED_STRICT_BACKPRESSURE; default unchanged", flush=True)


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
