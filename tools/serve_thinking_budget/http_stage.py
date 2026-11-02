"""Fresh bounded HTTP phase after the retained, pre-model fixture failures."""
import argparse
from pathlib import Path
import re
import time

import stage as s

PRIOR = s.BASE
BASE = s.ROOT / ".q4t-work/serve-thinking-budget-http-20261103"
PACKAGE, OUTPUT = BASE / "package", BASE / "execution"
LIMITS = dict(starts=5, generations=22, negatives=12, seconds=1800)
DRIVER_RESULT = BASE / "driver-result.json"
CANDIDATE = "c02b94144c5a40c6118bd7dde306b298dce10c963045708f82082f6a5f2b8b54"
ORIGINAL_CPU_GATE = s.cpu_gate


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


def boundary_cpu_gate():
    current = s.BASE
    try:
        s.BASE = PRIOR
        ORIGINAL_CPU_GATE()
    finally:
        s.BASE = current
    s.h.verify_seal(PRIOR / "cpu")
    cpu = BASE / "cpu"
    for name, total in (("budget.log", 8), ("grammar.log", 1), ("capabilities.log", 4)):
        s.h.require(f"{total} tests, {total} passed, 0 failed, 0 skipped" in
                    (cpu / name).read_text(), "affected CPU check failed: " + name)
    for name, total in (("client-budget.log", 1), ("driver.log", 13)):
        text = (cpu / name).read_text()
        s.h.require(f"Ran {total} test" in text and text.endswith("\nOK\n"), name)
    text = (cpu / "build-native.log").read_text()
    s.h.require("warning:" not in text and "error:" not in text, "native build diagnostics")


def boundary_reuse():
    boundary_cpu_gate()
    old = s.h.read(PRIOR / "package-correction/sources.json")
    paths = {p: digest for p, digest in old.items()
             if p.startswith(("src/", "include/", "cmake/", "tests/")) or p == "CMakeLists.txt"}
    changed = {p for p, digest in paths.items() if s.h.sha(s.ROOT / p) != digest}
    allowed = {"src/server/thinking_budget.cpp", "include/q4t/server/thinking_budget.h",
               "src/server/chat_capabilities.cpp", "tests/server_thinking_budget_test.cpp",
               "tests/server_structured_decoder_test.cpp", "tests/server_chat_capabilities_test.cpp"}
    s.h.require(changed == allowed, "unexpected runtime/CPU scope change")
    original = BASE / "execution-repair/business/candidate-mtp"
    files = {p.name: s.h.identity(p) for p in original.iterdir() if p.is_file() and
             (p.name.startswith(("unbudgeted-", "null-budget-")) or
              re.fullmatch(r"negative-\d+-(request|response)\.bin", p.name))}
    expected = {label + "-" + suffix for label in ("unbudgeted", "null-budget")
                for suffix in ("request.json", "result.json", "wire.bin", "headers.json", "sdk.json")}
    expected |= {f"negative-{i}-{kind}.bin" for i in range(12) for kind in ("request", "response")}
    s.h.require(set(files) == expected, "incomplete baseline/input reuse records")
    for label in ("unbudgeted", "null-budget"):
        result = s.h.read(original / (label + "-result.json"))
        s.h.require(result["message"]["content"].strip() == "710003" and
                    result["usage"]["completion_tokens"] == 26, "baseline quality")
    return dict(prior_retired=s.h.identity(PRIOR / "retired.json"),
        prior_phase_preserved=True, changed_sources=sorted(changed),
        reused_cpu_seal=s.h.identity(PRIOR / "cpu/sealed-files.json"),
        repair_cpu=s.h.identity(BASE / "cpu/repair-result.json"),
        reuse_http=dict(files=files, generations=2, negatives=12,
            rationale="Only nonnull enabled budget closure changed. Absent/null generation and input validation exit before PrepareTokens; their source paths are unchanged. Budget zero and one are rerun."))


def main():
    global PACKAGE, OUTPUT, DRIVER_RESULT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "finalize", "rollback"])
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--repair", action="store_true")
    mode.add_argument("--boundary", action="store_true")
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
    if args.boundary:
        original = BASE / "execution-repair"
        failed = s.h.read(original / "business/result.json")
        s.h.require(failed["request_attempts"] == 4 and failed["negative_requests"] == 12 and
                    failed["starts"] == 1 and failed["error"] == "AssertionError('answer quality')" and
                    s.h.read(original / "lease/exit.json")["status"] == "COMMAND_FAILED",
                    "boundary repair only follows the recorded budget-one business failure")
        continuation = dict(original_started=s.h.read(BASE / "execution/begin.json")["started"],
            prior_counts=dict(starts=2, generations=4, negatives=12), prior_execution=str(original))
        PACKAGE, OUTPUT = BASE / "package-boundary", BASE / "execution-boundary"
        s.BASE, s.cpu_gate = BASE, boundary_cpu_gate
    bind()
    if args.action == "prepare":
        s.prepare(provenance=boundary_reuse() if args.boundary else reuse(), continuation=continuation)
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
