"""Private tracefs instance for the bounded rowmax environment diagnosis.

The CLI only operates on this phase's fixed directory and nonce. It never
changes the top-level tracing configuration or an existing foreign instance.
No CUDA/NVML calls, kernel module loads, or allocator policy changes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / ".q4t-work/vision-attention-rowmax-20261107/env-02"
TRACE_ROOT = Path("/sys/kernel/tracing/instances")
FAILED_PAGE = "pfn == 18446744073709551615"
EVENT = "events/kmem/mm_page_alloc"
MAX_TRACE_BYTES = 16 * 1024 * 1024


def require(value, message):
    if not value:
        raise RuntimeError(message)


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    if os.geteuid() == 0:
        os.chown(path, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))


def identity(pid):
    proc = Path("/proc") / str(pid)
    fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    return dict(pid=pid, ppid=int(fields[1]), start_ticks=int(fields[19]),
        uid=proc.stat().st_uid,
        argv=[s.decode() for s in (proc / "cmdline").read_bytes().split(b"\0") if s])


def descendants(pid):
    records = {}
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                records[int(path.name)] = int(fields[1])
            except (FileNotFoundError, ProcessLookupError):
                pass
    owned = {pid}
    for _ in range(64):
        previous = len(owned)
        owned |= {p for p, parent in records.items() if parent in owned}
        if len(owned) == previous:
            return sorted(owned)
    raise RuntimeError("process ancestry depth")


def write(instance, name, value):
    (instance / name).write_text(value + "\n")


def instance_name(plan, guard_pid):
    nonce = plan["trace_nonce"]
    require(re.fullmatch("[0-9a-f]{16}", nonce) is not None, "invalid trace nonce")
    require(type(guard_pid) is int and guard_pid > 1, "invalid guard PID")
    return f"q4t_rowmax_{guard_pid}_{nonce}"


def configure(instance, pids):
    # Creating a tracefs instance populates its control files in the kernel.
    write(instance, "tracing_on", "0")
    write(instance, "trace_clock", "mono")
    write(instance, "buffer_size_kb", "256")
    write(instance, "options/event-fork", "1")
    write(instance, "set_event_pid", " ".join(map(str, pids)))
    write(instance, EVENT + "/filter", FAILED_PAGE)
    write(instance, EVENT + "/trigger", "stacktrace if " + FAILED_PAGE)
    write(instance, EVENT + "/enable", "1")
    write(instance, "tracing_on", "1")


def controls(instance):
    names = ("tracing_on", "trace_clock", "buffer_size_kb", "buffer_total_size_kb",
             "options/event-fork", "set_event_pid", EVENT + "/filter",
             EVENT + "/trigger", EVENT + "/enable")
    return {name: (instance / name).read_text() for name in names}


def global_controls():
    return {name: (TRACE_ROOT.parent / name).read_text() for name in
            ("tracing_on", "current_tracer", "events/enable", "set_event_pid", "trace_clock")}


def event_enabled(value):
    # event_enable_read appends '*' in soft mode, including for triggers.
    # '0*' is still disabled; unknown/mixed values must not pass.
    return value.strip() in ("1", "1*")


def setup(plan, output, guard_pid):
    guard = identity(guard_pid)
    require(guard["uid"] == int(os.environ["SUDO_UID"]) and
            str(ROOT / "tools/evalscope/gpu_exclusive.py") in guard["argv"],
            "not this user's repository GPU guard")
    require(os.getpid() in descendants(guard_pid), "helper is not a guard descendant")
    instance = TRACE_ROOT / instance_name(plan, guard_pid)
    global_before = global_controls()
    # Never reuse or reset a preexisting instance, even with a matching name.
    instance.mkdir()
    save(output / "owner.json", dict(instance=str(instance), guard=guard,
        nonce=plan["trace_nonce"], creator_pid=os.getpid(), global_before=global_before))
    try:
        configure(instance, descendants(guard_pid))
        observed = controls(instance)
        save(output / "observed.json", observed)
        require(observed["tracing_on"].strip() == "1" and
                observed["options/event-fork"].strip() == "1" and
                event_enabled(observed[EVENT + "/enable"]) and
                "[mono]" in observed["trace_clock"], "trace controls not applied")
        save(output / "configured.json", dict(status="CONFIGURED", controls=observed))
    except BaseException:
        # Owner receipt remains so the stage can collect/retire a partial setup.
        write(instance, "tracing_on", "0")
        raise
    return "CONFIGURED"


def collect(plan, output, guard_pid):
    owner = read(output / "owner.json")
    instance = TRACE_ROOT / instance_name(plan, guard_pid)
    require(owner["instance"] == str(instance) and owner["nonce"] == plan["trace_nonce"]
            and owner["guard"]["pid"] == guard_pid, "foreign trace ownership")
    require(instance.is_dir() and not instance.is_symlink(), "trace instance missing/aliased")
    write(instance, "tracing_on", "0")
    write(instance, EVENT + "/enable", "0")
    observed = controls(instance)
    with (instance / "trace").open("rb") as stream:
        data = stream.read(MAX_TRACE_BYTES + 1)
    target = output / "trace.txt"
    with target.open("xb") as stream:
        stream.write(data[:MAX_TRACE_BYTES])
    if os.geteuid() == 0:
        os.chown(target, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
    stats = {p.parent.name: p.read_text() for p in (instance / "per_cpu").glob("cpu*/stats")}
    require(stats, "trace CPU statistics missing")
    # Instance removal retires its event triggers too; no global trigger edits.
    instance.rmdir()
    result = dict(status="CAPTURED_AND_REMOVED", instance=str(instance), controls=observed,
                  cpu_stats=stats, bytes=len(data), truncated=len(data) > MAX_TRACE_BYTES,
                  global_unchanged=global_controls() == owner["global_before"],
                  scope="Guard descendants only; unrelated asynchronous kernel workers excluded")
    save(output / "collected.json", result)
    return result["status"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "collect"))
    parser.add_argument("--guard-pid", type=int, required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0 and "SUDO_UID" in os.environ, "run via sudo -n")
    plan = read(STAGE / "plan.json")
    require(plan["profile"] == "vision-rowmax-allocator-diagnosis-v1", "wrong phase")
    actual = hashlib.sha256((TRACE_ROOT.parent / (EVENT + "/format")).read_bytes()).hexdigest()
    require(actual == plan["trace_format_sha256"], "kernel trace format changed")
    output = STAGE / "execution/allocator-trace"
    require(output.is_dir() and output.resolve() == output, "invalid trace output")
    print(json.dumps(dict(event="helper_start", identity=identity(os.getpid()))), flush=True)
    action = setup if args.action == "setup" else collect
    print(json.dumps(dict(status=action(plan, output, args.guard_pid))))


if __name__ == "__main__":
    main()
