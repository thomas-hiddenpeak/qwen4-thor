"""Last bounded stage A start; preserve the strict buffering non-observation."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import time

import stage
v = stage.module("serve_closeout_verification", Path(__file__).with_name("verify.py"))
h, a, ROOT = stage.h, stage.a, stage.ROOT
FIRST = ROOT / ".q4t-work/serve-stage-a-20261102"
PRIOR = ROOT / ".q4t-work/serve-stage-a-finish-20261102"
BASE = ROOT / ".q4t-work/serve-stage-a-final-20261102"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"


def prepare():
    h.require(not PACKAGE.exists(), "closeout already frozen")
    h.verify_seal(PRIOR / "package")
    result = h.read(PRIOR / "execution/business/result.json")
    h.require(result["status"] == "FAILED" and result["request_attempts"] == 8 and
              result["starts"] == 2 and result["negative_requests"] == 9 and
              result["error"] == "AssertionError('real TCP EAGAIN was not observed')",
              "unknown failure; no reuse")
    folder = PRIOR / "execution/business/candidate-fault"
    h.require(not (folder / "backpressure.entered").exists() and
              h.read(folder / "shutdown.json")["clean"], "partial backpressure/cleanup failure")
    v.r.cancellation_wire((folder / "proxy-response.bin").read_bytes(), "explicit_cancel")
    v.r.cancellation_wire((folder / "deadline-response.bin").read_bytes(), "deadline_exceeded")
    h.require(h.read(folder / "proxy-timing.json")["heartbeat_received"] and
              h.read(folder / "queued-ordinary-result.json")["message"]["content"].strip() == "710003",
              "mixed/proxy cases incomplete")
    plan = h.read(PRIOR / "package/plan.json")
    a.verify_hashes(ROOT, h.read(PRIOR / "package/candidate/runtime-source.json"))
    a.verify_hashes(ROOT / "build", plan["old_hashes"])
    h.require(time.time() < plan["original_started"] + 1500, "original install reserve")
    PACKAGE.mkdir(parents=True)
    for name in ("previous", "candidate", "cpu"):
        shutil.copytree(PRIOR / "package" / name, PACKAGE / name)
    shutil.copy2(PRIOR / "package/numerical-reuse.json", PACKAGE / "numerical-reuse.json")
    shutil.copy2(FIRST / "cpu/driver-closeout.log", PACKAGE / "cpu/driver-closeout.log")
    h.require("Ran 9 tests" in (PACKAGE / "cpu/driver-closeout.log").read_text() and
              (PACKAGE / "cpu/driver-closeout.log").read_text().rstrip().endswith("OK"), "driver gate")
    if not (PRIOR / "execution/sealed-files.json").exists():
        h.seal(PRIOR / "execution")
    h.verify_seal(PRIOR / "execution")
    sources = {name: h.sha(ROOT / name) for name in a.acceptance.source_paths()}
    sources["docs/serve-transport-v1.schema.json"] = h.sha(ROOT / "docs/serve-transport-v1.schema.json")
    h.save(PACKAGE / "sources.json", sources)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    h.save(PACKAGE / "reuse.json", dict(prior_package=h.identity(PRIOR / "package/sealed-files.json"),
        prior_execution=h.identity(PRIOR / "execution/sealed-files.json"),
        first_execution=h.identity(FIRST / "execution/sealed-files.json"),
        runtime_source_commit="e0d9f9d", runtime_unchanged=True,
        prior_attempts=8, prior_starts=2, remaining_attempts=4, remaining_starts=1,
        remaining=["incremental SSE real TCP backpressure", "ordinary recovery", "schema quality", "active shutdown"],
        not_proven="strict full buffered response did not reach writer within 180 seconds",
        installed_health="reuse identical production ELF ordinary/strict-tool HTTP; probe installed CLI and worker"))
    plan.update(source_commit=commit, candidate=str(PACKAGE / "candidate/q4t"),
        fault_candidate=h.identity(PACKAGE / "candidate/fault-q4t"),
        source_manifest_sha256=h.sha(PACKAGE / "sources.json"))
    h.save(PACKAGE / "plan.json", plan)
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"), "4a3f498..HEAD"],
                   cwd=ROOT, check=True)
    h.seal(PACKAGE)
    print("PREPARED_LAST_START", flush=True)


class Remaining(v.Run):
    def budget(self):
        h.require(time.time() < self.deadline and self.requests <= 4 and
                  self.negative == 0 and self.starts <= 1 and self.capability_gets <= 1,
                  "last-start budget exceeded")

    def remaining(self, folder):
        self.backpressure(folder)
        result = self.success(folder, "schema", v.s.request(
            "计算17乘19，answer为乘积，label为中文。", stream=True,
            response_format=v.s.response_schema()))
        import json
        h.require(json.loads(result.message["content"]) == {"answer": 323, "label": "中文"},
                  "schema quality")
        self.resources_empty(folder, "before-final-shutdown")
        self.shutdown_case(folder)

    def run(self):
        state = dict(status="FAILED")
        try:
            h.verify_seal(PRIOR / "execution")
            shutil.copytree(FIRST / "execution/business/candidate-mtp", self.out / "candidate-mtp")
            shutil.copytree(PRIOR / "execution/business/candidate-fault", self.out / "reused-mixed")
            self.model("candidate-fault", Path(self.plan["fault_candidate"]["path"]), True,
                       self.remaining, fault=True)
            h.require(self.requests == 4, "missing remaining cases")
            subprocess.run(["/usr/bin/python3", "-B", str(Path(__file__).with_name("check_schema.py")),
                            str(self.out)], cwd=ROOT, check=True, timeout=30)
            state["status"] = "PASS_BOUNDED_SERVE_STAGE_A"
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(request_attempts=self.requests + 8, negative_requests=9,
                starts=self.starts + 2, new_attempts=self.requests, new_starts=self.starts,
                seconds_since_first_launch=time.time() - self.plan["started"],
                failed_attempts_preserved=2, performance_acceptance=False)
            h.save(self.out / "result.json", state)


def installed_health(plan):
    h.require(h.sha(ROOT / "build/q4t") == plan["candidate_sha256"], "installed ELF changed")
    original = FIRST / "execution/business/candidate-mtp"
    h.verify_seal(FIRST / "execution")
    h.require(h.read(original / "command.json")["sha256"] == plan["candidate_sha256"] and
              h.read(OUTPUT / "business/result.json")["status"] == "PASS_BOUNDED_SERVE_STAGE_A",
              "same production ELF HTTP proof missing")
    for name in ("ordinary", "project-read", "project-answer"):
        h.identity(original / (name + "-result.json"))
    command = [str(ROOT / "build/q4t"), "version"]
    version = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
    h.require("Qwen4-Thor" in version.stdout, "installed CLI failed")
    parent, child = socket.socketpair()
    child_fd = child.fileno()
    actions = [(os.POSIX_SPAWN_DUP2, child_fd, 3)]
    if child_fd != 3:
        actions.append((os.POSIX_SPAWN_CLOSE, child_fd))
    pid = os.posix_spawn(command[0], [command[0], "--q4t-grammar-worker",
        str(os.getpid()), str(2 * 1024**3)], {"LC_ALL": "C"}, file_actions=actions)
    child.close()
    descriptor = None
    try:
        descriptor = os.pidfd_open(pid)
        parent.settimeout(5)
        raw = bytearray()
        while len(raw) < 24:
            part = parent.recv(24 - len(raw))
            h.require(part, "worker not ready")
            raw.extend(part)
        magic, code, flags, words, rss = struct.unpack("=IIIIQ", raw)
        h.require((magic, code, flags, words) == (0x51475431, 0, 0, 0) and
                  0 < rss <= 2 * 1024**3, "installed worker handshake failed")
    finally:
        if descriptor is not None:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)  # Still-unreaped owned child.
        os.waitpid(pid, 0)
        if descriptor is not None:
            os.close(descriptor)
        parent.close()
    folder = OUTPUT / "health"
    folder.mkdir()
    h.save(folder / "result.json", dict(status="PASS_SAME_ELF_HTTP_HEALTH_REUSED",
        binary=h.identity(ROOT / "build/q4t"), http_source=str(original),
        http_source_seal=h.identity(FIRST / "execution/sealed-files.json"),
        installed_cli_stdout=version.stdout, worker_reported_rss_bytes=rss,
        new_model_requests=0, new_model_starts=0))


def verifier(plan, installed=False):
    if installed:
        installed_health(plan)
    else:
        Remaining(plan, OUTPUT / "business").run()


def bind():
    stage.BASE, stage.PACKAGE, stage.OUTPUT = BASE, PACKAGE, OUTPUT
    stage.bind()
    a.MARKER = ROOT / "build/serve-stage-a-final-recovery.json"
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
            limits=stage.LIMITS, previous_attempts=8, previous_starts=2))
        a.execute()
    elif args.action == "finalize":
        a.finalize()
    else:
        h.require(args.output is not None, "fresh rollback lease required")
        a.restore(args.output.resolve())
