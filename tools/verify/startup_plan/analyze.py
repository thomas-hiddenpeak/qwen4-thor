#!/usr/bin/env python3
"""Correlate sealed startup reservations with kernel and NVML observations.

This is an attribution report, not the runtime acceptance audit. It reads only
sealed inputs, supports original and sorted physical orders, and never writes
inside the stage. Pending calls are censored at the recorded runner SIGTERM.
"""

import argparse
import bisect
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "warm_reclaim"))
import analyze_kernel as kernel


SOURCES = (*kernel.SOURCES, "run/startup-plan.json",
           "resources/resources.jsonl", "resources/cgroup.jsonl",
           "resources/exit.json", "lease/nvml-queries.jsonl",
           "lease/events.jsonl", "lease/exit.json", "run/cache-launch.json",
           "run/cache-expected.json",
           "run/normal/http/isolation/identity.json",
           "run/normal/http/isolation/cleanup.json")
RESERVE_EVENTS = {"plan", "alloc", "free", "handoff", "prepared", "summary",
                  "startup_complete", "startup_incomplete", "fatal",
                  "pinned_gate_failed", "handoff_failed"}
LIMITATIONS = [
    "This report verifies attribution inputs, not runtime acceptance.",
    "Pending allocation intervals are censored at runner group SIGTERM; this "
    "does not prove the separately scoped model received SIGTERM then.",
    "Global vmstat and buddy samples are neither atomic nor model-exclusive.",
    "Only order>=4 page allocations were recorded; PFNs do not identify "
    "physical ownership, and large API bytes do not imply one contiguous block.",
    "nr_kernel_misc_reclaimable is global, not a direct NVIDIA pool inventory.",
    "Shrinker pointer hashes do not identify pool order. End retval is the "
    "final callback return, not total freed; no conversion to bytes is valid.",
    "Captured slab scan intervals omit preceding count_objects and do not "
    "bound other driver lock waits.",
    "Concurrent NVML waits and reservation calls do not identify the precise "
    "lock holder or prove benefits from reordering.",
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(), object_pairs_hook=kernel.unique_keys)


def rows(path):
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            yield number, json.loads(line, object_pairs_hook=kernel.unique_keys)


def verify(stage):
    raw = (stage / "sealed-files.json").read_bytes()
    manifest = json.loads(raw, object_pairs_hook=kernel.unique_keys)
    sources = {}
    for name in SOURCES:
        path = stage / name
        require(path.resolve().is_relative_to(stage),
                "input resolves outside stage: " + name)
        digest = kernel.sha256(path)
        require(manifest.get(name) == digest, "sealed input mismatch: " + name)
        sources[name] = digest
    return dict(manifest_sha256=hashlib.sha256(raw).hexdigest(), inputs=sources)


