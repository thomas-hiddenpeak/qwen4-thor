"""Seal originals and independently reconstruct the bounded forward diagnosis."""
import argparse
from dataclasses import asdict
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

from tools.vision_service import forward_timing_stage as t

s = t.s


def cleanup_observation():
    plan = t.t.read(t.FOLDER / "plan.json")
    owned = []
    for mode in ("off", "on"):
        identity = t.t.read(t.FOLDER / f"execution/model-{mode}/identity.json")
        owned.append(dict(pid=identity["pid"], start_ticks=identity["start_ticks"],
                          exists=Path(f"/proc/{identity['pid']}").exists()))
    path = s.ROOT / ".q4t-work/gpu-exclusive.lock"
    with path.open("r") as lock:
        stat = os.fstat(lock.fileno())
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gpu = s.nvml_process_query.query(timeout=5)
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", plan["port"]))
        result = dict(owned=owned, gpu=gpu, port_free=True, lock_available=True,
                      lock_device=stat.st_dev, lock_inode=stat.st_ino)
        fcntl.flock(lock, fcntl.LOCK_UN)
    s.save(t.BASE / "cleanup.json", result)
    s.require(not any(r["exists"] for r in owned) and gpu["status"] == "OK" and
              not gpu["pids"], "post-stage resources unavailable")
    return result


def seal():
    plan = t.t.read(t.FOLDER / "plan.json")
    paths = {Path(p) for p in plan["files"]}
    paths.update(p for directory in (t.FOLDER, t.BASE / "logs")
                 for p in directory.rglob("*") if p.is_file())
    paths.add(t.BASE / "cleanup.json")
    cas = t.BASE / "originals"
    cas.mkdir()
    files = {}
    for path in sorted(paths):
        digest = s.sha(path)
        if str(path) in plan["files"]:
            s.require(digest == plan["files"][str(path)], "source drift: " + str(path))
        target = cas / digest
        if not target.exists(): shutil.copyfile(path, target)
        s.require(s.sha(target) == digest, "sealed copy differs")
        files[str(path)] = digest
    s.save(t.BASE / "originals.json", dict(files=files))
    return dict(files=len(files))


