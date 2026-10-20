#!/usr/bin/env python3
"""Export exact synchronous startup allocations from a sealed runtime trace."""

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re


INPUTS = (
    "run/normal/http/server.log", "lease/exit.json", "run/build.json",
    "run/normal/http/binary.sha256", "run/normal/http/server-command.json",
)
FIELDS = {"seq", "call", "tid", "api", "phase", "bytes", "ptr", "stream",
          "flags", "caller", "rc", "mono", "backend"}
INTEGER_FIELDS = FIELDS - {"api", "phase", "mono", "backend"}
ALLOC = {"cudaMalloc", "cudaHostAlloc", "cudaMallocHost", "cudaMallocAsync"}
FREE = {"cudaFree", "cudaFreeHost", "cudaFreeAsync"}
APIS = ALLOC | FREE | {
    "cudaStreamSynchronize", "cudaDeviceSynchronize", "cudaEventRecord",
    "cudaEventSynchronize",
}
IDENTITY = ("api", "bytes", "tid", "stream", "flags", "caller", "backend")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def load_json(text):
    return json.loads(text, object_pairs_hook=unique_keys,
                      parse_constant=lambda value: (_ for _ in ()).throw(
                          ValueError("nonfinite JSON number: " + value)))


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def verified_inputs(stage):
    stage = stage.resolve()
    manifest_bytes = (stage / "sealed-files.json").read_bytes()
    manifest = load_json(manifest_bytes)
    if not isinstance(manifest, dict):
        raise ValueError("sealed manifest is not a mapping")
    payloads = {}
    hashes = {}
    for name in INPUTS:
        expected = manifest.get(name)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("missing or invalid sealed input hash: " + name)
        path = stage / name
        if not path.resolve().is_relative_to(stage):
            raise ValueError("sealed input escapes stage: " + name)
        content = path.read_bytes()
        digest = sha256_bytes(content)
        if digest != expected:
            raise ValueError("sealed input hash mismatch: " + name)
        payloads[name] = content
        hashes[name] = digest
    return payloads, dict(manifest_sha256=sha256_bytes(manifest_bytes),
                          inputs_sha256=hashes)


def validate_record(record):
    if not isinstance(record, dict) or record.keys() != FIELDS:
        raise ValueError("memory trace record fields differ")
    for key in INTEGER_FIELDS:
        value = record[key]
        if type(value) is not int or (key != "rc" and value < 0):
            raise ValueError("invalid integer field: " + key)
    if (type(record["mono"]) not in (int, float) or
            not math.isfinite(record["mono"]) or record["mono"] < 0):
        raise ValueError("invalid monotonic timestamp")
    if record["api"] not in APIS or record["phase"] not in ("begin", "end"):
        raise ValueError("unknown memory trace API or phase")
    if record["backend"] != "runtime":
        raise ValueError("plan requires the unchanged runtime allocation backend")
    if record["tid"] <= 0:
        raise ValueError("invalid thread id")


def parse_trace(text):
    ready = None
    opened = {}
    seen_ids = set()
    count = 0
    last_mono = -1
    startup_counts = Counter()
    all_counts = Counter()
    startup_begin = []
    startup_end = []
    host = []
    device = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("[q4t] serving on port "):
            if ready is not None:
                raise ValueError("multiple serving-ready boundaries")
            if opened:
                raise ValueError("unfinished call crosses serving-ready boundary")
            ready = dict(line=number, text=line, trace_records_before=count,
                         last_completed_monotonic=last_mono)
            continue
        if "[memory-trace]" not in line:
            continue
        if not line.startswith("[memory-trace] "):
            raise ValueError(f"malformed memory trace prefix at line {number}")
        record = load_json(line[len("[memory-trace] "):])
        validate_record(record)
        if record["seq"] != count:
            raise ValueError("non-contiguous trace sequence")
        count += 1
        if record["mono"] < last_mono:
            raise ValueError("trace timestamps are not monotonic")
        last_mono = record["mono"]
        ident, api = record["call"], record["api"]
        if record["phase"] == "begin":
            if ident in seen_ids:
                raise ValueError("duplicate call id")
            if record["rc"] != -1:
                raise ValueError("begin record result sentinel differs")
            seen_ids.add(ident)
            if ready is None:
                if api in FREE:
                    raise ValueError("startup contains a free")
                if api == "cudaMallocAsync":
                    raise ValueError("startup contains asynchronous allocation")
                if api == "cudaMalloc":
                    startup_begin.append(ident)
                    if record["bytes"] <= 0 or record["stream"] or record["flags"]:
                        raise ValueError("invalid synchronous startup allocation")
            opened[ident] = dict(record=record, line=number, startup=ready is None)
            continue
        begin = opened.pop(ident, None)
        if begin is None:
            raise ValueError("call end has no begin")
        original = begin["record"]
        if any(original[key] != record[key] for key in IDENTITY):
            raise ValueError("call begin/end identity differs")
        if record["rc"] != 0:
            raise ValueError("trace contains a failed CUDA call")
        if api in ALLOC:
            if original["ptr"] or (record["bytes"] > 0 and not record["ptr"]):
                raise ValueError("invalid successful allocation pointer")
        elif original["ptr"] != record["ptr"]:
            raise ValueError("non-allocation pointer changed within call")
        all_counts[api] += 1
        if not begin["startup"]:
            continue
        startup_counts[api] += 1
        if api not in ALLOC:
            continue
        item = dict(call=ident, bytes=record["bytes"], api=api,
                    flags=record["flags"], caller=record["caller"], tid=record["tid"],
                    begin_seq=original["seq"], end_seq=record["seq"],
                    begin_line=begin["line"], end_line=number,
                    begin_monotonic=original["mono"], end_monotonic=record["mono"])
        if api == "cudaMalloc":
            startup_end.append(ident)
            device.append(item)
        else:
            host.append(item)
    if ready is None:
        raise ValueError("serving-ready boundary missing")
    if opened:
        raise ValueError("trace contains unfinished calls")
    if not seen_ids or seen_ids != set(range(len(seen_ids))):
        raise ValueError("call id set is incomplete")
    if startup_begin != startup_end:
        raise ValueError("startup allocation begin/completion order differs")
    if not device or len({item["tid"] for item in device}) != 1:
        raise ValueError("startup device allocations require one owner thread")
    return dict(records=count, completed_calls=len(seen_ids),
                all_api_counts=dict(all_counts), startup_api_counts=dict(startup_counts),
                serving_ready=ready, startup_device_allocations=device,
                startup_host_allocations=host, startup_free_calls=0,
                startup_async_allocations=0,
                startup_device_bytes=sum(item["bytes"] for item in device),
                startup_host_bytes=sum(item["bytes"] for item in host),
                startup_begin_completion_order_identical=True)


