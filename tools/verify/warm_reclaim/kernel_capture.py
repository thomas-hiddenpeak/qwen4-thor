#!/usr/bin/env python3
"""Bounded, owned tracefs instance for reclaim/compaction diagnostics.

Run as root. This changes only a newly created tracefs instance, never the
global tracer or existing instances. Stop with --stop-file or SIGTERM. A time
or byte budget, lost events, or incomplete cleanup makes exit.json FAILED.
No GPU API, model data, sysctl, shrinker scan, or reclaim operation is used.
"""

import argparse
import errno
import json
import os
from pathlib import Path
import re
import selectors
import signal
import sys
import time
import uuid


TRACE_ROOT = Path("/sys/kernel/tracing")
LOSS_FIELDS = ("overrun", "commit overrun", "dropped events")
REQUIRED_EVENTS = {
    "vmscan/mm_shrink_slab_start": ("shr", "shrink", "cache_items"),
    "vmscan/mm_shrink_slab_end": ("shr", "shrink", "retval"),
    "vmscan/mm_vmscan_direct_reclaim_begin": ("order", "gfp_flags"),
    "vmscan/mm_vmscan_direct_reclaim_end": ("nr_reclaimed",),
    "compaction/mm_compaction_begin": ("sync", "zone_start"),
    "compaction/mm_compaction_end": ("status",),
    "compaction/mm_compaction_try_to_compact_pages": ("order",),
    "kmem/mm_page_alloc": ("pfn", "order", "gfp_flags"),
}
OPTIONAL_EVENTS = (
    "vmscan/mm_vmscan_memcg_reclaim_begin",
    "vmscan/mm_vmscan_memcg_reclaim_end",
    "vmscan/mm_vmscan_node_reclaim_begin",
    "vmscan/mm_vmscan_node_reclaim_end",
    "vmscan/mm_vmscan_lru_shrink_inactive",
    "vmscan/mm_vmscan_lru_shrink_active",
    "vmscan/mm_vmscan_kswapd_wake",
    "vmscan/mm_vmscan_kswapd_sleep",
    "compaction/mm_compaction_isolate_freepages",
    "compaction/mm_compaction_isolate_migratepages",
    "compaction/mm_compaction_migratepages",
    "migrate/mm_migrate_pages_start",
    "migrate/mm_migrate_pages",
)


class CaptureFailure(RuntimeError):
    """Failure that must invalidate the timeline rather than truncate it."""


def save_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def write_control(path, value):
    with path.open("w", encoding="ascii") as stream:
        stream.write(str(value) + "\n")


def format_fields(value):
    fields = set()
    for declaration in re.findall(r"field:([^;]+);", value):
        match = re.search(r"([A-Za-z_][A-Za-z_0-9]*)(?:\[[^]]*\])?$",
                          declaration.strip())
        if match:
            fields.add(match.group(1))
    return fields


def discover_events(root):
    selected = {}
    for name, expected in REQUIRED_EVENTS.items():
        path = root / "events" / name / "format"
        if not path.is_file():
            raise CaptureFailure("missing required event: " + name)
        value = path.read_text()
        missing = set(expected) - format_fields(value)
        if missing:
            raise CaptureFailure(f"{name}: missing fields {sorted(missing)}")
        selected[name] = value
    missing_optional = []
    for name in OPTIONAL_EVENTS:
        path = root / "events" / name / "format"
        if path.is_file():
            selected[name] = path.read_text()
        else:
            missing_optional.append(name)
    return selected, missing_optional


def parse_stats(value):
    result = {}
    for line in value.splitlines():
        if ":" not in line:
            continue
        key, entry = line.split(":", 1)
        entry = entry.strip()
        try:
            result[key.strip()] = int(entry)
        except ValueError:
            result[key.strip()] = entry
    missing = set(LOSS_FIELDS) - result.keys()
    if missing or any(not isinstance(result[key], int)
                      for key in LOSS_FIELDS):
        raise CaptureFailure("invalid per-CPU loss counters: " + value)
    return result


def read_stats(instance):
    result = {}
    for path in sorted((instance / "per_cpu").glob("cpu*/stats")):
        raw = path.read_text()
        result[path.parent.name] = {"counters": parse_stats(raw), "raw": raw}
    if not result:
        raise CaptureFailure("no per-CPU trace statistics")
    return result


def loss_totals(stats):
    return {key: sum(cpu["counters"][key] for cpu in stats.values())
            for key in LOSS_FIELDS}


def global_configuration(root):
    result = {}
    for name in ("current_tracer", "tracing_on", "trace_clock",
                 "buffer_size_kb", "events/enable"):
        try:
            result[name] = (root / name).read_text().strip()
        except OSError as error:
            result[name] = {"read_error": str(error)}
    return result


