"""One bounded lease: direct contracts, HF, two text modes and five tiers."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import traceback

from tools.evalscope import run_vision_rowmax as pilot
from tools.evalscope import run_acceptance as http

s = pilot.s
BASE = pilot.BASE
FOLDER = BASE / "accept-01"
LIMITS = dict(starts=4, generations=104, controls=64, native=4, seconds=7200)
GROUPS = ("plain-parent", "plain-candidate", "mtp-candidate", "mtp-parent")
ORIGINAL = s.ROOT / (".q4t-work/mtp-selected-head-rows-20261106/source/"
    ".q4t-work/qsa-softmax-adoption-20261106/http")
VISION = s.ROOT / ".q4t-work/vision-production-20261106"


def prepare():
    audit = pilot.read(BASE / "audit.json")
    s.require(audit["status"] == "PILOT_TARGET_MET_PENDING_CONTRACTS", "pilot target missing")
    for value in audit["binaries"].values():
        s.require(s.sha(value["path"]) == value["sha256"], "tested ELF changed")
    FOLDER.mkdir()
    inputs = FOLDER / "inputs"
    inputs.mkdir()
    original = pilot.read(ORIGINAL / "plan.json")
    cases = original["steps"][0]["cases"]
    for case in cases:
        source = Path(case["path"])
        s.require(s.sha(source) == original["files"][str(source)], "old fixture identity")
        destination = inputs / source.name
        shutil.copyfile(source, destination)
        case["path"] = str(destination)
    paths = [s.ROOT / name for name in subprocess.check_output(
        ["git", "ls-files"], cwd=s.ROOT, text=True).splitlines()
        if name.startswith(("src/", "include/", "cmake/", "tools/", "tests/"))
        or name == "CMakeLists.txt"]
    paths += [BASE / "audit.json", BASE / "originals.json", ORIGINAL / "plan.json",
        BASE / "contracts/vision_attention_contract", BASE / "contracts/CMakeCache.txt",
        BASE / "engine/q4t_vision_tower_contract", BASE / "engine/CMakeCache.txt",
        BASE / "engine/compile_commands.json",
        BASE / "engine/generated/q4t/server/chat_protocol_sources.json",
        s.ROOT / "docs/VISION_ROWMAX_ACCEPTANCE_2026-11-07.md",
        s.ROOT / "build/q4t", s.ROOT / "build/q4t.release.json",
        VISION / "phase-04/plan.json", VISION / "cpu/hf-tower-01/manifest.json"]
    paths += [Path(r["path"]) for r in audit["binaries"].values()]
    paths += list(inputs.iterdir()) + list((BASE / "numerical-input").iterdir())
    paths += [BASE / "logs" / name for name in (
        "configure.log", "build.log", "acceptance-build.log",
        "contracts-configure.log", "contracts-build.log", "host-contracts.log",
        "inventory.log", "inventory-02.log")]
    paths += [VISION / "cpu/hf-tower-01" / (name + ".json")
              for name in ("small", "rectangular")]
    paths += list((VISION / "phase-04/execution").glob("hf-*-output-*.bf16"))
    paths += [s.MODEL / n for n in ("config.json", "tokenizer.json",
        "model.safetensors.index.json", "preprocessor_config.json")]
    package = pilot.EVALSCOPE.parent.parent / "lib/python3.12/site-packages/evalscope"
    paths += sorted(package.rglob("*.py"))
    lock = (s.ROOT / ".q4t-work/gpu-exclusive.lock").stat()
    s.save(FOLDER / "plan.json", dict(profile="vision-rowmax-acceptance-v1",
        limits=LIMITS, groups=GROUPS, cases=cases, quality=original["quality"],
        port=s.PORT, model=str(s.MODEL), binaries=audit["binaries"],
        files={str(p): s.sha(p) for p in paths},
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=s.ROOT, text=True).strip(),
        lock_device=lock.st_dev, lock_inode=lock.st_ino,
        performance_acceptance=False, default_unchanged=True,
        pilot_reuse="64 fixed visual/text requests; buffered SSE decode is not model throughput"))
    return "PREPARED"


class Run(pilot.Run):
    limits = LIMITS

    def check(self):
        s.guard_tools.require_monitor(self.monitor, self.failures, self.heartbeat, 10)
        s.require(time.monotonic() - self.started < LIMITS["seconds"] - 45,
                  "acceptance deadline")
        s.require(self.children.running(self.model), "model exited during HTTP")

    def clean_env(self):
        return {k: v for k, v in os.environ.items() if not k.startswith("Q4T_")
                and k not in ("LD_PRELOAD", "LD_LIBRARY_PATH")}

    def contracts(self):
        commands = [("kernel", [str(BASE / "contracts/vision_attention_contract")])]
        for name in ("small", "rectangular"):
            commands.append(("hf-" + name, [str(BASE / "engine/q4t_vision_tower_contract"),
                str(VISION / "cpu/hf-tower-01" / (name + ".json")),
                str(self.output / ("hf-" + name + "-output"))]))
        for name, argv in commands:
            self.budget("native")
            folder = self.output / name
            process = self.children.launch(folder, argv, self.clean_env())
            self.children.wait(process, folder, 180)
            custody = self.output / (name + "-custody")
            custody.mkdir()
            self.children.reap(custody)
            self.children = s.Children(self.folder / "lease")
            if name == "kernel":
                rows = [json.loads(x) for x in (folder / "stdout.log").read_text().splitlines()]
                s.require(rows[-1]["passed"] and rows[-1]["cases"] == 26,
                          "kernel numerical coverage")
            else:
                for case in ("image", "video"):
                    file = name + "-output-" + case + ".bf16"
                    s.require((self.output / file).read_bytes() ==
                              (VISION / "phase-04/execution" / file).read_bytes(),
                              "full tower changed BF16 output: " + file)
            print("direct contract complete: " + name, flush=True)

    def launch_group(self, group):
        self.budget("starts")
        mode, arm = group.split("-")
        folder = self.output / ("model-" + group)
        argv = [self.plan["binaries"][arm]["path"], "serve",
            "--model-dir", self.plan["model"], "--host", "127.0.0.1",
            "--port", str(s.PORT), "--max-seq", "1", "--max-prefill", "8192",
            "--max-len", "208896", "--max-tokens", "256"]
        if mode == "plain":
            argv.append("--no-mtp")
        self.env = self.clean_env()
        s.save(self.output / ("env-" + group + ".json"),
            {k: v for k, v in self.env.items() if k.startswith(("Q4T_", "CUDA_", "OMP_"))
             or k in ("PATH", "LD_PRELOAD", "LD_LIBRARY_PATH")})
        self.model = self.children.launch(folder, argv, self.env)
        deadline = time.monotonic() + 180
        while "serving on port" not in (folder / "stderr.log").read_text():
            self.check()
            s.require(time.monotonic() < deadline, "startup timeout")
            time.sleep(.1)
        capacity = http.capacity_evidence((folder / "stderr.log").read_text(), 208896)
        s.save(folder / "capacity.json", capacity)
        s.require(capacity["matches_requested"], "effective capacity differs")
        return capacity

    def matrix(self, group, capacity):
        folder = self.output / ("client-" + group)
        folder.mkdir()
        results = {}
        for case in self.plan["cases"]:
            for _ in range(case["count"]):
                self.budget("generations")
            directory = folder / case["id"]
            directory.mkdir()
            inputs = Path(case["path"])
            shutil.copyfile(inputs, directory / "requests.jsonl")
            quality = case["id"] == "quality"
            rows = http.run_case(folder, directory, inputs, case["count"],
                case["min"], case["max"], case["tokens"], True, pilot.EVALSCOPE,
                s.MODEL, s.PORT, self.env, self.model, False, capacity,
                "quality" if quality else "performance", process_check=self.check)
            if quality:
                s.require({r["prompt_sha256"] for r in rows} == set(self.plan["quality"]),
                          "quality coverage")
                for row in rows:
                    expected = self.plan["quality"][row["prompt_sha256"]]
                    s.require(row["text"].strip() == expected["expected"] and
                              row["actual_input"] == expected["length"] and
                              row["finish"] == ["stop"], "fixed quality failure")
            results[case["id"]] = rows
            print(group + ": " + case["id"] + " complete", flush=True)
        caps = json.loads(self.http(group + "-caps", "/v1/capabilities"))
        s.require(not caps["runtime"]["media_enabled"] and
                  caps["runtime"]["mtp_enabled"] == group.startswith("mtp-"),
                  "wrong text mode")
        self.idle(group + "-idle")
        return results

    def run(self):
        s.guard_tools.require_guard()
        s.guard_tools.verify_files(dict(self.plan, model_file_metadata={}))
        s.require(self.plan["groups"] == list(GROUPS) and
                  sum(c["count"] for c in self.plan["cases"]) == 26, "frozen matrix")
        self.monitor.start()
        failure, cleanup, matrices = None, [], {}
        verdict = "FAILED"

        def expired(*_):
            raise TimeoutError("frozen acceptance deadline; cleanup only")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, LIMITS["seconds"] - 45)
        try:
            self.contracts()
            for group in GROUPS:
                capacity = self.launch_group(group)
                matrices[group] = self.matrix(group, capacity)
                self.retire_model(group)
            keys = ("prompt_sha256", "actual_input", "actual_output", "text", "finish")
            for mode in ("plain", "mtp"):
                for case in self.plan["cases"]:
                    old = matrices[mode + "-parent"][case["id"]]
                    new = matrices[mode + "-candidate"][case["id"]]
                    s.require([[r[k] for k in keys] for r in old] ==
                              [[r[k] for k in keys] for r in new],
                              "same-mode parent/candidate response differs")
            verdict = "CONTRACTS_AND_FULL_MATRIX_COLLECTED_PENDING_REVIEW"
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
                seconds=time.monotonic() - self.started, matrices=matrices,
                failure=failure, cleanup_errors=cleanup, monitor_failures=self.failures,
                default_unchanged=True, performance_acceptance=False))
        s.require(verdict == "CONTRACTS_AND_FULL_MATRIX_COLLECTED_PENDING_REVIEW",
                  failure or str(cleanup))
        return verdict


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    args = parser.parse_args()
    print(prepare() if args.action == "prepare" else Run(FOLDER).run())