def read_reservations(stage, verified, cutoff):
    plan = read_json(stage / "run/startup-plan.json")
    entries = plan["allocations"]
    require(plan["count"] == len(entries), "plan count differs")
    require(all(type(e["bytes"]) is int and e["bytes"] > 0 for e in entries),
            "invalid planned allocation bytes")
    require(sum(e["bytes"] for e in entries) == plan["device_bytes"],
            "plan byte total differs")
    physical = list(range(len(entries)))
    strategy = "logical_order"
    if "physical_order" in plan:
        order = plan["physical_order"]
        require(order["strategy"] == "stable_descending_bytes",
                "unknown physical ordering strategy")
        physical = sorted(physical, key=lambda i: -entries[i]["bytes"])
        require(order["indices"] == physical, "physical order differs")
        strategy = order["strategy"]
    reserve, memory = [], []
    with (stage / "run/normal/http/server.log").open() as stream:
        for number, line in enumerate(stream, 1):
            for prefix, target in (("[reserve-trace] ", reserve),
                                   ("[memory-trace] ", memory)):
                if line.startswith(prefix):
                    row = json.loads(line[len(prefix):],
                                     object_pairs_hook=kernel.unique_keys)
                    require(row["seq"] == len(target), "trace sequence gap")
                    row["source_line"] = number
                    target.append(row)
    require(bool(reserve) and reserve[0]["event"] == "plan",
            "reservation plan event missing")
    require(reserve[0]["count"] == len(entries) and
            reserve[0]["total_bytes"] == plan["device_bytes"] and
            reserve[0]["plan_sha256"] ==
            verified["inputs"]["run/startup-plan.json"],
            "reservation plan event identity differs")
    opened = None
    operations = []
    last_time = reserve[0]["mono"]
    for row in reserve[1:]:
        require(row["event"] in RESERVE_EVENTS and row["event"] != "plan",
                "unknown or duplicate reservation event")
        require(row["mono"] >= last_time, "reservation time reversed")
        last_time = row["mono"]
        if row["event"] != "alloc":
            continue
        index = row["index"]
        require(type(index) is int and 0 <= index < len(entries),
                "invalid reservation index")
        require(row["bytes"] == entries[index]["bytes"],
                "reservation bytes differ")
        ordinal = len(operations)
        if row["phase"] == "begin":
            require(opened is None, "overlapping physical reservations")
            require(ordinal < len(physical) and physical[ordinal] == index,
                    "observed physical order differs")
            if strategy != "logical_order" or "physical_ordinal" in row:
                require(row.get("physical_ordinal") == ordinal,
                        "physical ordinal differs")
            opened = row
        elif row["phase"] == "end":
            require(opened is not None, "reservation end without begin")
            require(all(opened.get(k) == row.get(k) for k in
                        ("index", "bytes", "tid", "physical_ordinal")),
                    "reservation call identity differs")
            require(row["rc"] != 0 or row["ptr"] > 0,
                    "successful reservation has null pointer")
            operations.append(make_operation(entries, opened, row, ordinal))
            opened = None
        else:
            raise ValueError("unknown physical reservation phase")
    if opened is not None:
        require(cutoff is not None and cutoff >= opened["mono"],
                "pending reservation has no usable cleanup boundary")
        operations.append(make_operation(entries, opened, None,
                                         len(operations), cutoff))
    require(bool(operations), "no physical reservation calls")
    tids = {o["tid"] for o in operations}
    require(len(tids) == 1, "multiple reservation threads unsupported")
    pid = next(iter(tids))
    identity = read_json(stage / "run/normal/http/isolation/identity.json")
    require(identity["pid"] == pid, "reservation PID differs from scope owner")
    return plan, reserve, memory, operations, pid, identity, strategy


def make_operation(entries, begin, end, ordinal, cutoff=None):
    index = begin["index"]
    return dict(index=index, physical_ordinal=ordinal,
                name=entries[index]["name"], bytes=begin["bytes"],
                tid=begin["tid"], begin=begin["mono"],
                end=end["mono"] if end else None,
                seconds=end["mono"] - begin["mono"] if end else None,
                observed_end=end["mono"] if end else cutoff,
                rc=end["rc"] if end else None,
                begin_line=begin["source_line"],
                end_line=end["source_line"] if end else None,
                events=Counter(), alloc_orders=Counter(),
                compact_tries=Counter(), compact_status=Counter(),
                migration=Counter(), reclaimed_pages=0)