def make_plan(stage):
    payloads, seal = verified_inputs(stage)
    guard = load_json(payloads["lease/exit.json"])
    if guard["status"] != "COMPLETED" or guard["child_returncode"] != 0:
        raise ValueError("source capture guard did not complete successfully")
    build = load_json(payloads["run/build.json"])
    binary = payloads["run/normal/http/binary.sha256"].decode().strip()
    if (not re.fullmatch(r"[0-9a-f]{64}", binary) or
            binary != build["arms"]["runtime"]["binary"] or
            build["default"] != build["unwrapped"]):
        raise ValueError("source runtime binary identity differs")
    trace = parse_trace(payloads["run/normal/http/server.log"].decode("utf-8"))
    _, after_seal = verified_inputs(stage)
    if after_seal != seal:
        raise ValueError("source seal changed during plan export")
    return dict(
        schema="q4t.device_reserve.startup_plan.v1", status="PASS",
        source_stage=str(stage.resolve()), source_seal=seal,
        exporter_sha256=sha256_bytes(Path(__file__).read_bytes()),
        source_runtime_binary_sha256=binary,
        source_default_binary_sha256=build["default"],
        source_command=load_json(payloads["run/normal/http/server-command.json"]),
        **trace,
        scope="Application-linked synchronous cudaMalloc before the unique "
              "serving-ready line. CUDA/library internal allocations excluded. "
              "Host allocations are recorded for audit, not reserved by this plan.")


def make_header(plan, plan_sha):
    sizes = [str(item["bytes"]) + "ULL"
             for item in plan["startup_device_allocations"]]
    lines = ["  " + ", ".join(sizes[index:index + 4]) + ","
             for index in range(0, len(sizes), 4)]
    source_sha = plan["source_seal"]["inputs_sha256"][INPUTS[0]]
    return "\n".join([
        "// Generated from a verified startup trace. Do not edit.",
        "#pragma once", "#include <cstddef>", "",
        "namespace q4t_reserve_plan {",
        "inline constexpr std::size_t kDeviceSizes[] = {", *lines, "};",
        f"inline constexpr std::size_t kCount = {len(sizes)};",
        "inline constexpr std::size_t kStartupDeviceBytes = "
        f"{plan['startup_device_bytes']}ULL;",
        f'inline constexpr char kPlanSha256[] = "{plan_sha}";',
        f'inline constexpr char kSourceLogSha256[] = "{source_sha}";',
        "}  // namespace q4t_reserve_plan", "",
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.stage.resolve()):
        parser.error("output must be outside the sealed source stage")
    if args.output.exists() or args.output.is_symlink():
        parser.error("output already exists; plan artifacts will not be overwritten")
    try:
        plan = make_plan(args.stage)
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"plan export failed: {error}\n")
    serialized = (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode()
    plan_sha = sha256_bytes(serialized)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "plan.json").write_bytes(serialized)
    (args.output / "reserve_plan.h").write_text(make_header(plan, plan_sha))
    print(json.dumps(dict(plan_sha256=plan_sha,
                          device_count=len(plan["startup_device_allocations"]),
                          host_count=len(plan["startup_host_allocations"]),
                          startup_device_bytes=plan["startup_device_bytes"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
