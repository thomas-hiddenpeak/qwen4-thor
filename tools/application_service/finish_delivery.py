"""Complete the known monitor-import failure without repeating model requests.

The previous candidate, business checks and installed health all passed; the
last independent NVML query failed to import pynvml in the SDK venv and the
known old pair was restored. Preserve that ENV_ABORT and its raw guard status.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import time

import stage as s

h, ROOT = s.h, s.ROOT
PRIOR = ROOT / ".q4t-work/application-service-correction-20261102"
BASE = ROOT / ".q4t-work/application-service-final-20261102"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
MARKER = ROOT / "build/application-service-final-recovery.json"


def prepare():
    h.require(not BASE.exists(), "finish stage already prepared")
    h.verify_seal(PRIOR / "package")
    h.verify_seal(PRIOR / "execution")
    execution = h.read(PRIOR / "execution/execution.json")
    health = h.read(PRIOR / "execution/health/result.json")
    query = h.read(PRIOR / "execution/final-gpu.json")
    h.require(execution.get("rollback") == "RESTORED" and
        execution.get("error") == "RuntimeError('GPU not released')" and
        query.get("error", {}).get("type") == "ModuleNotFoundError" and
        query.get("error", {}).get("message") == "No module named 'pynvml'",
        "not the recognized final monitor failure")
    h.require(health["status"] == "PASS_BOUNDED_APPLICATION_SERVICE" and
        h.read(PRIOR / "execution/business/result.json")["status"] ==
        "PASS_BOUNDED_APPLICATION_SERVICE", "business or health not accepted")
    # Every previous tested input remains identical except the monitor driver.
    sources = h.read(PRIOR / "package/sources.json")
    sources.pop("tools/application_service/stage.py")
    s.verify_hashes(ROOT, sources)
    s.verify_hashes(ROOT, h.read(PRIOR / "package/candidate/runtime-source.json"))
    h.require(not list((ROOT / "build").glob("*recovery*.json")), "recovery pending")
    old_binding = h.read(PRIOR / "execution/release/binding.json")
    s.verify_hashes(ROOT / "build", old_binding["old"])
    h.require(health["candidate_sha256"] == old_binding["new"]["q4t"],
              "health/candidate mismatch")
    BASE.mkdir()
    PACKAGE.mkdir()
    for name in ("previous", "candidate"):
        (PACKAGE / name).mkdir()
    for name in s.NAMES:
        h.copy_checked(h.identity(PRIOR / "package/previous" / name),
                       PACKAGE / "previous" / name)
    h.copy_checked(h.identity(PRIOR / "package/candidate/q4t"),
                   PACKAGE / "candidate/q4t")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                     cwd=ROOT, text=True).strip()
    metadata = h.read(PRIOR / "execution/release/q4t.release.json")
    metadata["tool_source_commit"] = commit
    metadata["deployment_completion"] = dict(
        previous_stage_status="ENV_ABORT_FINAL_MONITOR_IMPORT",
        raw_guard_status="COMMAND_FAILED", monitor_python="/usr/bin/python3",
        reused_health=str(PRIOR / "execution/health/result.json"),
        reused_health_sha256=h.sha(PRIOR / "execution/health/result.json"),
        model_requests_added=0, source_commit=commit)
    h.save(PACKAGE / "candidate/q4t.release.json", metadata)
    new = {name: h.sha(PACKAGE / "candidate" / name) for name in s.NAMES}
    tracked_tools = {name: h.sha(ROOT / name) for name in
        ("tools/application_service/stage.py",
         "tools/application_service/finish_delivery.py")}
    h.save(PACKAGE / "plan.json", dict(old=old_binding["old"], new=new,
        original_started=execution["original_started"], source_commit=commit,
        prior_package_sha256=h.sha(PRIOR / "package/sealed-files.json"),
        prior_execution_sha256=h.sha(PRIOR / "execution/sealed-files.json"),
        sources=sources, monitor_python=h.identity("/usr/bin/python3"),
        tools=tracked_tools))
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "5340cc4..HEAD"], cwd=ROOT, check=True)
    h.seal(PACKAGE)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE / "plan.json")
    h.require(h.sha(PRIOR / "package/sealed-files.json") ==
              plan["prior_package_sha256"] and
              h.sha(PRIOR / "execution/sealed-files.json") ==
              plan["prior_execution_sha256"], "prior seal changed")
    h.verify_seal(PRIOR / "package")
    h.verify_seal(PRIOR / "execution")
    s.verify_hashes(ROOT, plan["sources"])
    s.verify_hashes(ROOT, plan["tools"])
    h.require(h.identity("/usr/bin/python3") == plan["monitor_python"],
              "monitor interpreter changed")
    return plan


def restore(output):
    h.require_guard(output)
    plan = check()
    if MARKER.exists():
        h.require(h.read(MARKER).get("package") == str(PACKAGE), "foreign marker")
    else:
        h.save(MARKER, dict(package=str(PACKAGE), action="rollback"))
        h.fsync_directory(MARKER.parent)
    s.restore_pair(ROOT / "build", PACKAGE / "previous", PACKAGE / "candidate",
                   plan["old"], plan["new"])
    h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json", plan["old"]["q4t"])
    MARKER.unlink()
    h.fsync_directory(MARKER.parent)


def execute():
    h.require_guard(OUTPUT)
    plan = check()
    h.require(time.time() < plan["original_started"] + 1800, "original deadline")
    h.require(not list((ROOT / "build").glob("*recovery*.json")), "recovery pending")
    s.acceptance.install_interrupt_handlers()
    state, installed = {"status": "RUNNING", "started": time.time()}, False
    try:
        s.require_idle_gpu(OUTPUT / "initial-gpu.json")
        values = s.state_pair(ROOT / "build", PACKAGE / "previous",
                             PACKAGE / "candidate", plan["old"], plan["new"])
        h.require(values == plan["old"], "installed pair changed")
        h.save(MARKER, dict(package=str(PACKAGE), action="install"))
        h.fsync_directory(MARKER.parent)
        installed = True
        for name in s.NAMES:
            h.replace_checked(PACKAGE / "candidate" / name,
                              ROOT / "build" / name, values[name])
        h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json", plan["new"]["q4t"])
        s.require_idle_gpu(OUTPUT / "final-gpu.json")
        state["status"] = "INSTALLED_PENDING_GUARD"
    except BaseException as error:
        state.update(status="ENV_ABORT" if isinstance(error, s.EnvironmentAbort)
                     else "FAILED", error=repr(error))
        if installed:
            restore(OUTPUT)
            state["rollback"] = "RESTORED"
        raise
    finally:
        state["finished"] = time.time()
        h.save(OUTPUT / "execution.json", state)


def finalize():
    plan = check()
    h.require(h.read(OUTPUT / "lease/exit.json")["status"] == "COMPLETED" and
        h.read(OUTPUT / "execution.json")["status"] == "INSTALLED_PENDING_GUARD",
        "finish guard failed")
    s.verify_hashes(ROOT / "build", plan["new"])
    h.require(h.read(MARKER).get("package") == str(PACKAGE), "foreign marker")
    h.save(OUTPUT / "finalized.json", dict(status="DELIVERED", time=time.time(),
        installed=plan["new"], source_commit=plan["source_commit"],
        elapsed_from_original_start=time.time() - plan["original_started"]))
    MARKER.unlink()
    h.fsync_directory(MARKER.parent)
    print("DELIVERED", plan["new"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "finalize", "rollback"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "rollback":
        h.require(args.output is not None, "rollback requires fresh lease output")
        restore(args.output.resolve())
    else:
        globals()[args.action]()
