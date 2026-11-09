"""Build a CPU-only production semantics probe; no test/model side effects."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ["tools/serve_protocol/contract_probe.cpp", "src/io/json.cpp",
    "src/server/chat_contract.cpp", "src/server/chat_plan.cpp",
    "src/server/chat_storage_options.cpp",
    "src/server/sampling.cpp", "src/server/completion_plan.cpp",
    "src/server/prediction.cpp", "src/server/legacy_tools.cpp", "src/server/custom_tools.cpp",
    "src/server/chat_prompt.cpp", "src/server/chat_template.cpp",
    "src/server/chat_tool_history.cpp", "src/server/chat_response_parser.cpp",
    "src/server/reasoning_options.cpp", "src/server/thinking_budget.cpp",
    "src/server/text_stop.cpp", "src/server/input_contract.cpp",
    "src/server/output_constraints.cpp", "src/server/output_schema.cpp",
    "src/server/schema_graph.cpp", "src/server/schema_decimal.cpp",
    "src/server/schema_prefix.cpp", "src/server/schema_receipt.cpp",
    "src/server/http_response.cpp", "src/server/session_cache.cpp",
    "src/server/cache_io.cpp", "src/server/cache_worker.cpp",
    "src/server/grammar_process.cpp",
    "src/trace/sha256.cpp", "src/server/client_policy.cpp",
    "src/server/media_admission.cpp", "src/vision/media_plan.cpp"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--generated", type=Path,
                    default=ROOT / ".q4t-work/serve-preflight-20261103/engine/generated")
    args = ap.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    binary = output / "contract_probe"
    generated = args.generated.resolve()
    command = ["g++-14", "-std=c++23", "-O2", "-Wall", "-Wextra", "-Werror",
        "-pthread", "-Iinclude", "-I" + str(generated), *SOURCES, "-o", str(binary)]
    metadata = dict(argv=command, sources={p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
                                         for p in SOURCES},
        protocol_header_sha256=hashlib.sha256((generated/
            "q4t/server/chat_protocol_identity.h").read_bytes()).hexdigest())
    (output / "build-command.json").write_text(json.dumps(metadata, indent=2)+"\n")
    run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
    (output / "build.log").write_text(run.stdout+run.stderr)
    assert run.returncode == 0 and not run.stderr, "probe build diagnostics"
    (output / "build-result.json").write_text(json.dumps(dict(passed=True,
        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        scope="CPU-only shared production semantics; no tokenizer, model or CUDA"), indent=2)+"\n")


if __name__ == "__main__":
    main()
