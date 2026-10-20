#!/usr/bin/env python3
"""Analyze one sealed warm-reclaim stage; never treat cleanup as normal work."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re


LINE = re.compile(
    r"^\s*(.+)-(\d+)\s+\(([^)]*)\)\s+\[(\d+)\]\s+\S+\s+"
    r"(\d+\.\d+): (\w+): (.*)$")
FAMILIES = {
    "mm_compaction_begin": ("compaction", "mm_compaction_end"),
    "mm_vmscan_direct_reclaim_begin":
        ("direct_reclaim", "mm_vmscan_direct_reclaim_end"),
    "mm_vmscan_memcg_reclaim_begin":
        ("memcg_reclaim", "mm_vmscan_memcg_reclaim_end"),
}
END_FAMILIES = {value[1]: value[0] for value in FAMILIES.values()}
SOURCES = ("kernel/trace.txt", "kernel/exit.json", "kernel/timeline.jsonl",
           "driver-identity/driver_identity.json", "run/normal/http/server.log",
           "run/normal-process.json", "state.json")


def parse_line(line, number):
    match = LINE.match(line)
    if not match:
        raise ValueError(f"unrecognized trace line {number}: {line[:160]}")
    comm, pid, tgid, cpu, stamp, event, body = match.groups()
    return dict(line=number, comm=comm, pid=int(pid), tgid=tgid.strip(),
                cpu=int(cpu), t=float(stamp), event=event, body=body)


def integer(pattern, body):
    match = re.search(pattern, body)
    if not match:
        raise ValueError(f"missing field {pattern}: {body}")
    return int(match.group(1))


def overlap(begin, end, lower, upper):
    return max(0.0, min(end, upper) - max(begin, lower))


def window_end(window):
    if "observed_through_runner_sigterm" in window:
        return window["observed_through_runner_sigterm"]
    return window["call"]["end"]


def make_window(call):
    return dict(call=call, events=Counter(), page_alloc=Counter(),
                compaction_statuses=Counter(),
                compaction_migration_reported_pages=Counter(),
                direct_reclaimed_reported_pages=0)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate sealed manifest entry: " + key)
        result[key] = value
    return result


def verify_sealed_inputs(stage):
    stage = stage.resolve()
    raw = (stage / "sealed-files.json").read_bytes()
    manifest = json.loads(raw, object_pairs_hook=unique_keys)
    if not isinstance(manifest, dict):
        raise ValueError("sealed manifest must be a path/hash mapping")
    verified = {}
    for name in SOURCES:
        expected = manifest.get(name)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("missing or invalid sealed input hash: " + name)
        path = stage / name
        if not path.resolve().is_relative_to(stage):
            raise ValueError("sealed input resolves outside stage: " + name)
        actual = sha256(path)
        if actual != expected:
            raise ValueError("sealed input hash mismatch: " + name)
        verified[name] = actual
    return dict(manifest_sha256=hashlib.sha256(raw).hexdigest(), inputs=verified)


def validate_output_path(stage, output):
    if output.resolve().is_relative_to(stage.resolve()):
        raise ValueError("output must be outside the sealed stage")
    if output.exists() or output.is_symlink():
        raise ValueError("output already exists; evidence will not be overwritten")


def read_application(path):
    opened = {}
    completed = []
    sequence = 0
    for line in path.read_text().splitlines():
        if not line.startswith("[memory-trace] "):
            continue
        entry = json.loads(line.split(" ", 1)[1])
        if entry["seq"] != sequence:
            raise ValueError("application trace sequence gap")
        sequence += 1
        key = entry["call"]
        if entry["phase"] == "begin":
            if key in opened:
                raise ValueError("duplicate application call begin")
            opened[key] = entry
        elif entry["phase"] == "end":
            begin = opened.pop(key)
            if (begin["api"], begin["tid"]) != (entry["api"], entry["tid"]):
                raise ValueError("application begin/end identity differs")
            completed.append(dict(begin, end=entry["mono"], rc=entry["rc"],
                                  seconds=entry["mono"] - begin["mono"]))
        else:
            raise ValueError("unknown application trace phase")
    if not completed:
        raise ValueError("no completed application calls")
    model_pid = Counter(x["tid"] for x in completed).most_common(1)[0][0]
    return completed, list(opened.values()), model_pid


def cleanup_boundary(stage):
    process = json.loads((stage / "run/normal-process.json").read_text())
    signals = [x for x in process["signals"] if x["signal"] == "SIGTERM"]
    if not signals:
        return {"available": False, "reason": "no runner SIGTERM recorded"}
    state = json.loads((stage / "state.json").read_text())
    offset = state["started"]["wall"] - state["started"]["monotonic"]
    offsets = []
    for line in (stage / "kernel/timeline.jsonl").read_text().splitlines():
        item = json.loads(line)
        if item["kind"] == "marker":
            match = re.search(r"mono_ns=(\d+) realtime_ns=(\d+)", item["text"])
            offsets.append((int(match.group(2)) - int(match.group(1))) / 1e9)
    return {
        "available": True, "source": "run/normal-process.json first SIGTERM",
        "wall_seconds": signals[0]["t"],
        "wall_minus_mono_from_state_started": offset,
        "monotonic_approx": signals[0]["t"] - offset,
        "marker_wall_minus_mono_min": min(offsets),
        "marker_wall_minus_mono_max": max(offsets),
        "meaning": "Runner group cleanup boundary; does not prove the model "
                   "in another scope received SIGTERM at exactly that instant.",
    }


def analyze(stage):
    verified = verify_sealed_inputs(stage)
    exit_result = json.loads((stage / "kernel/exit.json").read_text())
    if (exit_result["status"] != "COMPLETED" or
            not exit_result["instance_removed"] or
            any(exit_result["loss_totals"].values())):
        raise ValueError("kernel capture incomplete, lost events, or not cleaned")
    identity = json.loads((stage / "driver-identity/driver_identity.json").read_text())
    if identity["status"] != "PASS" or not identity["attribution_valid"]:
        raise ValueError("driver callback identity not verified")
    callback = identity["callbacks"]["scan"]
    scan_symbol = callback["kernel_symbol"]["text"]
    calls, unclosed_calls, model_pid = read_application(
        stage / "run/normal/http/server.log")
    boundary = cleanup_boundary(stage)
    cutoff = boundary.get("monotonic_approx")
    windows = []
    for call in sorted(calls, key=lambda x: x["seconds"], reverse=True):
        if call["api"] != "cudaMalloc" or call["seconds"] < 0.05:
            continue
        windows.append(make_window(call))
    if cutoff is not None:
        for call in unclosed_calls:
            if call["api"] != "cudaMalloc" or call["mono"] >= cutoff:
                continue
            window = make_window(call)
            window["observed_through_runner_sigterm"] = cutoff
            window["observed_precleanup_seconds"] = cutoff - call["mono"]
            window["api_return_observed"] = False
            windows.append(window)
    events = Counter()
    by_pid = defaultdict(Counter)
    names = defaultdict(set)
    pool_open = {}
    pool_pairs = []
    pool_orphans = []
    interval_open = defaultdict(list)
    intervals = defaultdict(list)
    interval_orphans = []
    latest_order = {}
    order_tries = Counter()
    model_page_orders = Counter()
    event_count = 0
    with (stage / "kernel/trace.txt").open() as stream:
        for number, line in enumerate(stream, 1):
            entry = parse_line(line, number)
            pid, stamp, event, body = (entry[k] for k in
                                       ("pid", "t", "event", "body"))
            event_count += 1
            events[event] += 1
            by_pid[pid][event] += 1
            names[pid].add(entry["comm"])
            if event == "mm_compaction_try_to_compact_pages":
                latest_order[pid] = entry
                if pid == model_pid:
                    order_tries[body] += 1
            if event in FAMILIES:
                family = FAMILIES[event][0]
                interval_open[(pid, family)].append((entry, latest_order.get(pid)))
            elif event in END_FAMILIES:
                family = END_FAMILIES[event]
                stack = interval_open[(pid, family)]
                if not stack:
                    interval_orphans.append(entry)
                else:
                    begin, request = stack.pop()
                    intervals[family].append(dict(
                        pid=pid, begin=begin["t"], end=stamp,
                        seconds=stamp - begin["t"], start_body=begin["body"],
                        end_body=body, last_try=request,
                        start_line=begin["line"], end_line=number))
            if event in ("mm_shrink_slab_start", "mm_shrink_slab_end") and (
                    body.startswith(scan_symbol + " ")):
                pool_id = body[len(scan_symbol) + 1:].split(":", 1)[0]
                key = (pid, pool_id)
                if event.endswith("_start"):
                    if key in pool_open:
                        raise ValueError("overlapping pool shrink start")
                    pool_open[key] = entry
                else:
                    begin = pool_open.pop(key, None)
                    if begin is None:
                        pool_orphans.append(entry)
                    else:
                        pool_pairs.append(dict(
                            pid=pid, shr=pool_id, begin=begin["t"], end=stamp,
                            seconds=stamp - begin["t"],
                            cache_items=integer(r"cache items (\d+)", begin["body"]),
                            final_scan_retval=integer(r"return val (-?\d+)", body),
                            start_line=begin["line"], end_line=number,
                            start_body=begin["body"], end_body=body))
            if pid != model_pid:
                continue
            if event == "mm_page_alloc":
                model_page_orders[integer(r"order=(\d+)", body)] += 1
            for window in windows:
                if not window["call"]["mono"] <= stamp <= window_end(window):
                    continue
                window["events"][event] += 1
                if event == "mm_page_alloc":
                    order = integer(r"order=(\d+)", body)
                    pfn = int(re.search(r"pfn=0x([0-9a-f]+)", body).group(1), 16)
                    window["page_alloc"][f"order={order},pfn_nonzero={pfn != 0}"] += 1
                elif event == "mm_compaction_end":
                    window["compaction_statuses"][body.rsplit("status=", 1)[1]] += 1
                elif event == "mm_compaction_migratepages":
                    for key in ("nr_migrated", "nr_failed"):
                        window["compaction_migration_reported_pages"][key] += (
                            integer(key + r"=(\d+)", body))
                elif event == "mm_vmscan_direct_reclaim_end":
                    window["direct_reclaimed_reported_pages"] += integer(
                        r"nr_reclaimed=(\d+)", body)
    for window in windows:
        begin, end = window["call"]["mono"], window_end(window)
        matching_pools = [p for p in pool_pairs
                          if begin <= p["begin"] and p["end"] <= end]
        window["pool_pairs_all_threads"] = len(matching_pools)
        window["pool_max_cache_items"] = max(
            (p["cache_items"] for p in matching_pools), default=None)
        window["pool_max_final_retval"] = max(
            (p["final_scan_retval"] for p in matching_pools), default=None)
        if cutoff is not None:
            window["before_runner_sigterm_seconds"] = overlap(
                begin, end, begin, cutoff)
            window["after_runner_sigterm_seconds"] = overlap(begin, end, cutoff, end)
        for family, values in intervals.items():
            own = [v for v in values if v["pid"] == model_pid]
            window[family + "_count"] = sum(
                begin <= v["begin"] and v["end"] <= end for v in own)
            window[family + "_seconds"] = sum(
                overlap(v["begin"], v["end"], begin, end) for v in own)
            if cutoff is not None:
                window[family + "_seconds_before_runner_sigterm"] = sum(
                    overlap(v["begin"], v["end"], begin, min(cutoff, end))
                    for v in own)
    pool_groups = defaultdict(list)
    for pool in pool_pairs:
        pool_groups[pool["shr"]].append(pool)
    pool_summary = {}
    for key, values in pool_groups.items():
        pool_summary[key] = dict(
            count=len(values), first=values[0], last=values[-1],
            max_cache_items=max(v["cache_items"] for v in values),
            min_cache_items=min(v["cache_items"] for v in values),
            final_return_histogram=Counter(v["final_scan_retval"] for v in values),
            max_duration_seconds=max(v["seconds"] for v in values),
            by_pid=Counter(v["pid"] for v in values))
    paired_summary = {}
    for family, values in intervals.items():
        own = [v for v in values if v["pid"] == model_pid]
        paired_summary[family] = dict(
            count=len(values), model_count=len(own),
            model_seconds=sum(v["seconds"] for v in own),
            model_longest=sorted(own, key=lambda v: v["seconds"], reverse=True)[:20])
        if cutoff is not None:
            boundary[family + "_model_seconds_before_runner_sigterm"] = sum(
                max(0, min(v["end"], cutoff) - v["begin"]) for v in own)
    # Recheck after parsing so a changed input/manifest cannot produce PASS.
    if verify_sealed_inputs(stage) != verified:
        raise ValueError("sealed inputs or manifest changed during analysis")
    return dict(
        schema="q4t.warm_reclaim.kernel_analysis.v1", status="PASS",
        stage=str(stage.resolve()), evidence_sha256=verified["inputs"],
        sealed_manifest_sha256=verified["manifest_sha256"],
        sealed_inputs_verified=len(verified["inputs"]),
        event_count=event_count, event_histogram=events, unparsed_lines=[],
        by_pid={pid: dict(names=sorted(names[pid]), events=counts)
                for pid, counts in by_pid.items()},
        model_pid=model_pid, model_begin=min(c["mono"] for c in calls),
        model_event_histogram=by_pid[model_pid],
        model_high_alloc_order_counts=model_page_orders,
        model_compact_try_histogram=order_tries,
        driver_identity_attribution=callback, pool_pair_count=len(pool_pairs),
        pool_unmatched=pool_orphans, pool_unclosed=list(pool_open.values()),
        pool_summary=pool_summary,
        pool_longest=sorted(pool_pairs, key=lambda v: v["seconds"], reverse=True)[:20],
        paired_summary=paired_summary,
        unclosed_intervals={str(k): v for k, v in interval_open.items() if v},
        orphan_interval_ends=interval_orphans,
        long_alloc_windows=[w for w in windows if "end" in w["call"]],
        uncompleted_alloc_precleanup_windows=[w for w in windows
                                              if "end" not in w["call"]],
        unclosed_app_calls=unclosed_calls,
        cleanup_boundary=boundary,
        limitations=[
            "Trace text timestamps have microsecond precision; durations are "
            "elapsed time, not CPU time.",
            "Pool identities are stable hashed pointers, not pool orders. "
            "Object counts cannot be converted to bytes without order.",
            "Shrinker end retval is final scan result, not accumulated freed "
            "objects; scan below batch threshold may never call the callback.",
            "Recorded shrink intervals exclude count_objects before start; "
            "short intervals do not prove all driver pool locks were fast.",
            "A pool disappearing from traces does not directly prove exhaustion "
            "or physical release. Count/scan callbacks were not instrumented.",
            "Migration counters may revisit pages; sums are not unique pages.",
            "Runner cleanup boundary does not prove separate-scope model signal "
            "delivery time. Post-boundary durations are not clean-start latency.",
        ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        validate_output_path(args.stage, args.output)
    except ValueError as error:
        parser.error(str(error))
    try:
        result = analyze(args.stage)
    except Exception as error:
        result = dict(schema="q4t.warm_reclaim.kernel_analysis.v1", status="FAILED",
                      error=f"{type(error).__name__}: {error}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
