"""Freeze and verify the client/contract release against the unchanged default.

No pair replacement, automatic restart or automatic retry. GPU work is only
allowed beneath the matching shared lease; all failure evidence is retained.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BASE = ROOT / ".q4t-work/serve-protocol-20261103"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
LIMITS = dict(generations=10, negatives=20, starts=2, seconds=1800)
DEFAULT_SHA = "5642eb68d0e2d9296aeb0d5f4197797a18d064b2de749c4368a86fce918167ce"
SIDECAR_SHA = "6aca6a3ebf951ed8e625ff4944b73c34f40f3854b92497be2a8abe485315e382"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


a = module("protocol_shared_delivery", ROOT / "tools/application_service/stage.py")
h = a.h


def sdk_identity():
    node = Path(shutil.which("node")).resolve(strict=True)
    version = subprocess.check_output([str(node), "--version"], text=True).strip()
    h.require(version == "v24.18.0", "Node version drift")
    directory = BASE / "sdk"
    metadata = h.read(directory / "node_modules/openai/package.json")
    h.require(metadata["version"] == "7.47.0", "SDK version drift")
    for name in ("package.json", "package-lock.json"):
        h.require(h.sha(directory / name) == h.sha(Path(__file__).with_name(name)),
                  "SDK lock drift")
    paths = sorted(p for p in directory.rglob("*") if p.is_file())
    h.require(paths and all(not p.is_symlink() for p in paths), "SDK symlink or empty")
    return dict(node=h.identity(node), node_version=version, sdk_version=metadata["version"],
                sdk_directory=str(directory), files={str(p.relative_to(directory)): h.sha(p) for p in paths})


def node_report_passed(log):
    summary = {line.removeprefix("# ").removeprefix("ℹ ") for line in log.splitlines()}
    return {"tests 35", "pass 35", "fail 0", "cancelled 0"} <= summary


def cpu_gate():
    cpu = BASE / "cpu"
    first = h.read(cpu / "contracts-loaded/result.json")
    fixed = h.read(cpu / "contracts-repaired/result.json")
    h.require(first["cases"] == 396 and first["fields"] == 38 and len(first["failures"]) == 5 and
              all(r["native"]["accepted"] == r["expected"] for r in first["failures"]),
              "native/shape baseline mismatch")
    h.require(fixed["passed"] and fixed["cases"] == 5 and fixed["prior_result"]["sha256"] ==
              h.sha(cpu / "contracts-loaded/result.json"), "targeted repairs missing")
    h.require(h.sha(cpu / "contract-build/contract_probe") == fixed["probe_sha256"] ==
              first["probe_sha256"], "CPU probe changed")
    build = h.read(cpu / "contract-build/build-command.json")
    a.verify_hashes(ROOT, build["sources"])
    h.require(not (cpu / "contract-build/build.log").read_text(), "CPU compile diagnostic")
    js = (cpu / "js-tests-loaded.log").read_text()
    h.require(node_report_passed(js), "JS gate")
    for name, count in (("bridge-tests.log", 12), ("stage-tests-import-repaired.log", 5),
                        ("node-gate-test.log", 1)):
        log = (cpu / name).read_text()
        h.require(f"Ran {count} test" in log and log.endswith("OK\n"), "gate: " + name)
    original = (cpu / "stage-tests.log").read_text()
    h.require("Ran 8 tests" in original and original.endswith("FAILED (errors=5)\n"),
              "retain original driver import failure; three unaffected tests reused")
    subprocess.run(["/usr/bin/python3", "-B", str(Path(__file__).with_name("build_contract.py")),
                    "--check"], cwd=ROOT, check=True, timeout=30)


def prepare():
    h.require(not PACKAGE.exists(), "package exists; preserve it")
    h.require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT),
              "commit the reviewed stage before freezing")
    metadata = h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json", DEFAULT_SHA)
    h.require(h.sha(ROOT / "build/q4t.release.json") == SIDECAR_SHA, "default sidecar changed")
    h.require(not list((ROOT / "build").glob("*recovery*.json")), "pending recovery")
    cpu_gate()
    sdk = sdk_identity()
    runtime = h.read(metadata["tested_source_manifest"])
    h.require(h.sha(metadata["tested_source_manifest"]) == metadata["tested_source_manifest_sha256"],
              "runtime manifest differs")
    a.verify_hashes(ROOT, runtime)
    old_plan = h.read(ROOT / ".q4t-work/serve-preflight-20261103/package-final/plan.json")
    a.verify_hashes(old_plan["model_dir"], old_plan["model_inputs"])
    PACKAGE.mkdir()
    shutil.copytree(BASE / "cpu", PACKAGE / "cpu")
    h.save(PACKAGE / "runtime-source.json", runtime)
    paths = set(a.acceptance.source_paths())
    paths.update(str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("serve-*.json"))
    paths.update(("docs/SERVE_PROTOCOL_2026-11-03.md", "docs/MODEL.md", "docs/ARCHITECTURE.md"))
    h.save(PACKAGE / "sources.json", {p: h.sha(ROOT / p) for p in sorted(paths)})
    h.save(PACKAGE / "sdk.json", sdk)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "a030143a..HEAD"], cwd=ROOT, check=True, timeout=60)
    plan = dict(schema_version=1, limits=LIMITS, root=str(ROOT), source_commit=commit,
        binary=h.identity(ROOT / "build/q4t"), sidecar=h.identity(ROOT / "build/q4t.release.json"),
        model_dir=old_plan["model_dir"], model_inputs=old_plan["model_inputs"],
        protocol_sha256=old_plan["protocol_sha256"], sdk_manifest=h.identity(PACKAGE / "sdk.json"),
        source_manifest=h.identity(PACKAGE / "sources.json"), port=18099,
        default_replacement=False, performance_acceptance=False,
        prior_runtime_acceptance=h.identity(Path(metadata["acceptance_decision"])))
    h.save(PACKAGE / "plan.json", plan)
    h.seal(PACKAGE)
    print("PREPARED unchanged default " + DEFAULT_SHA, flush=True)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE / "plan.json")
    h.require(plan["limits"] == LIMITS and plan["root"] == str(ROOT), "changed stage scope")
    for key in ("binary", "sidecar", "sdk_manifest", "source_manifest", "prior_runtime_acceptance"):
        h.check_identity(plan[key])
    a.verify_hashes(ROOT, h.read(plan["source_manifest"]["path"]))
    a.verify_hashes(plan["model_dir"], plan["model_inputs"])
    h.require(sdk_identity() == h.read(plan["sdk_manifest"]["path"]), "SDK/Node changed")
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
    verify = module("protocol_http_driver", Path(__file__).with_name("verify.py"))
    verify.Run(plan, OUTPUT / "business").run()
    a.require_idle_gpu(OUTPUT / "gpu-after.json")
    check()


def finalize():
    plan = check()
    status = h.read(OUTPUT / "lease/exit.json")
    h.require(status["status"] == "COMPLETED" and status["child_returncode"] == 0,
              "guard did not complete")
    result = h.read(OUTPUT / "business/result.json")
    h.require(result["status"] == "PASS_BOUNDED_PROTOCOL_CLIENT", "business gate")
    started = h.read(OUTPUT / "begin.json")["started"]
    h.require(time.time() - started <= LIMITS["seconds"], "original stage deadline")
    h.save(OUTPUT / "finalized.json", dict(status="ACCEPT_BOUNDED_PROTOCOL_CLIENT",
        seconds=time.time() - started, binary=plan["binary"], sidecar=plan["sidecar"],
        default_replaced=False, performance_acceptance=False,
        scope="Complete supported text input surface plus required native semantics; pinned JS SDK and bounded business/transport recovery. No full OpenAI/multi-tenant acceptance."))
    h.seal(OUTPUT)
    print("ACCEPT_BOUNDED_PROTOCOL_CLIENT; default pair unchanged", flush=True)


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
