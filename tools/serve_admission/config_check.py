"""CPU-only native configuration/schema check; never starts a server/model."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

import jsonschema

ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prior", type=Path,
                        help="retain previous result; rerun only its failed fixtures")
    args = parser.parse_args()
    engine, output = args.engine.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).with_name("config_probe.cpp")
    obj, binary = output / "probe.o", output / "config_probe"
    compile_command = ["/usr/bin/g++-14", "-std=c++23", "-O2", "-Wall", "-Wextra",
        "-Werror", "-DQ4T_ENABLE_XGRAMMAR=1", "-I"+str(ROOT / "include"),
        "-I"+str(engine / "generated"), "-c", str(source), "-o", str(obj)]
    link_file = engine / "CMakeFiles/q4t_preflight_probe.dir/link.txt"
    link = shlex.split(link_file.read_text())
    original = "CMakeFiles/q4t_preflight_probe.dir/tools/serve_preflight/prompt_probe.cpp.o"
    if link.count(original) != 1:
        raise RuntimeError("unexpected production link command")
    link[link.index(original)] = str(obj)
    link[link.index("-o") + 1] = str(binary)
    link = ["-Wl,--dependency-file="+str(output / "link.d")
            if arg.startswith("-Wl,--dependency-file=") else arg for arg in link]
    archives = [engine / arg for arg in link if arg.endswith(".a")]
    save(output / "build-command.json", dict(compile=compile_command, link=link,
        cwd=str(engine), sources={str(p): sha(p) for p in
            [source, Path(__file__), link_file, *archives]}))
    previous = None
    if args.prior:
        previous = json.loads((args.prior / "result.json").read_text())
        binary = args.prior.resolve() / "config_probe"
        if sha(binary) != previous["binary_sha256"]:
            raise RuntimeError("previous native probe identity changed")
        save(output / "reuse-build.json", dict(binary=str(binary),
            sha256=sha(binary), prior_result_sha256=sha(args.prior / "result.json")))
    else:
        with (output / "build.log").open("xb") as log:
            for command in (compile_command, link):
                subprocess.run(command, cwd=engine, stdout=log, stderr=log,
                               timeout=120, check=True)
        if (output / "build.log").stat().st_size:
            raise RuntimeError("unexpected compiler/linker diagnostics")
    fixtures = [
        dict(name="defaults", argv=[], effective_slots=1, expected=[32, 31]),
        dict(name="requested_slots", argv=["--max-seq", "32"],
             effective_slots=32, expected=[64, 32]),
        dict(name="budget_shrink", argv=["--max-seq", "32"],
             effective_slots=2, expected=[32, 30]),
        dict(name="explicit", argv=["--max-inflight-requests", "4",
             "--max-queued-requests", "2"], effective_slots=2, expected=[4, 2]),
        dict(name="single_inflight", argv=["--max-inflight-requests", "1"],
             effective_slots=2, expected=[1, 0]),
        dict(name="upper_bounds", argv=["--max-inflight-requests", "4096",
             "--max-queued-requests", "4096"], effective_slots=1, expected=[4096, 4096]),
        dict(name="no_waiting", argv=["--max-queued-requests", "0"],
             effective_slots=1, expected=[32, 0]),
    ]
    for name, argv, slots in [
        ("zero_inflight", ["--max-inflight-requests", "0"], 1),
        ("negative_queue", ["--max-queued-requests", "-1"], 1),
        ("inflight_overflow", ["--max-inflight-requests", "4097"], 1),
        ("queue_overflow", ["--max-queued-requests", "4097"], 1),
        ("cross_limit", ["--max-inflight-requests", "4", "--max-queued-requests", "5"], 1),
        ("budget_shrink_invalid", ["--max-seq", "32", "--max-queued-requests", "40"], 2),
        ("zero_slots", [], 0), ("slots_overflow", [], 8193),
    ]:
        fixtures.append(dict(name=name, argv=argv, effective_slots=slots, expected=None))
    reused = 0
    if previous:
        old_fixtures = json.loads((args.prior / "fixtures.json").read_text())
        if old_fixtures != fixtures or not previous["failures"]:
            raise RuntimeError("targeted correction requires identical fixture inputs")
        failed_names = {row["name"] for row in previous["failures"]}
        reused = len(fixtures) - len(failed_names)
        fixtures = [row for row in fixtures if row["name"] in failed_names]
    save(output / "fixtures.json", fixtures)
    data = "".join(json.dumps(row)+"\n" for row in fixtures)
    (output / "requests.jsonl").write_text(data)
    result = subprocess.run([str(binary)], input=data.encode(), capture_output=True,
                            timeout=30, check=False)
    (output / "stdout.jsonl").write_bytes(result.stdout)
    (output / "stderr.log").write_bytes(result.stderr)
    if result.returncode or result.stderr:
        raise RuntimeError("native probe failed")
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    if len(responses) != len(fixtures):
        raise RuntimeError("probe response count")
    schema_path = ROOT / "docs/serve-config-v1.schema.json"
    if previous and sha(schema_path) != previous["schema_sha256"]:
        raise RuntimeError("schema identity changed")
    validator = jsonschema.Draft202012Validator(json.loads(schema_path.read_text()))
    failures = []
    for fixture, response in zip(fixtures, responses):
        try:
            accepted = fixture["expected"] is not None
            assert response["accepted"] is accepted
            if not accepted:
                assert response["error"]["error"]["code"] == "invalid_options"
                continue
            config = response["configuration"]
            validator.validate(config)
            inflight, queued = fixture["expected"]
            assert config["limits"]["text_requests"] == inflight
            assert config["limits"]["queued_requests"] == queued
            assert config["limits"]["queue_policy"] == "capacity_fifo_best_fit"
            assert config["limits"]["queue_order"] == "slot_admission_arrival"
            assert config["limits"]["queue_excludes_preparation"] is True
            assert config["max_inflight_requests"]["effective"] == inflight
            assert config["max_queued_requests"]["effective"] == queued
            for key, sentinel in (("max_inflight_requests", 0), ("max_queued_requests", -1)):
                flag = "--" + key.replace("_", "-")
                args_ = fixture["argv"]
                explicit = flag in args_
                requested = int(args_[args_.index(flag)+1]) if explicit else sentinel
                assert config[key]["requested"] == requested
                expected_source = "cli" if explicit else "builtin_default"
                assert config[key]["source"] == expected_source, (key, config[key])
        except Exception as error:
            failures.append(dict(name=fixture["name"], error=repr(error)))
    summary = dict(passed=not failures, fixtures=len(fixtures), reused=reused,
        positives=sum(row["expected"] is not None for row in fixtures),
        failures=failures, binary_sha256=sha(binary), schema_sha256=sha(schema_path),
        scope="Hypothetical effective slot counts, production CLI/config only; no GPU/HTTP.")
    save(output / "result.json", summary)
    print(json.dumps(summary))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