class ByteBudget:
    def __init__(self, limit):
        self.limit = limit
        self.written = 0
        self.unwritten_at_least = 0

    def append(self, stream, data):
        remaining = self.limit - self.written
        chunk = data[:remaining]
        pending = memoryview(chunk)
        while pending:
            count = stream.write(pending)
            if not count:
                raise CaptureFailure("trace write made no progress")
            self.written += count
            pending = pending[count:]
        self.unwritten_at_least += len(data) - len(chunk)
        if self.written >= self.limit:
            raise CaptureFailure("trace byte limit reached")


def capture(args):
    output = args.output.resolve()
    # Evidence is never overwritten, including a failed preparation.
    output.mkdir(parents=True, exist_ok=False)
    instance_name = f"q4t-warm-reclaim-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    instance = TRACE_ROOT / "instances" / instance_name
    ready_file = args.ready_file or output / "ready.json"
    terminal = {
        "schema": 1, "status": "FAILED", "reason": "not_started",
        "pid": os.getpid(), "instance": str(instance),
        "instance_removed": False, "max_seconds": args.max_seconds,
        "max_bytes": args.max_bytes, "buffer_kb_per_cpu": args.buffer_kb,
        "started_monotonic_ns": time.monotonic_ns(),
        "started_realtime_ns": time.time_ns(), "cleanup_errors": [],
        "limitations": [
            "Whole-system events; attribution requires PID/shrinker analysis.",
            "Only page allocations of order >= 4 are recorded.",
            "Shrinker retval is its final scan result, not total reclaimed.",
            "SIGKILL or machine failure cannot guarantee instance removal.",
        ],
    }
    owned = False
    enabled = False
    trace_fd = None
    trace_stream = None
    metadata = None
    selector = None
    stop_signal = None
    previous_handlers = {}
    budget = ByteBudget(args.max_bytes)

    def signal_handler(number, _frame):
        nonlocal stop_signal
        stop_signal = signal.Signals(number).name

    def record(kind, **values):
        entry = {"kind": kind, "monotonic_ns": time.monotonic_ns(),
                 "realtime_ns": time.time_ns(), **values}
        metadata.write(json.dumps(entry) + "\n")
        metadata.flush()
        return entry

    def marker(kind):
        before = time.monotonic_ns()
        wall = time.time_ns()
        text = (f"Q4T_WARM_RECLAIM {kind} mono_ns={before} "
                f"realtime_ns={wall}")
        write_control(instance / "trace_marker", text)
        return record("marker", label=kind, before_monotonic_ns=before,
                      after_monotonic_ns=time.monotonic_ns(), text=text)

    def sample_stats(label):
        stats = read_stats(instance)
        losses = loss_totals(stats)
        record("per_cpu_stats", label=label, cpus=stats, losses=losses)
        terminal["loss_totals"] = losses
        if any(losses.values()):
            raise CaptureFailure("trace events lost: " + json.dumps(losses))
        return stats

    def read_available(max_chunks=16):
        chunks = 0
        for _ in range(max_chunks):
            try:
                chunk = os.read(trace_fd, 65536)
            except BlockingIOError:
                break
            except OSError as error:
                if error.errno == errno.EINTR:
                    continue
                raise
            if not chunk:
                break
            budget.append(trace_stream, chunk)
            chunks += 1
        return chunks

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, signal_handler)
        if args.stop_file.exists():
            raise CaptureFailure("stop-file already exists before capture")
        if ready_file.exists():
            raise CaptureFailure("ready-file already exists")
        if os.geteuid() != 0:
            raise CaptureFailure("tracefs capture requires root")
        terminal["global_before"] = global_configuration(TRACE_ROOT)
        events, missing_optional = discover_events(TRACE_ROOT)
        clocks = (TRACE_ROOT / "trace_clock").read_text().replace("[", "")
        if "mono" not in clocks.replace("]", "").split():
            raise CaptureFailure("tracefs mono clock unavailable")
        formats = output / "formats"
        formats.mkdir()
        for name, value in events.items():
            (formats / (name.replace("/", "__") + ".txt")).write_text(value)
        save_json(output / "events.json", {
            "required": REQUIRED_EVENTS, "selected": list(events),
            "optional_missing": missing_optional,
            "filters": {"kmem/mm_page_alloc": "order >= 4"}})
        instance.mkdir()  # Never use exist_ok: only this process owns it.
        owned = True
        write_control(instance / "tracing_on", 0)
        write_control(instance / "current_tracer", "nop")
        write_control(instance / "trace_clock", "mono")
        write_control(instance / "buffer_size_kb", args.buffer_kb)
        write_control(instance / "options" / "overwrite", 1)
        write_control(instance / "options" / "context-info", 1)
        write_control(instance / "options" / "record-tgid", 1)
        for name in events:
            if name == "kmem/mm_page_alloc":
                write_control(instance / "events" / name / "filter",
                              "order >= 4")
            write_control(instance / "events" / name / "enable", 1)
        terminal["configuration"] = global_configuration(instance)
        terminal["configuration"]["page_alloc_filter"] = (
            instance / "events/kmem/mm_page_alloc/filter").read_text().strip()
        if "[mono]" not in terminal["configuration"]["trace_clock"]:
            raise CaptureFailure("mono trace clock was not selected")
        if re.sub(r"\s", "", terminal["configuration"]["page_alloc_filter"]) != (
                "order>=4"):
            raise CaptureFailure("page allocation order filter did not apply")
        for name in events:
            if (instance / "events" / name / "enable").read_text().strip() != "1":
                raise CaptureFailure("event did not enable: " + name)
        trace_stream = (output / "trace.txt").open("wb", buffering=0)
        metadata = (output / "timeline.jsonl").open("w", encoding="utf-8")
        trace_fd = os.open(instance / "trace_pipe", os.O_RDONLY | os.O_NONBLOCK)
        selector = selectors.DefaultSelector()
        selector.register(trace_fd, selectors.EVENT_READ)
        terminal["initial_stats"] = sample_stats("initial")
        terminal["cpu_count"] = len(terminal["initial_stats"])
        write_control(instance / "tracing_on", 1)
        enabled = True
        marker("START")
        terminal["capture_started_monotonic_ns"] = time.monotonic_ns()
        save_json(ready_file, {"status": "READY", "pid": os.getpid(),
                              "instance": str(instance), "clock": "mono",
                              "monotonic_ns": time.monotonic_ns(),
                              "max_seconds": args.max_seconds,
                              "max_bytes": args.max_bytes})
        deadline = time.monotonic() + args.max_seconds
        next_sample = time.monotonic() + 1.0
        while True:
            if stop_signal:
                terminal.update(status="COMPLETED", reason=stop_signal)
                break
            if args.stop_file.exists():
                terminal.update(status="COMPLETED", reason="stop_file")
                break
            now = time.monotonic()
            if now >= deadline:
                raise CaptureFailure("capture time limit reached")
            if now >= next_sample:
                marker("HEARTBEAT")
                sample_stats("periodic")
                os.fsync(trace_stream.fileno())
                os.fsync(metadata.fileno())
                next_sample = now + 1.0
            selector.select(timeout=min(0.1, deadline - now))
            read_available()
    except Exception as error:
        terminal.update(status="FAILED", reason=f"{type(error).__name__}: {error}")
    finally:
        def cleanup_failure(operation, error):
            terminal["cleanup_errors"].append(f"{operation}: {error}")
            terminal["status"] = "FAILED"

        if owned:
            if enabled:
                try:
                    marker("END")
                except Exception as error:
                    cleanup_failure("end marker", error)
            try:
                write_control(instance / "tracing_on", 0)
            except Exception as error:
                cleanup_failure("disable tracing", error)
            if trace_fd is not None:
                try:
                    drain_deadline = time.monotonic() + 3.0
                    while read_available():
                        if time.monotonic() >= drain_deadline:
                            raise CaptureFailure("final drain time limit reached")
                except Exception as error:
                    cleanup_failure("final drain", error)
            try:
                if metadata:
                    terminal["final_stats"] = sample_stats("final")
                else:
                    terminal["final_stats"] = read_stats(instance)
                if any(cpu["counters"].get("entries", 0)
                       for cpu in terminal["final_stats"].values()):
                    raise CaptureFailure("undrained trace entries remain")
            except Exception as error:
                cleanup_failure("final statistics", error)
        if selector:
            selector.close()
        if trace_fd is not None:
            os.close(trace_fd)
        for stream in (trace_stream, metadata):
            if stream:
                try:
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.close()
                except Exception as error:
                    cleanup_failure("persist evidence", error)
        if owned:
            try:
                # tracefs removes its virtual children; do not recurse/unlink.
                instance.rmdir()
                terminal["instance_removed"] = not instance.exists()
            except Exception as error:
                cleanup_failure("remove owned instance", error)
        terminal["instance_created"] = owned
        terminal["trace_bytes"] = budget.written
        terminal["trace_unwritten_at_least"] = budget.unwritten_at_least
        terminal["ended_monotonic_ns"] = time.monotonic_ns()
        terminal["ended_realtime_ns"] = time.time_ns()
        terminal["global_after"] = global_configuration(TRACE_ROOT)
        terminal["global_configuration_equal"] = (
            terminal.get("global_before") == terminal["global_after"])
        save_json(output / "exit.json", terminal)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0 if terminal["status"] == "COMPLETED" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--max-seconds", type=float, default=180)
    parser.add_argument("--max-bytes", type=int, default=512 << 20)
    parser.add_argument("--buffer-kb", type=int, default=4096)
    args = parser.parse_args()
    if not 0 < args.max_seconds <= 900:
        parser.error("--max-seconds must be in (0, 900]")
    if not 1024 <= args.max_bytes <= 2 << 30:
        parser.error("--max-bytes must be in [1024, 2147483648]")
    if not 64 <= args.buffer_kb <= 16384:
        parser.error("--buffer-kb must be in [64, 16384] per CPU")
    return capture(args)


if __name__ == "__main__":
    sys.exit(main())
