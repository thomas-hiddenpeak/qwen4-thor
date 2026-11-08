"""Three real text/cache turns affected by model-visible response schemas."""
import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_schema_v2 import acceptance as base
from tools.serve_service.package import read, require

LIMITS = dict(starts=1, chats=3, controls=2000, seconds=600)


class Cache(base.Stage):
    limits = LIMITS
    success_status = "PASS_SCHEMA_V2_PROMPT_CACHE"

    def business(self):
        rows = self.plan["requests"]
        cold = self.sdk_request("features", rows["features"])
        base.check_features(cold)
        require(8192 <= cold.usage["prompt_tokens"] <= 14500,
                "private cache prompt bound")
        warm = self.sdk_request("warm", rows["warm"])
        base.check_features(warm)
        require(cold.message == warm.message and cold.logprobs == warm.logprobs,
                "cached token probabilities changed")
        require(self.record(rows["warm"]) == dict(
            prefix=1024, source="shared", layout="target"), "shared cache miss")
        private = base.tagged("private", rows["features"])
        private.pop("prefix_cache_scope")
        private.pop("prefix_cache_write_tokens")
        private["messages"] += [dict(role="assistant", content=warm.message["content"]),
            dict(role="user", content="Return the same JSON object again, "
                 "with node.children empty and note null.")]
        value = self.sdk_request("private", private)
        base.check_features(value)
        record = self.record(private)
        require(record["prefix"] >= 8192 and record["source"] == "session",
                "private target cache miss")
        for label, answer in (("features", cold), ("warm", warm), ("private", value)):
            preflight = read(self.out / (label + "-preflight.wire"))
            require("# Response format" in preflight["prompt"] and
                    answer.usage["prompt_tokens"] == preflight["prompt_tokens"],
                    "schema prompt not included in token accounting")
        metrics = self.drained("final")
        require(metrics["q4t_requests_total"] == 3 and
                metrics["q4t_requests_success_total"] == 3 and
                metrics["q4t_requests_error_total"] == 0, "cache accounting")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--binary", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--port", type=int, default=18164)
    run = sub.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        base.prepare(args, limits=LIMITS, scope=__doc__)
    else:
        with base.lifecycle.child_subreaper():
            Cache(read(args.plan)).run()


if __name__ == "__main__":
    main()
