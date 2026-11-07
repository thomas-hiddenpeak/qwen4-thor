"""Frozen ABBA evalscope HTTP pilot; no native tests or automatic promotion.

Run with python -B -m tools.evalscope.run_vision_rowmax under gpu_exclusive.
Only deserialize databases produced locally by this exact invocation.
"""
import argparse
import base64
import copy
import json
import math
import os
from pathlib import Path
import pickle
import secrets
import signal
import sqlite3
import subprocess
import time
import traceback

from tools.vision_service import stage as s
from tools.vision_service.timing_stage import Run as TimingRun
from tools.evalscope.run_acceptance import run_checked_client

BASE = s.ROOT / ".q4t-work/vision-attention-rowmax-20261107"
FOLDER = BASE / "pilot-01"
PARENT = s.ROOT / ".q4t-work/vision-forward-timing-20261107/engine/q4t"
PARENT_SHA = "982bba071e68e807cda653fa32ab76bed692808aa2d9325876ac09ea9f9daa1d"
LIMITS = dict(starts=4, generations=64, controls=32, native=0, seconds=1200)
GROUPS = ("parent-0", "candidate-0", "candidate-1", "parent-1")
CASES = ("earth-python", "cabin-python", "multiple-real-photos",
         "motion-forward", "text-python")
EVALSCOPE = s.ROOT / "tools/evalscope/.venv/bin/evalscope"


def read(path):
    return json.loads(Path(path).read_text())


def prepare():
    s.require(s.sha(PARENT) == PARENT_SHA, "parent identity changed")
    FOLDER.mkdir()
    prior = s.ROOT / ".q4t-work/vision-forward-timing-20261107/phase-01/requests.json"
    originals = {r["case"]: r for r in read(prior) if r["mode"] == "off"}
    rows = []
    for group in GROUPS:
        selected = []
        for index, case in enumerate((CASES[0], *CASES, *CASES, *CASES)):
            row = copy.deepcopy(originals[case])
            row.update(group=group, label=f"{group}-{index:02d}-{case}",
                       first_request=index == 0)
            row.pop("mode", None)
            row["request"].update(request_id="vr-" + secrets.token_hex(12),
                cancel_token=secrets.token_hex(32), stream=True,
                stream_options=dict(include_usage=True))
            rows.append(row)
            selected.append(json.dumps(row["request"], ensure_ascii=False))
        (FOLDER / (group + ".jsonl")).write_text("\n".join(selected) + "\n")
    s.save(FOLDER / "requests.json", rows)
    paths = [s.ROOT / n for n in subprocess.check_output(
        ["git", "ls-files"], cwd=s.ROOT, text=True).splitlines()
        if n.startswith(("src/", "include/", "cmake/", "tools/", "tests/"))
        or n == "CMakeLists.txt"]
    engine = BASE / "engine"
    paths += [PARENT, engine / "q4t", engine / "CMakeCache.txt",
        engine / "compile_commands.json",
        engine / "generated/q4t/server/chat_protocol_sources.json",
        PARENT.parent / "generated/q4t/server/chat_protocol_sources.json",
        s.ROOT / "build/q4t", s.ROOT / "build/q4t.release.json", prior,
        s.ROOT / "docs/VISION_ROWMAX_PLAN_2026-11-07.md", EVALSCOPE]
    paths += [BASE / "logs/configure.log", BASE / "logs/build.log"]
    paths += list(FOLDER.iterdir())
    paths += [s.MODEL / n for n in ("config.json", "tokenizer.json",
        "preprocessor_config.json", "video_preprocessor_config.json")]
    package = EVALSCOPE.parent.parent / "lib/python3.12/site-packages/evalscope"
    paths += sorted(package.rglob("*.py"))
    lock = (s.ROOT / ".q4t-work/gpu-exclusive.lock").stat()
    s.save(FOLDER / "plan.json", dict(profile="vision-rowmax-http-pilot-v1",
        limits=LIMITS, port=s.PORT, model=str(s.MODEL), groups=GROUPS,
        binaries=dict(parent=str(PARENT), candidate=str(engine / "q4t")),
        files={str(p): s.sha(p) for p in paths},
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=s.ROOT, text=True).strip(),
        lock_device=lock.st_dev, lock_inode=lock.st_ino,
        target=dict(cases=list(CASES[1:3]), minimum_latency_reduction=.03,
                    pairs=[["parent-0", "candidate-0"],
                           ["parent-1", "candidate-1"]]),
        statistical_noninferiority=False, performance_acceptance=False,
        default_unchanged=True))
    return dict(status="PREPARED", requests=len(rows))


