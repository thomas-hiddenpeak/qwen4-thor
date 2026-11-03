"""Bounded CPU prerequisites for strict transport; no HTTP/model/GPU work.

Reuses a verified production build's static libraries. Synthetic finite logits
prove tokenizer/grammar path admissibility, never actual model token selection.
The old failed GPU experiment and its budget remain untouched.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_protocol.check_contract import load_validators


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open("x") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def require(value, message):
    if not value:
        raise RuntimeError(message)


def command(folder, label, argv, cwd, timeout):
    save(folder / (label + "-command.json"), dict(argv=argv, cwd=str(cwd),
                                                 started=time.time()))
    with (folder / (label + ".log")).open("xb") as stdout, \
            (folder / (label + "-stderr.log")).open("xb") as stderr:
        result = subprocess.run(argv, cwd=cwd, stdout=stdout, stderr=stderr,
                                timeout=timeout)
    save(folder / (label + "-exit.json"), dict(exit=result.returncode,
                                              ended=time.time()))
    require(result.returncode == 0, label + " failed; preserve this attempt")


def prepare(args):
    output = args.output.resolve()
    require(output.is_relative_to(ROOT / ".q4t-work"), "artifact directory")
    output.mkdir(parents=True, exist_ok=False)
    previous = ROOT / ".q4t-work/serve-observability-20261103"
    # Match the delivered runtime, not just a similarly named library directory.
    source_path = previous / "package/candidate/runtime-source.json"
    sources = json.loads(source_path.read_text())
    require(all(sha(ROOT / name) == value for name, value in sources.items()),
            "runtime changed; review/rebuild the probe dependency first")
    build = json.loads((previous / "cpu/build-result.json").read_text())
    require(build["passed"] and build["zero_warnings"] and
            sha(previous / "engine/q4t") == build["binary_sha256"] ==
            sha(ROOT / "build/q4t"), "production dependency identity")
    save(output / "runtime-sources.json", sources)
    reference = ROOT / ".q4t-work/serve-backpressure-20261103/payload-array"
    original = json.loads((reference / "sse-request.json").read_text())
    expected = (reference / "expected-content.json").read_bytes()
    require(len(expected) == 12443, "keep original output byte requirement")
    (output / "expected-content.json").write_bytes(expected)
    save(output / "original-sse-request.json", original)
    candidate = copy.deepcopy(original)
    candidate["messages"][0]["content"] = (
        "逐字复制下面的JSON，不解释、不添加空白或代码围栏。完整保留数组的48项，"
        "每个字符串中的等号数不能改变。\n" + expected.decode())
    candidate["request_id"] = "strict-copy-sse"
    validator = load_validators()["chat"]
    validator.validate(candidate)
    save(output / "sse-request.json", candidate)
    candidate = copy.deepcopy(candidate)
    candidate["stream"] = False
    candidate.pop("stream_options", None)
    candidate["request_id"] = "strict-copy-json"
    validator.validate(candidate)
    save(output / "json-request.json", candidate)
    require(candidate["max_tokens"] == 1024 and
            candidate["request_timeout_ms"] == 180000 and
            candidate["response_format"] == original["response_format"],
            "do not increase budget or change schema")

    prior = json.loads((previous / "cpu/planned-native/commands.json").read_text())
    old = str(previous / "cpu/planned-native")
    compile_command = [arg.replace(old, str(output)).replace(
        "tools/serve_protocol/contract_probe.cpp",
        "tools/serve_strict_transport/payload_probe.cpp")
        for arg in prior["compile"]]
    link_command = [arg.replace(old, str(output)).replace("/contract_probe", "/payload_probe")
                    for arg in prior["link"]]
    dependencies = {}
    for arg in link_command:
        path = Path(prior["cwd"]) / arg
        if not arg.startswith("-") and path.is_file():
            dependencies[str(path.resolve())] = sha(path)
    model = args.model.resolve()
    for name in ("tokenizer.json", "config.json", "generation_config.json"):
        dependencies[str(model / name)] = sha(model / name)
    for name in ("prepare.py", "payload_probe.cpp"):
        path = Path(__file__).parent / name
        dependencies[str(path)] = sha(path)
    save(output / "dependencies.json", dependencies)
    command(output, "compile", compile_command, prior["cwd"], 120)
    command(output, "link", link_command, prior["cwd"], 120)
    require(not re.search(r"\bwarning:", "".join(
        (output / (label + suffix)).read_text()
        for label in ("compile", "link")
        for suffix in (".log", "-stderr.log")), re.I), "build warning")
    command(output, "probe", [str(output / "payload_probe"), str(model),
                              str(output)], ROOT, 150)
    records = [json.loads(line) for line in (output / "probe.log").read_text().splitlines()]
    require(len(records) == 8, "incomplete CPU evidence")
    by_name = {(r["phase"], r.get("name")): r for r in records}
    require(by_name["tokenizer", None]["canonical_text_tokens"] == 243,
            "canonical encoding drift")
    require(not by_name["plan", "original"]["contains_expected"] and
            by_name["plan", "copy-sse"]["contains_expected"] and
            by_name["plan", "copy-sse"]["prompt_tokens"] + 1024 < 8192,
            "explicit copy conditioning prerequisite")
    require(by_name["replay", "canonical"]["eos_accepted"] and
            by_name["replay", "bytewise-prefix"]["accepted_text_tokens"] == 1100 and
            not by_name["replay", "bytewise-prefix"]["eos_accepted"],
            "grammar admissibility prerequisite")
    require(all(sha(path) == value for path, value in dependencies.items()) and
            all(sha(ROOT / name) == value for name, value in sources.items()),
            "dependencies changed during CPU probe")
    result = dict(passed=True, scope="CPU admissibility/serialization only",
        actual_model_generation_proven=False, real_tcp_backpressure_proven=False,
        old_failure_cause_proven=False, old_gpu_budget_restarted=False,
        binary_sha256=sha(output / "payload_probe"),
        production_sha256=build["binary_sha256"],
        unchanged_default=True, records=records)
    save(output / "result.json", result)
    print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=Path.home() /
        "models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream")
    args = parser.parse_args()
    prepare(args)


if __name__ == "__main__":
    main()