def parse_kernel(stage, operations, pid, scan, cutoff):
    starts = [o["begin"] for o in operations]
    interval_open, pairs = defaultdict(list), defaultdict(list)
    orphans, pools, pool_open, latest_try = [], [], {}, {}
    actors, names, events = defaultdict(Counter), defaultdict(set), Counter()
    model_range = [None, None]
    for number, line in enumerate((stage / "kernel/trace.txt").open(), 1):
        row = kernel.parse_line(line, number)
        p, t, event, body = (row[k] for k in ("pid", "t", "event", "body"))
        actors[p][event] += 1
        names[p].add(row["comm"])
        events[event] += 1
        if p == pid:
            if model_range[0] is None:
                model_range[0] = t
            model_range[1] = t
        if event == "mm_compaction_try_to_compact_pages":
            latest_try[p] = row
        if event in kernel.FAMILIES:
            interval_open[(p, kernel.FAMILIES[event][0])].append(
                (row, latest_try.get(p)))
        elif event in kernel.END_FAMILIES:
            family = kernel.END_FAMILIES[event]
            key = (p, family)
            if not interval_open[key]:
                orphans.append(row)
            else:
                begin, request = interval_open[key].pop()
                pairs[family].append(dict(
                    pid=p, begin=begin["t"], end=t, seconds=t-begin["t"],
                    begin_body=begin["body"], end_body=body, request=request,
                    start_line=begin["line"], end_line=number))
        if (event in ("mm_shrink_slab_start", "mm_shrink_slab_end") and
                body.startswith(scan + " ")):
            pool = body[len(scan) + 1:].split(":", 1)[0]
            key = (p, pool)
            if event.endswith("_start"):
                require(key not in pool_open, "nested shrinker scan")
                pool_open[key] = row
            else:
                begin = pool_open.pop(key, None)
                if begin is None:
                    orphans.append(row)
                else:
                    pools.append(dict(
                        pid=p, pool=pool, begin=begin["t"], end=t,
                        seconds=t-begin["t"],
                        cache_items=kernel.integer(r"cache items (\d+)",
                                                   begin["body"]),
                        final_retval=kernel.integer(r"return val (-?\d+)", body),
                        begin_body=begin["body"], end_body=body,
                        start_line=begin["line"], end_line=number))
        if p != pid:
            continue
        position = bisect.bisect_right(starts, t) - 1
        if position < 0 or t > operations[position]["observed_end"]:
            continue
        op = operations[position]
        op["events"][event] += 1
        if event == "mm_page_alloc":
            order = kernel.integer(r"order=(\d+)", body)
            match = re.search(r"pfn=0x([0-9a-f]+)", body)
            require(match is not None, "page allocation PFN absent")
            pfn = int(match.group(1), 16)
            op["alloc_orders"][f"order={order},pfn_nonzero={pfn != 0}"] += 1
        if event == "mm_compaction_try_to_compact_pages":
            op["compact_tries"][body] += 1
        if event == "mm_compaction_end":
            op["compact_status"][body.rsplit("status=", 1)[1]] += 1
        if event == "mm_compaction_migratepages":
            for key in ("nr_migrated", "nr_failed"):
                op["migration"][key] += kernel.integer(key + r"=(\d+)", body)
        if event == "mm_vmscan_direct_reclaim_end":
            op["reclaimed_pages"] += kernel.integer(r"nr_reclaimed=(\d+)", body)
    for op in operations:
        upper = op["observed_end"]
        before_cleanup = min(cutoff, upper) if cutoff is not None else upper
        for family in (v[0] for v in kernel.FAMILIES.values()):
            own = [v for v in pairs[family] if v["pid"] == pid and
                   v["end"] >= op["begin"] and v["begin"] <= upper]
            for suffix, limit in (("", upper), ("_precleanup", before_cleanup)):
                op[family + "_seconds" + suffix] = sum(
                    kernel.overlap(v["begin"], v["end"], op["begin"], limit)
                    for v in own)
            op[family + "_count"] = len(own)
        op["observed_seconds_precleanup"] = max(0, before_cleanup-op["begin"])
    return dict(events=events, model_trace_time_range=model_range,
                actors={p: dict(names=sorted(names[p]), events=hist)
                        for p, hist in actors.items()}, intervals=pairs,
                unclosed_intervals={str(key): val
                                    for key, val in interval_open.items() if val},
                orphans=orphans, pool_scan_callback=scan, pool_pairs=pools,
                pool_unclosed=list(pool_open.values()))


def kv(text):
    result = {}
    for line in text.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2:
            result[parts[0]] = int(parts[1])
    return result