def parse_database(folder, expected):
    databases = list(folder.rglob("benchmark_data.db"))
    s.require(len(databases) == 1, "missing/duplicate local evalscope database")
    with sqlite3.connect("file:" + str(databases[0]) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("select * from result order by start_time").fetchall()
    s.require(len(rows) == len(expected), "actual HTTP count differs")
    parsed = []
    for row, frozen in zip(rows, expected):
        body = json.loads(row["request"])
        s.require(body == frozen["request"], "wire body/order differs from plan")
        chunks = pickle.loads(base64.b64decode(row["response_messages"]))
        s.require(chunks and all(isinstance(c, dict) and "error" not in c
                                for c in chunks), "error/empty response")
        s.require(all(c["id"] == body["request_id"] and c["model"] == body["model"]
                      for c in chunks), "response identity")
        choices = [c for chunk in chunks for c in chunk.get("choices", [])]
        s.require(all(c["index"] == 0 for c in choices), "choice index")
        deltas = [c["delta"] for c in choices]
        s.require(all(set(d) <= {"role", "content", "reasoning_content"}
                      and not d.get("reasoning_content") for d in deltas),
                  "unexpected reasoning/tool output")
        content = "".join(d.get("content") or "" for d in deltas)
        finish = [c["finish_reason"] for c in choices if c.get("finish_reason")]
        actual = json.loads(content)
        s.Draft202012Validator(body["response_format"]["json_schema"]["schema"]).validate(actual)
        s.require(row["success"] and finish == ["stop"] and
                  actual == frozen["expected"], "HTTP quality/terminal failure")
        latency, ttft = row["latency"], row["first_chunk_latency"]
        prompt, completion = row["prompt_tokens"], row["completion_tokens"]
        s.require(all(math.isfinite(x) for x in (latency, ttft)) and
                  0 < ttft < latency and prompt > 0 and completion > 1 and
                  completion < body["max_tokens"], "HTTP metrics/usage invalid")
        parsed.append(dict(label=frozen["label"], case=frozen["case"],
            group=frozen["group"], first_request=frozen["first_request"],
            content=content, finish=finish, prompt_tokens=prompt,
            completion_tokens=completion, latency=latency, ttft=ttft,
            decode_tokens=completion - 1, decode_seconds=latency - ttft,
            start_time=row["start_time"], completed_time=row["completed_time"]))
    return parsed


class Run(s.Run):
    limits = LIMITS
    retire_model = TimingRun.retire_model

    def check(self):
        s.guard_tools.require_monitor(self.monitor, self.failures, self.heartbeat, 10)
        s.require(time.monotonic() - self.started < LIMITS["seconds"] - 45,
                  "pilot deadline")
        s.require(self.children.running(self.model), "model exited during HTTP")

    def launch_group(self, group):
        self.budget("starts")
        folder = self.output / ("model-" + group)
        binary = self.plan["binaries"][group.split("-")[0]]
        argv = [binary, "serve", "--model-dir", self.plan["model"],
            "--host", "127.0.0.1", "--port", str(s.PORT), "--allow-media",
            "--no-mtp", "--max-seq", "1", "--max-prefill", "8192",
            "--max-len", "16384", "--max-inflight-requests", "8",
            "--max-queued-requests", "4"]
        self.env = {k: v for k, v in os.environ.items()
            if not k.startswith("Q4T_") and k not in ("LD_PRELOAD", "LD_LIBRARY_PATH")}
        s.save(self.output / ("env-" + group + ".json"),
            {k: v for k, v in self.env.items() if k.startswith(("Q4T_", "CUDA_", "OMP_"))
             or k in ("PATH", "LD_PRELOAD", "LD_LIBRARY_PATH")})
        self.model = self.children.launch(folder, argv, self.env)
        deadline = time.monotonic() + 180
        while "serving on port" not in (folder / "stderr.log").read_text():
            self.check()
            s.require(time.monotonic() < deadline, "startup timeout")
            time.sleep(.1)

    def collect_group(self, group, selected):
        folder = self.output / ("client-" + group)
        folder.mkdir()
        for _ in selected:
            self.budget("generations")  # Reserve attempted requests before client launch.
        command = [str(EVALSCOPE), "perf", "--model", "qwen3.8-flash-next",
            "--url", f"http://127.0.0.1:{s.PORT}/v1/chat/completions",
            "--api", "openai", "--tokenizer-path", str(s.MODEL),
            "--dataset", "line_by_line", "--dataset-path",
            str(self.folder / (group + ".jsonl")), "--no-apply-chat-template",
            "--parallel", "1", "--number", str(len(selected)), "--warmup-num", "0",
            "--no-test-connection", "--connect-timeout", "10",
            "--read-timeout", "130", "--total-timeout", "150", "--stream",
            "--outputs-dir", str(folder)]
        s.save(folder / "command.json", command)
        with (folder / "client.log").open("x") as log:
            code = run_checked_client(command, self.env, log, self.check,
                                      folder / "client-process.json")
        s.require(code == 0, "evalscope failed")
        parsed = parse_database(folder, selected)
        s.save(folder / "parsed.json", parsed)
        caps = json.loads(self.http(group + "-caps", "/v1/capabilities"))
        s.require(caps["runtime"]["media_enabled"] and
                  not caps["runtime"]["mtp_enabled"], "media mode differs")
        self.idle(group + "-idle")
        return parsed

    def run(self):
        s.guard_tools.require_guard()
        s.guard_tools.verify_files(dict(self.plan, model_file_metadata={}))
        rows = read(self.folder / "requests.json")
        s.require(len(rows) == 64 and self.plan["groups"] == list(GROUPS), "pilot shape")
        self.monitor.start()
        failure, cleanup, all_rows = None, [], []
        verdict = "FAILED"

        def expired(*_):
            raise TimeoutError("frozen pilot deadline; cleanup only")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, LIMITS["seconds"] - 45)
        try:
            for group in GROUPS:
                self.launch_group(group)
                selected = [r for r in rows if r["group"] == group]
                all_rows += self.collect_group(group, selected)
                self.retire_model(group)
                log = (self.output / ("model-" + group) / "stderr.log").read_text()
                s.require("[q4t][vision-timing]" not in log and
                          "[q4t][vision-forward-timing]" not in log,
                          "performance run instrumented")
                print(group + ": 16 evalscope requests and owned shutdown complete", flush=True)
            signatures = {}
            for row in all_rows:
                signature = (row["content"], row["finish"],
                             row["prompt_tokens"], row["completion_tokens"])
                signatures.setdefault(row["case"], signature)
                s.require(signatures[row["case"]] == signature,
                          "repetition/parent-candidate output differs")
            verdict = "HTTP_PILOT_COLLECTED_PENDING_REVIEW"
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
                seconds=time.monotonic() - self.started, rows=all_rows,
                failure=failure, cleanup_errors=cleanup, monitor_failures=self.failures,
                note="Counts reserve attempted requests; actual rows are in local databases. "
                     "Evalscope retains content chunks and usage columns, not literal DONE bytes."))
        s.require(verdict == "HTTP_PILOT_COLLECTED_PENDING_REVIEW", failure or str(cleanup))
        return verdict


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    args = parser.parse_args()
    print(prepare() if args.action == "prepare" else Run(FOLDER).run())
