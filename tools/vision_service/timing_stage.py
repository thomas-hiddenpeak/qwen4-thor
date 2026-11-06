"""One bounded, exclusive two-mode HTTP run; no retries or optimization."""
import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time
import traceback

from tools.vision_service import stage as s
from tools.vision_service import timing_analysis as a

BASE = s.ROOT / ".q4t-work/vision-timing-20261107"
FOLDER = BASE / "phase-01"
LIMITS = dict(starts=2, generations=22, controls=40, native=0, seconds=900)


def read(path):
    return json.loads(Path(path).read_text())


def prepare():
    s.require(read(BASE / "cpu/result.json")["passed"], "CPU contracts failed")
    FOLDER.mkdir()
    prior = s.ROOT / ".q4t-work/vision-client-20261107/phase-01"
    old = {r["label"]: r for r in read(prior / "requests.json")}
    pair = copy.deepcopy(old["earth-python"])
    pair["label"] = "earth-same-pixels-png"
    pair["request"]["messages"][0]["content"][0] = s.image_part(
        BASE / "cpu/earth-same-pixels.png")
    cases = [old["earth-python"], old["cabin-python"], pair,
             old["multiple-real-photos"], old["real-record-chart"],
             old["motion-forward"], old["text-python"], old["earth-python"]]
    rows = []
    for mode in ("off", "on"):
        selected = cases + ([old["cabin-python"], old["earth-python"]]
                            if mode == "on" else [])
        for index, original in enumerate(selected):
            row = copy.deepcopy(original)
            row.update(mode=mode, case=original["label"],
                       label=f"{mode}-{index:02d}-{original['label']}")
            row["request"].update(request_id="vt-" + secrets.token_hex(12),
                                   cancel_token=secrets.token_hex(32))
            rows.append(row)
    s.save(FOLDER / "requests.json", rows)
    paths = [s.ROOT / name for name in subprocess.check_output(
        ["git", "ls-files"], cwd=s.ROOT, text=True).splitlines()
        if name.startswith(("src/", "include/", "cmake/", "tools/", "tests/")) or
        name == "CMakeLists.txt"]
    paths += [BASE / "engine/q4t", BASE / "engine/CMakeCache.txt",
              BASE / "engine/compile_commands.json",
              BASE / "engine/generated/q4t/server/chat_protocol_sources.json",
              FOLDER / "requests.json", prior / "requests.json",
              s.ROOT / "docs/VISION_TIMING_PLAN_2026-11-07.md",
              s.ROOT / "build/q4t", s.ROOT / "build/q4t.release.json"]
    paths += [p for p in (BASE / "cpu").iterdir() if p.is_file()]
    paths += [s.MODEL / name for name in ("config.json", "tokenizer.json",
              "preprocessor_config.json", "video_preprocessor_config.json")]
    s.save(FOLDER / "plan.json", dict(profile="q4t-vision-wall-timing-v1",
        limits=LIMITS, port=s.PORT, binary=str(BASE / "engine/q4t"),
        model=str(s.MODEL), files={str(p): s.sha(p) for p in paths},
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=s.ROOT, text=True).strip(), planned_requests=len(rows),
        performance_acceptance=False, default_unchanged=True))


