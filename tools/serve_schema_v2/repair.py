"""Complete the failed final JSON and three unexecuted A05 text requests."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_schema_v2 import acceptance as base
from tools.serve_service.package import read, save, sha, require

LIMITS = dict(starts=1, chats=4, controls=2000, seconds=700)
CUMULATIVE = dict(starts=2, chats=9, seconds=900)
REUSED = ("features", "warm", "private", "tool")


def previous_evidence(previous):
    result = read(previous / "result.json")
    cleanup = read(previous / "cleanup.json")
    require(result["status"] == "FAILED" and result["counts"]["starts"] == 1
            and result["counts"]["chats"] == 5 and
            "grammar operation time limit exceeded" in result["error"],
            "unexpected preceding failure")
    require(cleanup["model_exit"] == 0 and not cleanup["remaining"] and
            cleanup["ports_released"] and cleanup["lease_continuously_held"],
            "preceding resources not released")
    require(result["seconds"] + LIMITS["seconds"] <= CUMULATIVE["seconds"],
            "cumulative time budget")
    files = ["plan.json", "result.json", "cleanup.json", "final.request.json",
             "tool-execution.json", "model.log"]
    for label in REUSED:
        files += [label + ".request.json", label + ".result.json",
                  label + ".wire"]
    return {name: sha(previous / name) for name in files}


def prepare(args):
    previous = args.previous.resolve(strict=True)
    evidence = previous_evidence(previous)
    old = read(previous / "plan.json")
    # Cache, parser, sampler and decoder control path are unchanged. Only the
    # regular-language representation changes, covered by the targeted CPU
    # real-vocabulary and language-boundary cases before this repair run.
    allowed = {"src/server/regex_syntax/schema_regex.rs",
               "tools/serve_schema_v2/acceptance.py"}
    changed = [name for name, digest in old["sources"].items()
               if sha(ROOT / name) != digest]
    require(set(changed) <= allowed and
            "src/server/regex_syntax/schema_regex.rs" in changed,
            "unreviewed source drift during evidence reuse")
    require(old["requests"] == base.requests(), "original requests changed")
    base.prepare(args)
    plan = read(args.output / "plan.json")
    plan.update(planned=dict(starts=1, chats=4), repair_limits=LIMITS,
                cumulative_limits=CUMULATIVE, previous=str(previous),
                previous_evidence=evidence, reused_requests=list(REUSED),
                changed_previous_sources=changed,
                final_request=read(previous / "final.request.json"),
                execution=read(previous / "tool-execution.json"),
                repair="Counted regular-language chains; same request and "
                       "worker limits. One failed request is the ninth "
                       "cumulative Chat; no replay of four passed requests.")
    require(plan["execution"]["result"]["source"]["sha256"] ==
            plan["sources"][base.DOCUMENT], "read document changed")
    save(args.output / "plan.json", plan)


class Repair(base.Stage):
    def __init__(self, plan):
        require(plan["repair_limits"] == LIMITS and
                plan["cumulative_limits"] == CUMULATIVE, "budget drift")
        previous = Path(plan["previous"])
        require(previous_evidence(previous) == plan["previous_evidence"],
                "previous evidence changed")
        require(read(previous / "final.request.json") == plan["final_request"]
                and read(previous / "tool-execution.json") == plan["execution"],
                "original tool follow-up changed")
        super().__init__(plan)

    def budget(self, kind):
        require(time.monotonic() - self.started < LIMITS["seconds"] - 40,
                "repair time exhausted")
        require(self.counts[kind] < LIMITS[kind], "repair attempts exhausted")
        super().budget(kind)

    def business(self):
        final = self.sdk_request("final", self.plan["final_request"])
        base.check_final(final, self.plan["execution"]["result"])
        self.remainder(total_requests=4)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--binary", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--previous", type=Path, required=True)
    prep.add_argument("--port", type=int, default=18164)
    run = sub.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Repair(read(args.plan)).run()


if __name__ == "__main__":
    main()
