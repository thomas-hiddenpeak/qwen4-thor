"""Fresh bounded HTTP phase after the retained, pre-model fixture failures."""
import argparse
from pathlib import Path
import time

import stage as s

PRIOR = s.BASE
BASE = s.ROOT / ".q4t-work/serve-thinking-budget-http-20261103"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
LIMITS = dict(starts=5, generations=22, negatives=12, seconds=1800)
DRIVER_RESULT = BASE / "driver-result.json"
CANDIDATE = "c02b94144c5a40c6118bd7dde306b298dce10c963045708f82082f6a5f2b8b54"


def bind():
    # BASE in the shared preparer remains the immutable build/CPU source.
    s.PACKAGE, s.OUTPUT, s.LIMITS = PACKAGE, OUTPUT, LIMITS
    s.RELEASE_VERSION = "serve-thinking-budget-http-20261103"
    s.bind()


def reuse():
    for name in ("package", "package-correction", "cpu", "execution", "execution-correction"):
        s.h.verify_seal(PRIOR / name)
    retired = s.h.read(PRIOR / "retired.json")
    s.h.require(not retired["accepted"] and retired["actual"] ==
                dict(starts=2, generations=0, negatives=0, model_loads=0),
                "prior phase is not the frozen pre-model failure state")
    s.h.require(s.h.sha(PRIOR / "engine/q4t") == CANDIDATE, "candidate changed")
    old_sources = s.h.read(PRIOR / "package-correction/sources.json")
    checked = {p: digest for p, digest in old_sources.items()
               if p.startswith(("src/", "include/", "cmake/", "tests/")) or p == "CMakeLists.txt"}
    s.a.verify_hashes(s.ROOT, checked)
    preflight = s.h.read(PRIOR / "cpu/startup-preflight/result.json")
    s.a.verify_hashes(s.ROOT, preflight["sources"])
    s.h.require(s.h.identity(preflight["binary"]["path"]) == preflight["binary"],
                "startup preflight binary changed")
    s.h.require(s.h.read(DRIVER_RESULT)["passed"], "new driver checks failed")
    return dict(prior_retired=s.h.identity(PRIOR / "retired.json"),
        prior_counts=retired["actual"], prior_phase_preserved=True,
        runtime_sources=checked, reused_cpu_seal=s.h.identity(PRIOR / "cpu/sealed-files.json"),
        new_driver=s.h.identity(DRIVER_RESULT),
        scope="Only previously unexecuted HTTP cases. Prior failures remain unaccepted; new limits are explicit.")


def main():
    global PACKAGE, OUTPUT, DRIVER_RESULT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "finalize", "rollback"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()
    continuation = None
    if args.repair:
        original = BASE / "execution"
        failed = s.h.read(original / "business/result.json")
        s.h.require(failed["request_attempts"] == failed["negative_requests"] == 0 and
                    failed["starts"] == 1 and "read_bytes" in failed["error"] and
                    not list((original / "business").rglob("server.log")) and
                    s.h.read(original / "lease/exit.json")["status"] == "COMMAND_FAILED",
                    "repair only covers the preserved pre-Popen path conversion error")
        continuation = dict(original_started=s.h.read(original / "begin.json")["started"],
            prior_counts=dict(starts=1, generations=0, negatives=0), prior_execution=str(original))
        PACKAGE, OUTPUT = BASE / "package-repair", BASE / "execution-repair"
        DRIVER_RESULT = BASE / "driver-repair-result.json"
    bind()
    if args.action == "prepare":
        s.prepare(provenance=reuse(), continuation=continuation)
    elif args.action == "execute":
        s.h.require_guard(OUTPUT)
        s.h.save(OUTPUT / "begin.json", dict(started=time.time(), limits=LIMITS))
        s.a.execute()
    elif args.action == "finalize":
        s.a.finalize()
    else:
        s.h.require(args.output is not None, "fresh lease output required")
        s.a.restore(args.output.resolve())


if __name__ == "__main__":
    main()