class Run(s.Run):
    limits = LIMITS
    success_status = "PASS_BOUNDED_VISION_TIMING"

    def retire_model(self, mode):
        self.children.signal(self.model, signal.SIGTERM)
        self.children.wait(self.model, self.output / ("model-" + mode), 30)
        custody = self.output / (mode + "-custody")
        custody.mkdir()
        self.children.reap(custody)
        # Children retains its registration history after reaping. Each model
        # lifetime needs a fresh ledger; the enclosing GPU lease stays held.
        self.children = s.Children(self.folder / "lease")
        self.model = None

    def launch_mode(self, mode):
        self.budget("starts")
        folder = self.output / ("model-" + mode)
        argv = [self.plan["binary"], "serve", "--model-dir", self.plan["model"],
                "--host", "127.0.0.1", "--port", str(self.plan["port"]),
                "--allow-media", "--no-mtp", "--max-seq", "1",
                "--max-prefill", "8192", "--max-len", "16384",
                "--max-inflight-requests", "8", "--max-queued-requests", "4"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("Q4T_")
               and k not in ("LD_PRELOAD", "LD_LIBRARY_PATH")}
        if mode == "on":
            env["Q4T_VISION_TIMING"] = "1"
        s.save(self.output / ("env-" + mode + ".json"), {k: v for k, v in env.items()
            if k.startswith(("Q4T_", "CUDA_", "OMP_")) or k in
            ("PATH", "LD_LIBRARY_PATH", "LD_PRELOAD")})
        self.model = self.children.launch(folder, argv, env)
        deadline = time.monotonic() + 180
        while "serving on port" not in (folder / "stderr.log").read_text():
            s.require(self.children.running(self.model) and
                      time.monotonic() < deadline, "startup failed")
            time.sleep(.1)
        caps = json.loads(self.http(mode + "-caps", "/v1/capabilities"))
        s.require(caps["runtime"]["media_enabled"] and
                  not caps["runtime"]["mtp_enabled"], "wrong media profile")

    def run(self):
        s.guard_tools.require_guard()
        s.guard_tools.verify_files(dict(self.plan, model_file_metadata={}))
        rows = read(self.folder / "requests.json")
        s.require(len(rows) == self.plan["planned_requests"] == 18,
                  "frozen request count")
        self.monitor.start()
        failure, cleanup = None, []
        verdict = "FAILED"

        def expired(*_):
            raise TimeoutError("frozen phase deadline; cleanup only")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, self.plan["limits"]["seconds"] - 45)
        try:
            off = []
            for mode in ("off", "on"):
                self.launch_mode(mode)
                selected = [r for r in rows if r["mode"] == mode]
                for index, row in enumerate(selected):
                    label, body = row["label"], row["request"]
                    pre = json.loads(self.http(label + "-preflight",
                        "/v1/chat/preflight", dict(request=body)))
                    value = self.complete(label, body)
                    actual = json.loads(value.message["content"])
                    s.Draft202012Validator(body["response_format"]["json_schema"]
                                          ["schema"]).validate(actual)
                    same = (value.message, value.finish_reason, value.usage)
                    self.observe(label, value.finish_reason == "stop" and
                        actual == row["expected"] and
                        value.usage["prompt_tokens"] == pre["prompt_tokens"] and
                        (mode == "off" or index >= len(off) or same == off[index]),
                        actual=actual, expected=row["expected"], usage=value.usage)
                    if mode == "off":
                        off.append(same)
                self.idle(mode + "-idle")
                self.retire_model(mode)
                records = a.parse_records((self.output /
                    ("model-" + mode) / "stderr.log").read_text())
                media = [r for r in selected if a.input_counts(r["request"])["items"]]
                if mode == "off":
                    s.require(not records, "timing default is not silent")
                else:
                    s.require(set(records) == {r["request"]["request_id"] for r in media},
                              "missing/unexpected timing records")
                    for row in media:
                        a.validate_request(records[row["request"]["request_id"]], row["request"])
            s.require(all(r["passed"] for r in self.observations), "HTTP contract/quality failure")
            verdict = self.success_status
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
                s.require(gpu["status"] == "OK" and not gpu["pids"], "ENV_ABORT: final GPU query")
            except BaseException:
                cleanup.append(traceback.format_exc())
            finally:
                self.stop_monitor.set()
                self.monitor.join(5)
                self.children.cleanup()
            if self.monitor.is_alive() or self.failures:
                cleanup.append("ENV_ABORT: final monitor")
            try:
                s.guard_tools.verify_files(dict(self.plan, model_file_metadata={}))
            except BaseException:
                cleanup.append(traceback.format_exc())
            if failure or cleanup:
                verdict = "FAILED"
            s.save(self.output / "result.json", dict(status=verdict, counts=self.counts,
                seconds=time.monotonic() - self.started, observations=self.observations,
                failure=failure, cleanup_errors=cleanup, monitor_failures=self.failures))
        s.require(verdict == self.success_status, failure or str(cleanup))
        return verdict


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    args = parser.parse_args()
    print(prepare() if args.action == "prepare" else Run(FOLDER).run())
