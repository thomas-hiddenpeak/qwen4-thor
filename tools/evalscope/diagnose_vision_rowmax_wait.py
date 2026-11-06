"""One frozen wait-stack diagnosis; never a performance acceptance stage."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import traceback

from tools.evalscope import accept_vision_rowmax as a
from tools.evalscope.diagnose_vision_rowmax_env import guard_pid

s = a.s
BASE = a.BASE
FOLDER = BASE / "wait-01"
LIMITS = dict(starts=1, generations=9, controls=16, native=0, seconds=360)


def prepare():
    old = a.pilot.read(a.FOLDER / "plan.json")
    for name, digest in old["files"].items():
        s.require(s.sha(name) == digest, "original dependency changed: " + name)
    FOLDER.mkdir()
    inputs = FOLDER / "inputs"
    inputs.mkdir()
    cases = old["cases"][:3]
    s.require([r["id"] for r in cases] == ["context-1024", "context-4096", "context-8192"],
              "original diagnostic sequence")
    for case in cases:
        source = Path(case["path"])
        target = inputs / source.name
        shutil.copyfile(source, target)
        case["path"] = str(target)
    paths = [s.ROOT / name for name in subprocess.check_output(
        ["git", "ls-files"], cwd=s.ROOT, text=True).splitlines()
        if name.startswith(("src/", "include/", "cmake/", "tools/", "tests/"))
        or name == "CMakeLists.txt"]
    paths += list(inputs.iterdir()) + [s.ROOT / "docs/VISION_ROWMAX_WAIT_PLAN_2026-11-07.md",
        BASE / "env-02/review/analysis.json", BASE / "env-02/originals.json",
        BASE / "logs/wait-stack-capability.json", BASE / "logs/wait-contracts.log",
        s.ROOT / "build/q4t", s.ROOT / "build/q4t.release.json"]
    files = dict(old["files"])
    files.update({str(p): s.sha(p) for p in paths})
    lock = (s.ROOT / ".q4t-work/gpu-exclusive.lock").stat()
    s.save(FOLDER / "plan.json", dict(profile="vision-rowmax-wait-diagnosis-v1",
        limits=LIMITS, observer_processes=1, snapshot_thresholds_seconds=[2, 4],
        max_snapshots=2, max_threads_per_process=64, max_bytes_per_file=8192,
        port=s.PORT, model=old["model"], binaries=old["binaries"], cases=cases,
        files=files, lock_device=lock.st_dev, lock_inode=lock.st_ino,
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=s.ROOT, text=True).strip(), performance_acceptance=False,
        default_unchanged=True, purpose="Read the blocked query's actual wait stack, not allocation-attempt traces"))
    return "PREPARED_WAIT_DIAGNOSIS_ONLY"


class Run(a.Run):
    limits = LIMITS

    def observer_rows(self):
        return [json.loads(line) for line in (self.observer_folder / "stdout.log").read_text().splitlines()]

    def observer_check(self):
        if self.observer.poll() is not None and not self.observer_finished:
            rows = self.observer_rows()
            s.require(self.observer.returncode == 0 and rows[-1]["event"] == "finished"
                      and rows[-1]["reason"] == "capture_budget_complete",
                      "wait observer exited prematurely")
            self.observer_finished = True

    def check(self):
        s.guard_tools.require_monitor(self.monitor, self.failures, self.heartbeat, 10)
        s.require(time.monotonic() - self.started < LIMITS["seconds"] - 60,
                  "wait diagnosis deadline")
        self.observer_check()
        s.require(self.children.running(self.model), "model exited")

    def start_observer(self):
        self.observer_folder = self.output / "wait-observer"
        self.observer_folder.mkdir()
        argv = ["sudo", "-n", "/usr/bin/timeout", "--kill-after=2s", "340s",
                "/usr/bin/python3", "-B", str(s.ROOT / "tools/evalscope/rowmax_wait_snapshot.py"),
                "--guard-pid", str(guard_pid())]
        s.save(self.observer_folder / "command.json", dict(argv=argv,
            scope="read-only procfs observer; no CUDA/NVML calls"))
        with (self.observer_folder / "stdout.log").open("xb") as stdout, (
                self.observer_folder / "stderr.log").open("xb") as stderr:
            self.observer = subprocess.Popen(argv, cwd=s.ROOT, stdout=stdout, stderr=stderr)
        s.save(self.observer_folder / "process.json", dict(pid=self.observer.pid,
            started_monotonic=time.monotonic()))
        deadline = time.monotonic() + 5
        while not (self.observer_folder / "stdout.log").read_text().endswith("\n"):
            s.require(self.observer.poll() is None and time.monotonic() < deadline,
                      "wait observer did not become ready")
            time.sleep(.05)
        rows = self.observer_rows()
        s.require(rows[0]["event"] == "ready", "observer startup receipt")

    def stop_observer(self):
        if self.observer is None:
            return
        (self.observer_folder / "stop").touch(exist_ok=False)
        code = self.observer.wait(timeout=5)
        rows = self.observer_rows()
        s.save(self.observer_folder / "exit.json", dict(returncode=code,
            monotonic=time.monotonic(), events=len(rows)))
        s.require(code == 0 and rows[-1]["event"] == "finished" and
                  rows[-1]["reason"] in ("stop_requested", "capture_budget_complete") and
                  rows[-1]["snapshots"] <= 2, "wait observer completion")

    def run(self):
        s.guard_tools.require_guard()
        s.guard_tools.verify_files(dict(self.plan, model_file_metadata={}))
        self.observer, self.observer_finished = None, False
        self.monitor.start()
        failure, cleanup, rows = None, [], None
        verdict = "FAILED"

        def expired(*_):
            raise TimeoutError("wait diagnosis deadline; cleanup only")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, LIMITS["seconds"] - 60)
        try:
            self.start_observer()
            capacity = self.launch_group("mtp-candidate")
            rows = self.matrix("mtp-candidate", capacity)
            self.retire_model("mtp-candidate")
            verdict = "DIAGNOSTIC_HTTP_COMPLETE_PENDING_REVIEW"
        except BaseException:
            failure = traceback.format_exc()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            try:
                if self.model and self.children.children and self.children.running(self.model):
                    self.children.signal(self.model, signal.SIGTERM)
                s.wait_owned(self.children, cleanup)
                if self.children.children:
                    self.children.reap(self.output)
                for _ in range(2):
                    boundary = self.heartbeat["last_sample"]
                    while self.heartbeat["last_sample"] <= boundary:
                        s.guard_tools.require_monitor(self.monitor, self.failures, self.heartbeat, 10)
                        time.sleep(.05)
                gpu = s.nvml_process_query.query(timeout=5)
                s.save(self.output / "gpu-final.json", gpu)
                s.require(gpu["status"] == "OK" and not gpu["pids"], "ENV_ABORT: final GPU")
            except BaseException:
                cleanup.append(traceback.format_exc())
            finally:
                try:
                    self.stop_observer()
                except BaseException:
                    cleanup.append(traceback.format_exc())
                self.stop_monitor.set()
                self.monitor.join(5)
                self.children.cleanup()
            if self.monitor.is_alive() or self.failures:
                cleanup.append("ENV_ABORT: diagnostic monitor finalization")
            try:
                s.guard_tools.verify_files(dict(self.plan, model_file_metadata={}))
            except BaseException:
                cleanup.append(traceback.format_exc())
            if failure or cleanup:
                verdict = "FAILED"
            s.save(self.output / "result.json", dict(status=verdict, counts=self.counts,
                rows=rows, failure=failure, cleanup_errors=cleanup,
                monitor_failures=self.failures, seconds=time.monotonic()-self.started,
                performance_acceptance=False, default_unchanged=True, root_cause_proven=False))
        s.require(verdict == "DIAGNOSTIC_HTTP_COMPLETE_PENDING_REVIEW", failure or str(cleanup))
        return verdict


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    args = parser.parse_args()
    print(prepare() if args.action == "prepare" else Run(FOLDER).run())
