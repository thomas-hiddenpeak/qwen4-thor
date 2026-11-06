"""Frozen 2-model/12-Chat/600-second diagnosis; no retries or optimization."""
import argparse
import copy
import os
import secrets
import subprocess

from tools.vision_service import timing_stage as t
from tools.vision_service import timing_analysis as wall
from tools.vision_service import forward_timing_analysis as forward

s = t.s
BASE = s.ROOT / ".q4t-work/vision-forward-timing-20261107"
FOLDER = BASE / "phase-01"
LIMITS = dict(starts=2, generations=12, controls=32, native=0, seconds=600)
CASES = ("earth-python", "cabin-python", "multiple-real-photos",
         "motion-forward", "text-python", "earth-python")
PATCHES = dict(zip(CASES, (1024, 2640, 3664, 1536, 0, 1024)))


def prepare():
    FOLDER.mkdir()
    prior = t.FOLDER / "requests.json"
    old = {r["case"]: r for r in t.read(prior) if r["mode"] == "off"}
    rows = []
    for mode in ("off", "on"):
        for index, case in enumerate(CASES):
            row = copy.deepcopy(old[case])
            row.update(mode=mode, label=f"{mode}-{index:02d}-{case}",
                       patches=PATCHES[case])
            row["request"].update(request_id="vf-" + secrets.token_hex(12),
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
              FOLDER / "requests.json", prior,
              s.ROOT / "docs/VISION_FORWARD_TIMING_PLAN_2026-11-07.md",
              s.ROOT / "build/q4t", s.ROOT / "build/q4t.release.json"]
    paths += sorted((BASE / "logs").glob("cpu-*.log"))
    paths += [s.MODEL / name for name in ("config.json", "tokenizer.json",
              "preprocessor_config.json", "video_preprocessor_config.json")]
    lock_stat = (s.ROOT / ".q4t-work/gpu-exclusive.lock").stat()
    s.save(FOLDER / "plan.json", dict(profile="q4t-vision-forward-timing-v1",
        limits=LIMITS, port=s.PORT, binary=str(BASE / "engine/q4t"),
        model=str(s.MODEL), files={str(p): s.sha(p) for p in paths},
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=s.ROOT, text=True).strip(), planned_requests=len(rows),
        depth=t.read(s.MODEL / "config.json")["vision_config"]["depth"],
        lock_device=lock_stat.st_dev, lock_inode=lock_stat.st_ino,
        performance_acceptance=False, default_unchanged=True))
    return "PREPARED"


class Run(t.Run):
    limits = LIMITS
    planned_requests = 12
    success_status = "PASS_BOUNDED_VISION_FORWARD_TIMING"

    def timing_env(self, mode):
        env = {"Q4T_VISION_TIMING": "1"}
        if mode == "on": env["Q4T_VISION_FORWARD_TIMING"] = "1"
        return env

    def validate_timings(self, mode, selected):
        log = (self.output / ("model-" + mode) / "stderr.log").read_text()
        outer, inner = wall.parse_records(log), forward.parse_records(log)
        media = [r for r in selected if r["patches"]]
        expected = {r["request"]["request_id"] for r in media}
        s.require(set(outer) == expected, "wall record coverage")
        s.require(set(inner) == (expected if mode == "on" else set()),
                  "forward record coverage")
        for row in media:
            body = row["request"]
            w = outer[body["request_id"]]
            wall.validate_request(w, body)
            s.require(w["patches"] == row["patches"], "frozen geometry differs")
            if mode == "on":
                forward.validate_request(inner[body["request_id"]], body, w,
                                          self.plan["depth"], row["patches"])


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    args = parser.parse_args()
    print(prepare() if args.action == "prepare" else Run(FOLDER).run())
