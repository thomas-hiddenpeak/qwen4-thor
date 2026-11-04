"""Finish the recognized cleanup-observation race without replaying passed work.

One remaining model start: ordinary/schema health plus active shutdown. Install
only after those pass; bind the same-ELF HTTP evidence and probe the installed
ELF/worker entry without another model load. Keep the original total deadline.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import struct
import subprocess
import time

import stage as s

a, h, ROOT = s.a, s.h, s.ROOT
PRIOR = ROOT / ".q4t-work/grammar-isolation-20261102-repair"
FIRST = ROOT / ".q4t-work/grammar-isolation-20261102"
BASE = ROOT / ".q4t-work/grammar-isolation-final-20261102"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
LIMITS = dict(starts=1, generations=4, negatives=5, seconds=1800)
v = s.module("grammar_finish_checks", Path(__file__).with_name("verify.py"))


def recognized_cleanup_failure(result, rows, log):
    h.require(result["status"] == "FAILED" and result["request_attempts"] == 7 and
        result["starts"] == 1 and result["overload_rejections"] == 1 and
        result["error"] == "AssertionError('worker leak or missing reap')",
        "not the recognized observation failure")
    h.require(rows["q4t_structured_workers_active"] == 1 and
        rows["q4t_structured_workers_started_total"] ==
        rows["q4t_structured_workers_reaped_total"] + 1,
        "failure was not the final response cleanup window")
    reports = re.findall(r"grammar worker reaped=1 ", log)
    h.require(len(reports) == rows["q4t_structured_workers_started_total"] and
        "shutdown complete (0 in-flight remaining)" in log and
        "event=end stage=response" in log and "outcome=success" in log,
        "final worker was not reaped")
    h.require(rows["q4t_structured_compile_timeouts_total"] == 1 and
        rows["q4t_structured_prepare_failed_total"] == 1 and
        rows["q4t_structured_resource_exhausted_total"] == 0,
        "additional preparation failure")


def prior_checks():
    h.verify_seal(PRIOR / "package")
    h.verify_seal(PRIOR / "execution")
    h.verify_seal(FIRST / "execution")
    folder = PRIOR / "execution/business/candidate-mtp"
    result = h.read(PRIOR / "execution/business/result.json")
    rows = v.r.metric_contract(h.read(folder / "before-final-shutdown-metrics.json")["raw"])
    recognized_cleanup_failure(result, rows, (folder / "server.log").read_text())
    h.require(h.read(PRIOR / "execution/lease/exit.json")["status"] == "COMMAND_FAILED"
        and h.read(folder / "shutdown.json")["clean"], "prior environment/shutdown failed")
    for label in ("compile-timeout", "compile-cancel"):
        h.require(h.read(folder / (label + "-result.json"))["passed"], "missing interrupt case")
    v.r.cancellation_wire((folder / "compile-cancel-wire.bin").read_bytes(), "explicit_cancel")
    v.r.cancellation_wire((folder / "cancelled-wire.bin").read_bytes(), "explicit_cancel")
    h.require(h.read(folder / "busy-response.json")["status"] == 503 and
        h.read(folder / "deadline-response.json")["status"] == 409,
        "missing overload/deadline proof")
    ordinary = h.read(folder / "queued-ordinary-result.json")
    read = h.read(folder / "project-read-result.json")
    answer = h.read(folder / "project-answer-result.json")
    h.require(ordinary["message"]["content"] == "710003" and
        read["finish_reason"] == "tool_calls" and
        read["message"]["tool_calls"][0]["function"]["name"] == "read_project_record" and
        "Qwen3.8-Flash-Next" in answer["message"]["content"] and
        "[docs/MODEL.md:L1-L8]" in answer["message"]["content"], "prior business quality")
    plan = h.read(PRIOR / "package/plan.json")
    h.require(h.read(folder / "command.json")["sha256"] == plan["candidate_sha256"],
              "prior HTTP executable mismatch")
    sources = h.read(PRIOR / "package/sources.json")
    sources.pop("tools/grammar_isolation/verify.py")
    a.verify_hashes(ROOT, sources)
    a.verify_hashes(ROOT, h.read(PRIOR / "package/candidate/runtime-source.json"))
    # Exactly the missing wait changed; passed scenarios and assertions stay put.
    previous = subprocess.check_output(["git", "show", "ad69ce4:tools/grammar_isolation/verify.py"],
                                      cwd=ROOT, text=True)
    current = (ROOT / "tools/grammar_isolation/verify.py").read_text().replace(
        '        self.observe(lambda m: m["q4t_structured_workers_active"] == 0 and\n'
        '                     m["q4t_structured_active"] == 0)\n', '')
    h.require(current == previous, "verifier changed beyond cleanup observation")
    return plan


def prepare():
    h.require(not BASE.exists(), "finish already frozen")
    plan = prior_checks()
    a.verify_hashes(ROOT / "build", plan["old_hashes"])
    PACKAGE.mkdir(parents=True)
    for name in ("previous", "candidate", "cpu"):
        shutil.copytree(PRIOR / "package" / name, PACKAGE / name)
    sources = {name: h.sha(ROOT / name) for name in a.acceptance.source_paths()}
    h.save(PACKAGE / "sources.json", sources)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    plan.update(source_commit=commit, runtime_source_commit="ad69ce4",
        candidate=str(PACKAGE / "candidate/q4t"), limits=LIMITS,
        source_manifest_sha256=h.sha(PACKAGE / "sources.json"),
        started=h.read(FIRST / "execution/begin.json")["started"],
        original_started=h.read(FIRST / "execution/begin.json")["started"],
        prior_attempts=8, prior_starts=2, prior_seal=h.identity(PRIOR / "execution/sealed-files.json"))
    h.save(PACKAGE / "plan.json", plan)
    subprocess.run(["git", "bundle", "create", str(PACKAGE / "source.bundle"),
                    "48e0682..HEAD"], cwd=ROOT, check=True)
    h.seal(PACKAGE)


class Remaining(v.Run):
    def budget(self):
        h.require(time.time() < self.deadline and self.requests <= 3 and
            self.starts <= 1 and self.negative == 0 and self.capability_gets <= 1,
            "remaining frozen budget exceeded")

    def remaining(self, folder):
        self.health(folder)
        self.shutdown_case(folder)

    def run(self):
        state = dict(status="FAILED")
        try:
            self.model("candidate-mtp", Path(self.plan["candidate"]), True, self.remaining)
            h.require(self.requests == 3 and self.starts == 1, "remaining cases missing")
            state.update(status="PASS_BOUNDED_GRAMMAR_ISOLATION",
                reused_prior_cases=str(PRIOR / "execution/business"),
                reused_prior_seal=self.plan["prior_seal"],
                total_attempts_with_failed_first=11, total_starts=3)
        except BaseException as error:
            state["error"] = repr(error)
            raise
        finally:
            state.update(request_attempts=self.requests, starts=self.starts,
                seconds=time.monotonic() - self.started, performance_acceptance=False)
            h.save(self.out / "result.json", state)


def installed_health(plan):
    h.require(h.sha(ROOT / "build/q4t") == plan["candidate_sha256"], "installed ELF changed")
    h.require(h.read(OUTPUT / "business/result.json")["status"] ==
              "PASS_BOUNDED_GRAMMAR_ISOLATION", "same-ELF HTTP health not accepted")
    command = [str(ROOT / "build/q4t"), "version"]
    version = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
    h.require("Qwen4-Thor" in version.stdout, "installed CLI entry failed")
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
            h.require(part, "installed worker closed before ready")
            raw.extend(part)
        magic, code, flags, words, rss = struct.unpack("=IIIIQ", raw)
        h.require((magic, code, flags, words) == (0x51475431, 0, 0, 0) and
            0 < rss <= 2 * 1024**3, "installed worker handshake/limit failed")
    finally:
        if descriptor is not None:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)  # Unreaped owned child, no PID reuse.
        os.waitpid(pid, 0)
        if descriptor is not None:
            os.close(descriptor)
        parent.close()
    directory = OUTPUT / "health"
    directory.mkdir()
    h.save(directory / "result.json", dict(status="PASS_SAME_ELF_HTTP_HEALTH_REUSED",
        binary_sha256=plan["candidate_sha256"], version=version.stdout,
        installed_worker_reported_rss_bytes=rss, new_model_requests=0,
        http_health=str(OUTPUT / "business/candidate-mtp"),
        scope="HTTP ordinary/schema on identical ELF before install; installed CLI/worker handshake"))


def verifier(plan, installed=False):
    if installed:
        installed_health(plan)
    else:
        Remaining(plan, OUTPUT / "business").run()


def bind():
    s.BASE, s.PACKAGE, s.OUTPUT, s.LIMITS = BASE, PACKAGE, OUTPUT, LIMITS
    s.bind()
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
        prior_checks()
        a.execute()
    elif args.action == "finalize":
        a.finalize()
    else:
        h.require(args.output is not None, "fresh lease required")
        a.restore(args.output.resolve())
