"""One diagnostic lease; captured timings are never performance acceptance."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import threading
import time
import traceback

from tools.evalscope import accept_vision_rowmax as a

s = a.s
BASE = a.BASE
FOLDER = BASE / "env-02"
LIMITS = dict(starts=1, generations=9, controls=16, native=0, seconds=596)


def prepare():
    old = a.pilot.read(a.FOLDER / "plan.json")
    candidate = old["binaries"]["candidate"]
    s.require(s.sha(candidate["path"]) == candidate["sha256"], "candidate changed")
    previous = a.pilot.read(BASE / "env-01/execution/result.json")
    s.require(previous["status"] == "FAILED" and previous["counts"] ==
              dict(starts=0, generations=0, controls=0, native=0), "previous diagnostic budget")
    events = [json.loads(r) for r in (BASE / "env-01/lease/events.jsonl").read_text().splitlines()]
    previous_seconds = events[-1]["monotonic"] - events[0]["monotonic"]
    s.require(previous_seconds + LIMITS["seconds"] <= 600, "aggregate diagnosis time budget")
    FOLDER.mkdir()
    inputs = FOLDER / "inputs"
    inputs.mkdir()
    trace_format = inputs / "mm_page_alloc.format"
    trace_format.write_bytes(subprocess.check_output(["sudo", "-n", "cat",
        "/sys/kernel/tracing/events/kmem/mm_page_alloc/format"], timeout=5))
    (inputs / "installed-driver-version.txt").write_bytes(Path("/proc/driver/nvidia/version").read_bytes())
    cases = old["cases"][:3]
    s.require([r["id"] for r in cases] == ["context-1024", "context-4096", "context-8192"],
              "original request order")
    for case in cases:
        source = Path(case["path"])
        s.require(s.sha(source) == old["files"][str(source)], "original input changed")
        target = inputs / source.name
        shutil.copyfile(source, target)
        case["path"] = str(target)
    paths = [s.ROOT / name for name in subprocess.check_output(
        ["git", "ls-files"], cwd=s.ROOT, text=True).splitlines()
        if name.startswith(("src/", "include/", "cmake/", "tools/", "tests/"))
        or name == "CMakeLists.txt"]
    paths += list(inputs.iterdir()) + [Path(candidate["path"]), a.FOLDER / "plan.json",
        s.ROOT / "build/q4t", s.ROOT / "build/q4t.release.json",
        s.ROOT / "docs/VISION_ROWMAX_ENV_DIAGNOSIS_2026-11-07.md",
        BASE / "logs/trace-contracts.log", BASE / "logs/trace-contracts-02.log",
        BASE / "logs/trace-contracts-03.log", BASE / "env-01/plan.json",
        BASE / "env-01/lease/events.jsonl", BASE / "env-01/lease/exit.json",
        BASE / "env-01/execution/result.json", BASE / "env-01/originals.json"]
    paths += [s.MODEL / n for n in ("config.json", "tokenizer.json",
        "model.safetensors.index.json", "preprocessor_config.json")]
    lock = (s.ROOT / ".q4t-work/gpu-exclusive.lock").stat()
    for name, digest in old["files"].items():
        s.require(s.sha(name) == digest, "prior frozen dependency changed: " + name)
    files = dict(old["files"])
    files.update({str(p): s.sha(p) for p in paths})
    s.save(FOLDER / "plan.json", dict(profile="vision-rowmax-allocator-diagnosis-v1",
        limits=LIMITS, trace_nonce=secrets.token_hex(8), trace_helper_calls=2,
        prior_attempt_seconds=previous_seconds, aggregate_seconds_limit=600,
        trace_format_sha256=s.sha(trace_format),
        port=s.PORT, model=str(s.MODEL), binaries=old["binaries"], cases=cases,
        files=files, lock_device=lock.st_dev, lock_inode=lock.st_ino,
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=s.ROOT,
            text=True).strip(), performance_acceptance=False, default_unchanged=True,
        purpose="Distinguish allocation paths using failure-only process-scoped kernel traces"))
    return "PREPARED_DIAGNOSTIC_ONLY"


def guard_pid():
    pid = os.getppid()
    for _ in range(64):
        proc = Path("/proc") / str(pid)
        argv = [x.decode() for x in (proc / "cmdline").read_bytes().split(b"\0") if x]
        if str(s.ROOT / "tools/evalscope/gpu_exclusive.py") in argv:
            return pid
        pid = int((proc / "stat").read_text().rsplit(")", 1)[1].split()[1])
        if pid <= 1:
            break
    raise RuntimeError("absolute-path guard ancestor required")


class Run(a.Run):
    limits = LIMITS

    def check(self):
        s.guard_tools.require_monitor(self.monitor, self.failures, self.heartbeat, 10)
        s.require(time.monotonic() - self.started < LIMITS["seconds"] - 60,
                  "diagnostic deadline")
        s.require(self.allocator_sampler.is_alive(), "allocator sampler stopped")
        s.require(self.children.running(self.model), "model exited")

    def allocator_samples(self):
        try:
            with (self.output / "allocator-memory.jsonl").open("x") as stream:
                while not self.stop_monitor.is_set():
                    row = dict(monotonic=time.monotonic(), unix_seconds=time.time(),
                        buddyinfo=Path("/proc/buddyinfo").read_text(),
                        vmstat=Path("/proc/vmstat").read_text())
                    stream.write(json.dumps(row) + "\n")
                    stream.flush()
                    self.stop_monitor.wait(1)
        except BaseException:
            self.failures.append(traceback.format_exc())

    def trace(self, action):
        s.require(self.trace_calls < 2, "trace helper call budget")
        self.trace_calls += 1
        folder = self.output / ("trace-" + action)
        folder.mkdir()
        argv = ["sudo", "-n", "/usr/bin/timeout", "--kill-after=1s", "8s",
            "/usr/bin/python3", "-B", str(s.ROOT / "tools/evalscope/rowmax_alloc_trace.py"),
            action, "--guard-pid", str(self.owner_pid)]
        s.save(folder / "command.json", dict(argv=argv, purpose="CPU tracefs control only"))
        with (folder / "stdout.log").open("wb") as stdout, (folder / "stderr.log").open("wb") as stderr:
            value = subprocess.run(argv, stdout=stdout, stderr=stderr, cwd=s.ROOT, timeout=12)
        s.save(folder / "exit.json", dict(returncode=value.returncode, monotonic=time.monotonic()))
        s.require(value.returncode == 0, "trace helper failed: " + action)
        if action == "collect":
            receipt = a.pilot.read(self.output / "allocator-trace/collected.json")
            s.require(receipt["global_unchanged"], "global tracing changed during diagnosis")

    def run(self):
        s.guard_tools.require_guard()
        s.guard_tools.verify_files(dict(self.plan, model_file_metadata={}))
        self.owner_pid = guard_pid()
        self.trace_calls = 0
        (self.output / "allocator-trace").mkdir()
        self.allocator_sampler = threading.Thread(target=self.allocator_samples, daemon=True)
        self.monitor.start()
        self.allocator_sampler.start()
        failure, cleanup, rows = None, [], None
        verdict = "FAILED"

        def expired(*_):
            raise TimeoutError("diagnostic deadline; cleanup only")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, LIMITS["seconds"] - 60)
        try:
            self.trace("setup")
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
                # Trace retirement must run even when the monitor/custody fails.
                if (self.output / "allocator-trace/owner.json").exists():
                    try:
                        self.trace("collect")
                    except BaseException:
                        cleanup.append(traceback.format_exc())
                self.stop_monitor.set()
                self.monitor.join(5)
                self.allocator_sampler.join(5)
                self.children.cleanup()
            if self.monitor.is_alive() or self.allocator_sampler.is_alive() or self.failures:
                cleanup.append("ENV_ABORT: diagnostic sampler finalization")
            try:
                s.guard_tools.verify_files(dict(self.plan, model_file_metadata={}))
            except BaseException:
                cleanup.append(traceback.format_exc())
            if failure or cleanup:
                verdict = "FAILED"
            s.save(self.output / "result.json", dict(status=verdict, counts=self.counts,
                trace_helper_calls=self.trace_calls, rows=rows, failure=failure,
                cleanup_errors=cleanup, monitor_failures=self.failures,
                seconds=time.monotonic()-self.started, performance_acceptance=False,
                default_unchanged=True, root_cause_proven=False))
        s.require(verdict == "DIAGNOSTIC_HTTP_COMPLETE_PENDING_REVIEW", failure or str(cleanup))
        return verdict


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    args = parser.parse_args()
    print(prepare() if args.action == "prepare" else Run(FOLDER).run())
