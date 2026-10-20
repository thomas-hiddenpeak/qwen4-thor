#!/usr/bin/env python3
"""Correlate real reservation calls with a sealed kernel timeline."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/verify/warm_reclaim"))
import analyze_kernel as kernel
import export_plan

EVENTS = {"plan", "alloc", "free", "handoff", "prepared", "summary", "fatal",
          "pinned_gate_failed", "handoff_failed"}


def verify_inputs(stage):
    verified = kernel.verify_sealed_inputs(stage)
    manifest = export_plan.load_json((stage / "sealed-files.json").read_bytes())
    name = "plan/plan.json"
    path = stage / name
    if not path.resolve().is_relative_to(stage.resolve()):
        raise ValueError("reservation plan resolves outside sealed stage")
    digest = kernel.sha256(path)
    if manifest.get(name) != digest:
        raise ValueError("sealed reservation plan hash mismatch or absent")
    verified["inputs"][name] = digest
    return verified


def positive_int(row, key, zero=True):
    value = row[key]
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError("invalid reserve integer: " + key)
    return value


def parse_reserve(lines, sizes, plan_sha, source_sha):
    rows = []
    opened = {}
    operations = []
    plan_row = None
    prepared = []
    summaries = []
    errors = []
    handed = set()
    allocated = set()
    latest = -1
    for number, line in enumerate(lines, 1):
        if "[reserve-trace]" not in line:
            continue
        if not line.startswith("[reserve-trace] "):
            raise ValueError("malformed reserve trace prefix")
        row = export_plan.load_json(line[len("[reserve-trace] "):])
        if positive_int(row, "seq") != len(rows):
            raise ValueError("reserve trace sequence gap")
        positive_int(row, "tid", zero=False)
        if (type(row["mono"]) not in (int, float) or
                not math.isfinite(row["mono"]) or row["mono"] < latest):
            raise ValueError("reserve timestamp is invalid or out of order")
        latest = row["mono"]
        event = row["event"]
        if event not in EVENTS:
            raise ValueError("unknown reserve event: " + str(event))
        row["source_line"] = number
        rows.append(row)
        if event == "plan":
            if plan_row is not None:
                raise ValueError("multiple reservation plans")
            if (row["plan_sha256"] != plan_sha or
                    row["source_sha256"] != source_sha or
                    row["count"] != len(sizes) or row["total_bytes"] != sum(sizes)):
                raise ValueError("logged reservation plan identity differs")
            plan_row = row
        elif event in ("alloc", "free", "handoff"):
            if plan_row is None:
                raise ValueError("reservation operation precedes plan")
            index = positive_int(row, "index")
            if index >= len(sizes) or row["bytes"] != sizes[index]:
                raise ValueError("reservation operation differs from planned size")
            positive_int(row, "ptr")
            if event == "handoff":
                if index in handed or index not in allocated or not row["ptr"]:
                    raise ValueError("invalid reservation handoff")
                handed.add(index)
                continue
            if row["api"] != ("cudaMalloc" if event == "alloc" else "cudaFree"):
                raise ValueError("reserve event API mismatch")
            key = (event, index)
            if row["phase"] == "begin":
                if key in opened or row["rc"] != -1:
                    raise ValueError("duplicate reserve begin or invalid sentinel")
                if event == "alloc" and (index in allocated or row["ptr"]):
                    raise ValueError("duplicate allocation or nonnull begin pointer")
                opened[key] = row
            elif row["phase"] == "end":
                begin = opened.pop(key, None)
                if begin is None or begin["tid"] != row["tid"]:
                    raise ValueError("reserve end has no matching begin")
                positive_int(row, "rc")
                if event == "free" and (row["ptr"] != begin["ptr"] or
                                        row["reason"] != begin["reason"]):
                    raise ValueError("reserve free pointer/reason changed")
                if event == "alloc" and row["rc"] == 0:
                    if not row["ptr"]:
                        raise ValueError("successful reservation pointer is null")
                    allocated.add(index)
                operations.append(dict(event=event, index=index, bytes=row["bytes"],
                                       tid=row["tid"], begin=begin["mono"],
                                       end=row["mono"], seconds=row["mono"]-begin["mono"],
                                       rc=row["rc"], pointer=row["ptr"],
                                       begin_line=begin["source_line"], end_line=number,
                                       reason=row.get("reason")))
            else:
                raise ValueError("unknown reserve operation phase")
        elif event == "prepared":
            prepared.append(row)
        elif event == "summary":
            summaries.append(row)
        else:
            errors.append(row)
    if plan_row is None:
        raise ValueError("reservation plan event missing")
    if len(prepared) > 1 or len(summaries) > 1:
        raise ValueError("duplicate prepared/summary event")
    return dict(records=len(rows), event_counts=Counter(r["event"] for r in rows),
                plan=plan_row, prepared=prepared, summaries=summaries, errors=errors,
                allocated_count=len(allocated), handed_count=len(handed),
                operations=operations, uncompleted_operations=list(opened.values()))


def operation_at(operations, starts, timestamp):
    index = bisect_right(starts, timestamp) - 1
    if index < 0:
        return None
    operation = operations[index]
    return operation if timestamp <= operation["observed_end"] else None


def analyze(stage):
    verified = verify_inputs(stage)
    capture = json.loads((stage / "kernel/exit.json").read_text())
    if capture["status"] != "COMPLETED" or any(capture["loss_totals"].values()):
        raise ValueError("kernel capture incomplete or lost events")
    plan = export_plan.load_json((stage / "plan/plan.json").read_bytes())
    sizes = [item["bytes"] for item in plan["startup_device_allocations"]]
    source_sha = plan["source_seal"]["inputs_sha256"][export_plan.INPUTS[0]]
    result = parse_reserve((stage / "run/normal/http/server.log").read_text().splitlines(),
                           sizes, verified["inputs"]["plan/plan.json"], source_sha)
    boundary = kernel.cleanup_boundary(stage)
    cutoff = boundary.get("monotonic_approx")
    operations = result["operations"]
    for row in result["uncompleted_operations"]:
        # Bound unknown calls at runner cleanup; never invent an API return.
        if cutoff is not None and cutoff > row["mono"]:
            operations.append(dict(event=row["event"], index=row["index"],
                                   bytes=row["bytes"], tid=row["tid"], begin=row["mono"],
                                   end=None, seconds=None, rc=None,
                                   begin_line=row["source_line"], end_line=None,
                                   observed_end=cutoff))
    by_tid = defaultdict(list)
    for operation in operations:
        operation.setdefault("observed_end", operation["end"])
        operation["kernel_events"] = Counter()
        operation["page_alloc"] = Counter()
        operation["compaction_status"] = Counter()
        operation["compaction_seconds"] = 0.0
        operation["direct_reclaim_seconds"] = 0.0
        operation["compaction_seconds_before_runner_sigterm"] = 0.0
        operation["direct_reclaim_seconds_before_runner_sigterm"] = 0.0
        by_tid[operation["tid"]].append(operation)
    starts = {}
    for tid, own in by_tid.items():
        own.sort(key=lambda item: item["begin"])
        starts[tid] = [item["begin"] for item in own]
        if any(a["observed_end"] > b["begin"] for a, b in zip(own, own[1:])):
            raise ValueError("same-thread reservation operation intervals overlap")
    opened = defaultdict(list)
    paired = defaultdict(list)
    orphan_ends = []
    for number, line in enumerate((stage / "kernel/trace.txt").open(), 1):
        row = kernel.parse_line(line, number)
        tid, timestamp, event, body = (row[k] for k in ("pid", "t", "event", "body"))
        if tid not in by_tid:
            continue
        if event in kernel.FAMILIES:
            family = kernel.FAMILIES[event][0]
            opened[(tid, family)].append(row)
        elif event in kernel.END_FAMILIES:
            family = kernel.END_FAMILIES[event]
            if not opened[(tid, family)]:
                orphan_ends.append(row)
            else:
                begin = opened[(tid, family)].pop()
                paired[(tid, family)].append((begin["t"], timestamp))
        operation = operation_at(by_tid[tid], starts[tid], timestamp)
        if operation is None:
            continue
        operation["kernel_events"][event] += 1
        if event == "mm_page_alloc":
            order = kernel.integer(r"order=(\d+)", body)
            pfn = int(re.search(r"pfn=0x([0-9a-f]+)", body).group(1), 16)
            operation["page_alloc"][f"order={order},pfn_nonzero={pfn != 0}"] += 1
        elif event == "mm_compaction_end":
            operation["compaction_status"][body.rsplit("status=", 1)[1]] += 1
    for (tid, family), intervals in paired.items():
        for begin, end in intervals:
            for operation in by_tid[tid]:
                if operation["begin"] > end:
                    break
                if operation["observed_end"] < begin:
                    continue
                key = family + "_seconds"
                operation[key] = operation.get(key, 0.0) + kernel.overlap(
                    begin, end, operation["begin"], operation["observed_end"])
                if cutoff is not None:
                    key = family + "_seconds_before_runner_sigterm"
                    operation[key] = operation.get(key, 0.0) + kernel.overlap(
                        begin, end, operation["begin"],
                        min(operation["observed_end"], cutoff))
    if verify_inputs(stage) != verified:
        raise ValueError("sealed inputs changed during reservation analysis")
    complete_allocs = [x for x in operations if x["event"] == "alloc" and x["end"] is not None]
    result.update(
        schema="q4t.device_reserve.kernel_analysis.v1", status="PASS",
        stage=str(stage.resolve()), sealed_manifest_sha256=verified["manifest_sha256"],
        sealed_inputs_verified=len(verified["inputs"]),
        evidence_sha256=verified["inputs"], cleanup_boundary=boundary,
        longest_completed_allocations=sorted(
            complete_allocs, key=lambda item: item["seconds"], reverse=True)[:20],
        completed_allocation_seconds=sum(x["seconds"] for x in complete_allocs),
        compaction_seconds_in_completed_allocations=sum(
            x["compaction_seconds"] for x in complete_allocs),
        direct_reclaim_seconds_in_completed_allocations=sum(
            x["direct_reclaim_seconds"] for x in complete_allocs),
        unmatched_kernel_ends=orphan_ends,
        open_kernel_intervals={str(key): value for key, value in opened.items() if value},
        limitations=[
            "PASS validates this analysis, not runtime or ownership acceptance.",
            "First logical cudaMalloc can contain the entire reservation loop; "
            "only reserve-trace alloc pairs time individual real cudaMalloc calls.",
            "Kernel text timestamps have microsecond precision; duration is elapsed time.",
            "Post-runner-SIGTERM time is cleanup, not clean startup latency.",
            "Open calls have no inferred return code or allocation duration.",
        ])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        kernel.validate_output_path(args.stage, args.output)
    except ValueError as error:
        parser.error(str(error))
    try:
        result = analyze(args.stage)
    except Exception as error:
        result = dict(schema="q4t.device_reserve.kernel_analysis.v1", status="FAILED",
                      error=f"{type(error).__name__}: {error}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
