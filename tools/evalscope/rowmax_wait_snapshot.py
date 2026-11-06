"""Read-only, owned-process wait snapshots for the frozen rowmax diagnosis."""
import argparse
import json
import os
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[2]
PHASE = ROOT / ".q4t-work/vision-attention-rowmax-20261107/wait-02"
PROC = Path("/proc")
QUERY = str(ROOT / "tools/evalscope/nvml_process_query.py")
GUARD = str(ROOT / "tools/evalscope/gpu_exclusive.py")
MAX_FILE_BYTES = 8192
MAX_THREADS = 64
THRESHOLDS = (2.0, 4.0)


def identity(proc, pid):
    path = proc / str(pid)
    stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
    return dict(pid=pid, ppid=int(stat[1]), start_ticks=int(stat[19]),
                state=stat[0], uid=path.stat().st_uid,
                argv=[x.decode() for x in (path / "cmdline").read_bytes().split(b"\0") if x])


def same_identity(left, right):
    return all(left[key] == right[key] for key in ("pid", "ppid", "start_ticks", "uid", "argv"))


def is_query(value, guard):
    argv = value["argv"]
    return (value["ppid"] == guard["pid"] and value["uid"] == guard["uid"] and
            len(argv) == 5 and argv[1:4] == ["-B", QUERY, "--worker"] and
            re.fullmatch(r"[0-9a-f]{32}", argv[4]) is not None)


def matching_queries(proc, guard):
    # The installed Tegra kernel omits /proc/PID/task/TID/children. Read
    # stat ancestry first; read argv only for direct children of this guard.
    result = []
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if int(stat[1]) != guard["pid"]:
                continue
            value = identity(proc, int(path.name))
            if is_query(value, guard):
                result.append(value)
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


def snapshot(proc, expected):
    before = identity(proc, expected["pid"])
    if not same_identity(before, expected):
        raise RuntimeError("snapshot target identity changed")
    path = proc / str(expected["pid"])
    tids = sorted(int(p.name) for p in (path / "task").iterdir() if p.name.isdigit())
    rows = []
    for tid in tids[:MAX_THREADS]:
        row = dict(tid=tid, files={}, errors={}, truncated=[])
        for name in ("stat", "status", "wchan", "stack", "syscall"):
            try:
                with (path / "task" / str(tid) / name).open("rb") as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                row["files"][name] = data[:MAX_FILE_BYTES].decode(errors="replace")
                if len(data) > MAX_FILE_BYTES:
                    row["truncated"].append(name)
            except OSError as error:
                row["errors"][name] = repr(error)
        rows.append(row)
    try:
        after = identity(proc, expected["pid"])
        stable = same_identity(before, after)
    except OSError:
        after, stable = None, False
    return dict(identity_before=before, identity_after=after, stable_identity=stable,
                threads_total=len(tids), threads_truncated=len(tids) > MAX_THREADS,
                threads=rows)


class Tracker:
    """At most two snapshots of the first slow owned query, never a retry."""

    def __init__(self):
        self.first_seen = {}
        self.selected = None
        self.next_index = 0

    def due(self, values, now):
        alive = {(v["pid"], v["start_ticks"]): v for v in values}
        self.first_seen = {key: self.first_seen.get(key, now) for key in alive}
        if self.selected is None:
            for key, seen in self.first_seen.items():
                if now - seen >= THRESHOLDS[0]:
                    self.selected = key
                    break
        key = self.selected
        if key not in alive or self.next_index >= len(THRESHOLDS):
            return None
        age = now - self.first_seen[key]
        threshold = THRESHOLDS[self.next_index]
        if age < threshold:
            return None
        self.next_index += 1
        return alive[key], dict(observed_age_seconds=age, threshold_seconds=threshold)


def emit(**value):
    print(json.dumps(dict(monotonic=time.monotonic(), unix_seconds=time.time(), **value)),
          flush=True)


def observe(guard_pid):
    if os.geteuid() != 0 or "SUDO_UID" not in os.environ:
        raise RuntimeError("run through sudo -n")
    uid = int(os.environ["SUDO_UID"])
    guard = identity(PROC, guard_pid)
    if guard["uid"] != uid or GUARD not in guard["argv"]:
        raise RuntimeError("not this user's repository guard")
    parent = os.getpid()
    for _ in range(64):
        if parent == guard_pid:
            break
        parent = identity(PROC, parent)["ppid"]
        if parent <= 1:
            raise RuntimeError("observer is not a guard descendant")
    else:
        raise RuntimeError("ancestry depth exceeded")
    plan = json.loads((PHASE / "plan.json").read_text())
    if plan["profile"] != "vision-rowmax-wait-diagnosis-v1":
        raise RuntimeError("wrong frozen diagnosis")
    helper = PHASE / "execution/wait-observer"
    if helper.resolve() != helper or helper.stat().st_uid != uid:
        raise RuntimeError("unexpected observer output directory")
    started = time.monotonic()
    tracker = Tracker()
    count = 0
    # Prove the real process-discovery operation before allowing model load.
    matching_queries(PROC, guard)
    emit(event="ready", identity=identity(PROC, os.getpid()), guard=guard,
         process_discovery="proc_stat_parent_validated",
         scope="owned slow NVML query and same-stage model threads only")
    reason = "deadline"
    while time.monotonic() - started < 330:
        if (helper / "stop").exists():
            reason = "stop_requested"
            break
        try:
            if not same_identity(identity(PROC, guard_pid), guard):
                reason = "guard_identity_changed"
                break
        except OSError:
            reason = "guard_exited"
            break
        values = matching_queries(PROC, guard)
        due = tracker.due(values, time.monotonic())
        if due:
            expected, age = due
            captured = dict(query_identity=expected, **age)
            begin = time.monotonic()
            try:
                captured["query"] = snapshot(PROC, expected)
            except (OSError, RuntimeError) as error:
                captured["query_error"] = repr(error)
            try:
                saved = json.loads((PHASE / "execution/model-mtp-candidate/identity.json").read_text())
                model = identity(PROC, saved["pid"])
                if (model["uid"] != uid or model["start_ticks"] != saved["start_ticks"] or
                        model["ppid"] != saved["ppid"] or model["argv"] != saved["argv"] or
                        model["argv"][0] != plan["binaries"]["candidate"]["path"]):
                    raise RuntimeError("model identity changed")
                captured["model"] = snapshot(PROC, model)
            except (OSError, RuntimeError, json.JSONDecodeError) as error:
                captured["model_error"] = repr(error)
            emit(event="snapshot", capture_seconds=time.monotonic()-begin, **captured)
            count += 1
            if count == len(THRESHOLDS):
                reason = "capture_budget_complete"
                break
        time.sleep(.1)
    emit(event="finished", reason=reason, snapshots=count,
         seconds=time.monotonic()-started)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guard-pid", type=int, required=True)
    args = parser.parse_args()
    raise SystemExit(observe(args.guard_pid))
