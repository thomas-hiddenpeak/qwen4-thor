"""Retry only failed video and the two unexecuted image/tool turns."""
import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_schema_v2 import acceptance as base, media
from tools.serve_service.package import read, save, sha, require

LIMITS = dict(starts=1, chats=3, controls=2000, seconds=600)
CUMULATIVE = dict(starts=3, chats=6, seconds=800)


def preceding(previous):
    result = read(previous / "result.json")
    cleanup = read(previous / "cleanup.json")
    old = read(previous / "plan.json")
    require(result["status"] == "FAILED" and result["counts"]["starts"] == 1
            and result["counts"]["chats"] == 2 and
            "grammar-accepted stop token" in result["error"],
            "unexpected preceding media failure")
    require(cleanup["model_exit"] == 0 and not cleanup["remaining"] and
            cleanup["ports_released"] and cleanup["lease_continuously_held"],
            "preceding media resources remain")
    require(result["seconds"] + LIMITS["seconds"] <= CUMULATIVE["seconds"],
            "cumulative media time budget")
    require(old["requests"] == media.requests() and
            old["fixtures"] == media.fixtures(), "original task/fixtures changed")
    allowed = {"src/server/regex_syntax/schema_regex.rs",
               "tools/serve_schema_v2/media.py",
               "src/server/chat_template.cpp", "src/server/chat_capabilities.cpp",
               "tools/serve_protocol/build_contract.py"}
    changed = {name for name, digest in old["sources"].items()
               if sha(ROOT / name) != digest}
    require(changed <= allowed and
            "src/server/regex_syntax/schema_regex.rs" in changed,
            "unreviewed source drift")
    names = ["plan.json", "result.json", "cleanup.json", "model.log",
             "cabin.request.json", "cabin.result.json", "cabin.wire",
             "motion.request.json", "motion.wire", "lease/exit.json"]
    return {name: sha(previous / name) for name in names}


def failed_retry(previous):
    result = read(previous / "result.json")
    cleanup = read(previous / "cleanup.json")
    require(result["status"] == "FAILED" and result["counts"]["starts"] == 1
            and result["counts"]["chats"] == 1 and
            "visual truth differs: motion" in result["error"],
            "unexpected preceding semantic failure")
    require(cleanup["model_exit"] == 0 and not cleanup["remaining"] and
            cleanup["ports_released"] and cleanup["lease_continuously_held"],
            "preceding semantic retry resources remain")
    return {name: sha(previous / name) for name in
            ("plan.json", "result.json", "cleanup.json", "motion.request.json",
             "motion.result.json", "motion.wire", "model.log", "lease/exit.json")}


def prepare(args):
    previous = args.previous.resolve(strict=True)
    evidence = preceding(previous)
    retry = args.failed_retry.resolve(strict=True)
    retry_evidence = failed_retry(retry)
    require(read(previous / "result.json")["seconds"] +
            read(retry / "result.json")["seconds"] + LIMITS["seconds"] <=
            CUMULATIVE["seconds"], "cumulative media retry deadline")
    old = read(previous / "plan.json")
    base.prepare(args, rows=media.requests(), limits=LIMITS,
        scope="Assertion context and schema model-visibility repair. Original "
              "video request plus unexecuted image/tool/result requests; "
              "reuse passed cabin and unchanged text/cache evidence. "
              "No schema weakening, limit increase or automatic retries.")
    plan = read(args.output / "plan.json")
    plan.update(previous=str(previous), previous_evidence=evidence,
                failed_retry=str(retry), failed_retry_evidence=retry_evidence,
                cumulative_limits=CUMULATIVE, reused_requests=["cabin"],
                fixtures=old["fixtures"], expected=old["expected"],
                fixture_provenance=old["fixture_provenance"])
    save(args.output / "plan.json", plan)


class Repair(media.Media):
    limits = LIMITS
    visual_labels = ("motion",)

    def __init__(self, plan):
        require(plan["cumulative_limits"] == CUMULATIVE and
                preceding(Path(plan["previous"])) == plan["previous_evidence"],
                "preceding media evidence changed")
        require(failed_retry(Path(plan["failed_retry"])) ==
                plan["failed_retry_evidence"], "preceding retry changed")
        super().__init__(plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--binary", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--previous", type=Path, required=True)
    prep.add_argument("--failed-retry", type=Path, required=True)
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