def audit():
    files = t.t.read(t.BASE / "originals.json")["files"]
    cas = t.BASE / "originals"
    for digest in set(files.values()):
        s.require(s.sha(cas / digest) == digest, "original drift")

    def raw(name):
        path = Path(name)
        if not path.is_absolute(): path = t.FOLDER / path
        return (cas / files[str(path)]).read_bytes()

    def read(name): return json.loads(raw(name))

    plan = read("plan.json")
    s.require(plan["limits"] == t.LIMITS and plan["planned_requests"] == 12,
              "frozen plan differs")
    for path, digest in plan["files"].items():
        s.require(files[path] == digest, "frozen binding changed")
    tree = subprocess.check_output(["git", "ls-tree", "-r", "-z", plan["source_commit"]], cwd=s.ROOT)
    blobs = {}
    for item in tree.split(b"\0"):
        if item:
            header, name = item.split(b"\t", 1)
            blobs[name.decode()] = header.split()[2].decode()
    git_sources = 0
    for name in plan["files"]:
        path = Path(name)
        if not path.is_relative_to(s.ROOT): continue
        relative = str(path.relative_to(s.ROOT))
        if relative in blobs:
            data = raw(path)
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            s.require(blob == blobs[relative], "Git/source mismatch: " + relative)
            git_sources += 1
    result = read("execution/result.json")
    s.require(result["status"] == t.Run.success_status and not result["failure"]
              and not result["cleanup_errors"] and not result["monitor_failures"],
              "stage failed; cannot accept complete diagnosis")
    for kind, count in result["counts"].items():
        attempts = [p for p in files if p.startswith(str(t.FOLDER / ("execution/attempt-" + kind + "-")))]
        s.require(len(attempts) == count <= plan["limits"][kind], "attempt count mismatch")
    s.require(result["counts"]["starts"] == 2 and
              result["counts"]["generations"] == 12 and result["counts"]["native"] == 0,
              "unexpected run size")
    events = [json.loads(line) for line in raw("lease/events.jsonl").splitlines()]
    guard = read("lease/exit.json")
    s.require(events[0]["event"] == "lease_acquired" and
              events[-1]["event"] == "lease_released" and
              events[-1]["status"] == guard["status"] == "COMPLETED" and
              guard["child_returncode"] == 0, "guard not complete")
    samples = [e for e in events if e["event"] == "monitor"]
    s.require(samples and all(not e["conflicts"] and not e["unreadable_processes"] for e in samples),
              "guard conflict/monitor failure")
    seconds = events[-1]["monotonic"] - events[0]["monotonic"]
    s.require(0 < seconds < plan["limits"]["seconds"], "lease budget")
    owned = []
    for mode in ("off", "on"):
        model = "execution/model-" + mode + "/"
        identity, wait, reaped = (read(model + f + ".json")
                                   for f in ("identity", "waitable", "reaped"))
        s.require(wait["returncode"] == 0 and wait["identity"]["state"] == "Z",
                  "model not waitable at exit zero")
        for key in ("pid", "start_ticks", "pgid", "ppid"):
            s.require(identity[key] == wait["identity"][key], "model identity drift")
        custody = read(f"execution/{mode}-custody/custody.json")
        s.require(custody["retained"] == [wait["identity"]] and
                  reaped == dict(pid=identity["pid"], returncode=0), "model custody/reap differs")
        s.require(any(e["monotonic"] > wait["monotonic"] for e in samples),
                  "no post-exit monitor sample")
        env = read(f"execution/env-{mode}.json")
        s.require({k: v for k, v in env.items() if k.startswith("Q4T_")} ==
                  t.Run.timing_env(None, mode), "unexpected diagnostic/optimization environment")
        owned.append(identity["pid"])
    queries = [json.loads(line) for line in raw("lease/nvml-queries.jsonl").splitlines()]
    s.require(queries and all(q["status"] == "OK" and set(q["pids"]) <= set(owned)
                             for q in queries), "foreign context or NVML failure")
    final_query = [json.loads(line) for line in queries[-1]["stdout"].splitlines()][-1]
    s.require(final_query["status"] == "OK" and not final_query["pids"] and
              final_query["monotonic"] > read("execution/model-on/waitable.json")["monotonic"],
              "no fresh empty final GPU query")
    final = read("execution/gpu-final.json")
    cleanup = read(t.BASE / "cleanup.json")
    s.require(final["status"] == cleanup["gpu"]["status"] == "OK" and
              not final["pids"] and not cleanup["gpu"]["pids"] and
              [r["pid"] for r in cleanup["owned"]] == owned and
              not any(r["exists"] for r in cleanup["owned"]) and
              cleanup["port_free"] and cleanup["lock_available"] and
              cleanup["lock_inode"] == plan["lock_inode"] and
              cleanup["lock_device"] == plan["lock_device"], "cleanup differs")
    walls, forwards = {}, {}
    for mode in ("off", "on"):
        log = raw(f"execution/model-{mode}/stderr.log").decode()
        walls[mode], forwards[mode] = t.wall.parse_records(log), t.forward.parse_records(log)
    rows = read("requests.json")
    s.require(len(rows) == 12 and not forwards["off"], "wrong requests/default timing")
    originals = {r["case"]: r for r in read(t.t.FOLDER / "requests.json") if r["mode"] == "off"}
    observations, off = [], []
    for mode in ("off", "on"):
        selected = [r for r in rows if r["mode"] == mode]
        s.require(tuple(r["case"] for r in selected) == t.CASES, "fixed case order")
        ids = {r["request"]["request_id"] for r in selected if r["patches"]}
        s.require(set(walls[mode]) == ids and (mode == "off" or set(forwards[mode]) == ids),
                  "timing coverage differs")
        for index, row in enumerate(selected):
            label, body = row["label"], row["request"]
            original = originals[row["case"]]
            scrub = lambda b: {k: v for k, v in b.items() if k not in ("request_id", "cancel_token")}
            s.require(scrub(body) == scrub(original["request"]) and
                      row["expected"] == original["expected"], "original request/truth changed")
            s.require(read(f"execution/{label}-request.json")["body"] == body,
                      "sent body differs from plan")
            transport = read(f"execution/{label}-transport.json")
            s.require(transport["status"] == 200, "HTTP failed")
            wire = io.BytesIO(raw(f"execution/{label}-wire.bin"))
            value = (s.client.read_sse if body.get("stream") else s.client.read_json)(wire)
            s.require(value.id == body["request_id"] and value.model == body["model"] and
                      asdict(value) == read(f"execution/{label}-completion.json"), "response identity/derived record")
            actual = json.loads(value.message["content"])
            s.Draft202012Validator(body["response_format"]["json_schema"]["schema"]).validate(actual)
            pre = read(f"execution/{label}-preflight-wire.bin")
            s.require(actual == row["expected"] and value.finish_reason == "stop" and
                      value.usage["prompt_tokens"] == pre["prompt_tokens"], "quality/terminal/usage")
            same = (value.message, value.finish_reason, value.usage)
            if mode == "off": off.append(same)
            else: s.require(same == off[index], "timing changed output")
            w = f = None
            if row["patches"]:
                w = walls[mode][body["request_id"]]
                t.wall.validate_request(w, body)
                s.require(w["patches"] == t.PATCHES[row["case"]] == row["patches"], "patch geometry")
                if mode == "on":
                    f = forwards[mode][body["request_id"]]
                    t.forward.validate_request(f, body, w, plan["depth"], row["patches"])
            observations.append(dict(label=label, case=row["case"], mode=mode,
                e2e_ms=transport["seconds"] * 1000, answer=actual, usage=value.usage,
                wall=w, forward=f))
    audit = dict(status="PASS_BOUNDED_FORWARD_DIAGNOSIS", counts=result["counts"],
        lease_seconds=seconds, files=len(files), git_sources=git_sources,
        source_commit=plan["source_commit"], binary_sha256=plan["files"][plan["binary"]],
        observations=observations, cleanup=cleanup, performance_acceptance=False,
        default_unchanged=True)
    s.save(t.BASE / "audit.json", audit)
    return {k: v for k, v in audit.items() if k not in ("observations", "cleanup")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("cleanup", "seal", "audit"))
    args = parser.parse_args()
    print(json.dumps(dict(cleanup=cleanup_observation, seal=seal, audit=audit)[args.action]()))