def parse_resources(stage, pid, identity):
    resources, stacks, cgroups, nvml = [], [], [], []
    for number, row in rows(stage / "resources/resources.jsonl"):
        system = row["system"]
        mem = {k: v*1024 for k, v in kv(system["meminfo"]["value"]).items()}
        buddy = {}
        for line in system["buddyinfo"]["value"].splitlines():
            match = re.fullmatch(r"Node (\d+), zone\s+(\w+)\s+(.*)", line)
            require(match is not None, "unrecognized buddyinfo line")
            node, zone, counts = match.groups()
            require(node == "0", "multiple NUMA nodes need explicit aggregation")
            counts = list(map(int, counts.split()))
            buddy[zone] = dict(
                counts=counts,
                order9plus_bytes=sum(c*(4096 << i) for i, c in enumerate(counts)
                                     if i >= 9),
                all_bytes=sum(c*(4096 << i) for i, c in enumerate(counts)))
        resources.append(dict(source_line=number, mono=row["monotonic"],
                              finish=row["finished"]["monotonic"], mem=mem,
                              vmstat=kv(system["vmstat"]["value"]), buddy=buddy))
        for target in row["targets"]:
            if target["pid"] != pid and target["kind"] != "nvml":
                continue
            for task in target["tasks"]:
                if target["pid"] == pid and task["tid"] == pid:
                    require(task["starttime_ticks"] == identity["start_ticks"],
                            "model PID start ticks differ")
                stacks.append(dict(
                    mono=row["monotonic"], sample_line=number,
                    pid=target["pid"], kind=target["kind"], tid=task["tid"],
                    state=task["state"], starttime_ticks=task["starttime_ticks"],
                    wchan=task["wchan"], stack=task["stack"]))
    for _, query in rows(stage / "lease/nvml-queries.jsonl"):
        query["events"] = [json.loads(line, object_pairs_hook=kernel.unique_keys)
                           for line in query.pop("stdout").splitlines()]
        nvml.append(query)
    for number, row in rows(stage / "resources/cgroup.jsonl"):
        for group in row["groups"]:
            if "files" not in group:
                continue
            files = group["files"]
            cgroups.append(dict(
                source_line=number, mono=group["monotonic"],
                owners=group["owners"], current=int(files["memory.current"]["value"]),
                max=int(files["memory.max"]["value"]),
                events=kv(files["memory.events"]["value"]),
                stat=kv(files["memory.stat"]["value"])))
    return dict(resources=resources, stack_samples=stacks,
                cgroups=cgroups, nvml=nvml)


def analyze(stage):
    verified = verify(stage)
    capture = read_json(stage / "kernel/exit.json")
    require(capture["status"] == "COMPLETED" and
            not any(capture["loss_totals"].values()), "kernel capture incomplete")
    driver = read_json(stage / "driver-identity/driver_identity.json")
    require(driver["status"] == "PASS" and driver["attribution_valid"],
            "driver scan attribution unavailable")
    scan = driver["callbacks"]["scan"]["kernel_symbol"]["text"]
    boundary = kernel.cleanup_boundary(stage)
    cutoff = boundary.get("monotonic_approx")
    plan, reserve, memory, ops, pid, identity, strategy = read_reservations(
        stage, verified, cutoff)
    result = parse_kernel(stage, ops, pid, scan, cutoff)
    result.update(parse_resources(stage, pid, identity))
    require(verify(stage) == verified, "sealed inputs changed during analysis")
    result.update(
        status="PASS", meaning="sealed input attribution only",
        stage=str(stage), sources=verified["inputs"],
        manifest_sha256=verified["manifest_sha256"],
        parser_sha256=kernel.sha256(Path(kernel.__file__)),
        script_sha256=kernel.sha256(Path(__file__)),
        kernel_loss=capture["loss_totals"],
        kernel_instance_removed=capture["instance_removed"],
        cleanup_boundary=boundary, pid=pid, process_identity=identity,
        allocation_plan=dict(
            count=plan["count"], total_bytes=plan["device_bytes"],
            physical_strategy=strategy, completed_count=sum(
                o["end"] is not None for o in ops), successful_count=sum(
                o["rc"] == 0 for o in ops), completed_bytes=sum(
                o["bytes"] for o in ops if o["rc"] == 0),
            events=Counter(r["event"] for r in reserve),
            application_memory_trace_count=len(memory)),
        operations=ops,
        lifecycle_events=[r for r in reserve if r["event"] not in
                          ("alloc", "free", "handoff")],
        first_application_pinned=next((r for r in memory if
            r["api"] in ("cudaHostAlloc", "cudaMallocHost") and
            r["phase"] == "begin"), None),
        limitations=LIMITATIONS)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    stage, output = args.stage.resolve(), args.output.resolve()
    kernel.validate_output_path(stage, output)
    result = analyze(stage)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(dict(status="PASS", meaning=result["meaning"],
                          output=str(output), pid=result["pid"],
                          sources=len(result["sources"]),
                          operations=len(result["operations"]),
                          trace_events=sum(result["events"].values()))))


if __name__ == "__main__":
    main()
