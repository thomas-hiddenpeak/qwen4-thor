"""Finish only the unrun stage A cases within the original wall/start budget."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import time

import stage
verification = stage.module("serve_finish_verification", Path(__file__).with_name("verify.py"))
Run, wire = verification.Run, verification.wire

h, a, ROOT = stage.h, stage.a, stage.ROOT
FIRST = ROOT / ".q4t-work/serve-stage-a-20261102"
BASE = ROOT / ".q4t-work/serve-stage-a-finish-20261102"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"


def prior_evidence():
    result = h.read(FIRST / "execution/business/result.json")
    h.require(result["status"] == "FAILED" and result["request_attempts"] == 4 and
              result["negative_requests"] == 8 and result["starts"] == 1 and
              result["error"] == "AssertionError('no heartbeat through seven-second idle proxy')",
              "unrecognized previous failure")
    folder = FIRST / "execution/business/candidate-mtp"
    log = (folder / "server.log").read_text()
    h.require("trace_id=proxy-trace status=503 error=structured_capacity_exhausted" in log and
              "id=transport-proxy event=admitted" not in log and
              h.read(folder / "shutdown.json")["clean"], "failure was not the known cleanup race")
    for name, reason in (("ordinary", "stop"), ("project-read", "tool_calls"),
                         ("project-answer", "stop")):
        h.require(h.read(folder / (name + "-result.json"))["finish_reason"] == reason,
                  "prior successful case missing")
    launch = h.read(FIRST / "execution/launch-failure.json")
    # Include even the zero-request ancestry failure in the original wall budget.
    return datetime.fromisoformat(launch["original_exit"]["finished"]["utc"]).timestamp() - 0.2


def prepare():
    h.require(not PACKAGE.exists(), "finish package already exists")
    h.verify_seal(FIRST / "package")
    original_started = prior_evidence()
    plan = h.read(FIRST / "package/plan.json")
    a.verify_hashes(ROOT, h.read(FIRST / "package/candidate/runtime-source.json"))
    a.verify_hashes(ROOT / "build", plan["old_hashes"])
    h.require(time.time() < original_started + 1500, "original installation reserve exhausted")
    PACKAGE.mkdir(parents=True)
    for name in ("previous", "candidate", "cpu"):
        shutil.copytree(FIRST / "package" / name, PACKAGE / name)
    for name in ("driver-repair.log", "driver-repair-final.log"):
        shutil.copy2(FIRST / "cpu" / name, PACKAGE / "cpu" / name)
    h.require("Ran 8 tests" in (PACKAGE / "cpu/driver-repair-final.log").read_text() and
              (PACKAGE / "cpu/driver-repair-final.log").read_text().rstrip().endswith("OK"),
              "finish driver contracts failed")
    shutil.copy2(FIRST / "package/numerical-reuse.json", PACKAGE / "numerical-reuse.json")
    if not (FIRST / "execution/sealed-files.json").exists():
        h.seal(FIRST / "execution")
    h.verify_seal(FIRST / "execution")
    sources = {name: h.sha(ROOT / name) for name in a.acceptance.source_paths()}
    sources["docs/serve-transport-v1.schema.json"] = h.sha(ROOT / "docs/serve-transport-v1.schema.json")
    h.save(PACKAGE / "sources.json", sources)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"), "4a3f498..HEAD"],
                   cwd=ROOT, check=True)
    h.save(PACKAGE / "reuse.json", dict(prior_package=h.identity(FIRST / "package/sealed-files.json"),
        prior_execution=h.identity(FIRST / "execution/sealed-files.json"),
        completed=["transport 8 negatives + readiness", "ordinary", "real project tool and answer"],
        not_reused=["proxy and mixed cancellation", "deadline", "TCP backpressure", "shutdown", "installed health"],
        initial_runtime_source=plan["source_commit"], runtime_unchanged=True,
        remaining_starts=2, remaining_request_attempts=8, remaining_negatives=8,
        validation_scope="Pending long-wait/mixed/backpressure/shutdown cases share the instrumented build. Installed health uses production ELF."))
    plan.update(source_commit=commit, original_started=original_started,
        candidate=str(PACKAGE / "candidate/q4t"),
        fault_candidate=h.identity(PACKAGE / "candidate/fault-q4t"),
        source_manifest_sha256=h.sha(PACKAGE / "sources.json"))
    h.save(PACKAGE / "plan.json", plan)
    h.seal(PACKAGE)
    print("PREPARED_REMAINING", plan["candidate_sha256"], flush=True)


class FinishRun(Run):
    def budget(self):
        wire.require(time.time() < self.deadline, "original stage deadline")
        wire.require(self.requests <= (2 if self.installed else 6) and
                     self.negative <= (0 if self.installed else 1) and
                     self.starts <= 1 and self.capability_gets <= 1,
                     "remaining stage A budget")

    def remaining(self, folder):
        self.proxy_cancel(folder)
        self.deadline_case(folder)
        self.backpressure(folder)
        self.resources_empty(folder, "before-final-shutdown")
        self.shutdown_case(folder)

    def run(self):
        if self.installed:
            return super().run()
        state = dict(status="FAILED")
        try:
            h.verify_seal(FIRST / "execution")
            shutil.copytree(FIRST / "execution/business/candidate-mtp", self.out / "candidate-mtp")
            wire.save(self.out / "reused.json", dict(source=str(FIRST / "execution/business"),
                seal=h.identity(FIRST / "execution/sealed-files.json"), prior_attempts=4, prior_negatives=8, prior_starts=1))
            self.model("candidate-fault", Path(self.plan["fault_candidate"]["path"]), True,
                       self.remaining, fault=True)
            wire.require(self.requests == 6 and self.negative == 1, "remaining cases incomplete")
            subprocess.run(["/usr/bin/python3", "-B", str(Path(__file__).with_name("check_schema.py")),
                            str(self.out)], cwd=ROOT, check=True, timeout=30)
            state["status"] = "PASS_BOUNDED_SERVE_STAGE_A"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(request_attempts=self.requests + 4, negative_requests=self.negative + 8,
                starts=self.starts + 1, new_attempts=self.requests, new_starts=self.starts,
                seconds_since_first_launch=time.time() - self.plan["started"],
                prior_rejected_generation_attempts=1, performance_acceptance=False)
            wire.save(self.out / "result.json", state)


def verifier(plan, installed=False):
    FinishRun(plan, OUTPUT / ("health" if installed else "business"), installed=installed).run()


def bind():
    stage.BASE, stage.PACKAGE, stage.OUTPUT = BASE, PACKAGE, OUTPUT
    stage.bind()
    a.MARKER = ROOT / "build/serve-stage-a-finish-recovery.json"
    a.verifier = verifier


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "finalize", "rollback"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    bind()
    if args.action == "prepare":
        prepare()
    elif args.action == "execute":
        h.require_guard(OUTPUT)
        h.save(OUTPUT / "begin.json", dict(started=h.read(PACKAGE / "plan.json")["original_started"],
            limits=stage.LIMITS, previous_attempts=4, previous_starts=1))
        a.execute()
    elif args.action == "finalize":
        a.finalize()
    else:
        h.require(args.output is not None, "fresh rollback lease required")
        a.restore(args.output.resolve())
