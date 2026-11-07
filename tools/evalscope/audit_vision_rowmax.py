"""Independent raw-database/resource review of the frozen row-max pilot.

No new inference. The local databases are trusted outputs of our own client;
their response pickle is never accepted from a request or remote artifact.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import shutil
import socket
import sqlite3
import statistics
import subprocess

from tools.evalscope import run_vision_rowmax as t

s = t.s


def review():
    plan = t.read(t.FOLDER / "plan.json")
    result = t.read(t.FOLDER / "execution/result.json")
    s.require(result["status"] == "HTTP_PILOT_COLLECTED_PENDING_REVIEW" and
              not result["failure"] and not result["cleanup_errors"] and
              not result["monitor_failures"], "pilot did not complete cleanly")
    s.require(result["counts"] == dict(starts=4, generations=64, controls=8, native=0),
              "unexpected attempt counts")
    for kind, count in result["counts"].items():
        s.require(len(list((t.FOLDER / "execution").glob(f"attempt-{kind}-*.json")))
                  == count <= t.LIMITS[kind], "attempt ledger differs")
    for name, digest in plan["files"].items():
        s.require(s.sha(name) == digest, "frozen input changed: " + name)
    tree = subprocess.check_output(["git", "ls-tree", "-r", "-z",
        plan["source_commit"]], cwd=s.ROOT)
    blobs = {}
    for item in tree.split(b"\0"):
        if item:
            header, name = item.split(b"\t", 1)
            blobs[name.decode()] = header.split()[2].decode()
    git_count = 0
    for name in plan["files"]:
        path = Path(name)
        if path.is_relative_to(s.ROOT):
            relative = str(path.relative_to(s.ROOT))
            if relative in blobs:
                raw = path.read_bytes()
                digest = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                s.require(digest == blobs[relative], "Git source mismatch: " + relative)
                git_count += 1
    events = [json.loads(x) for x in (t.FOLDER / "lease/events.jsonl").read_text().splitlines()]
    exit_record = t.read(t.FOLDER / "lease/exit.json")
    s.require(events[0]["event"] == "lease_acquired" and
              events[-1]["event"] == "lease_released" and
              events[-1]["status"] == exit_record["status"] == "COMPLETED" and
              exit_record["child_returncode"] == 0, "incomplete lease")
    lease_seconds = events[-1]["monotonic"] - events[0]["monotonic"]
    s.require(0 < lease_seconds < t.LIMITS["seconds"], "lease time limit")
    samples = [e for e in events if e["event"] == "monitor"]
    s.require(samples and all(not e["conflicts"] and not e["unreadable_processes"]
                             for e in samples), "environment invalid")
    owned, raw_rows, signatures = [], [], {}
    frozen = t.read(t.FOLDER / "requests.json")
    for group in t.GROUPS:
        folder = t.FOLDER / "execution"
        model = folder / ("model-" + group)
        identity, wait, reaped = (t.read(model / (n + ".json"))
                                  for n in ("identity", "waitable", "reaped"))
        s.require(wait["returncode"] == 0 and wait["identity"]["state"] == "Z" and
                  all(identity[k] == wait["identity"][k]
                      for k in ("pid", "ppid", "pgid", "start_ticks")), "model exit identity")
        custody = t.read(folder / (group + "-custody/custody.json"))
        s.require(custody["retained"] == [wait["identity"]] and
                  reaped == dict(pid=identity["pid"], returncode=0) and
                  not Path(f"/proc/{identity['pid']}").exists(), "model custody/reap")
        s.require(any(e["monotonic"] > wait["monotonic"] for e in samples),
                  "missing post-exit monitor")
        owned.append(identity["pid"])
        env = t.read(folder / ("env-" + group + ".json"))
        s.require(not any(k.startswith("Q4T_") for k in env), "unexpected experiment environment")
        argv = t.read(model / "command.json")["argv"]
        s.require(argv[0] == plan["binaries"][group.split("-")[0]] and
                  "--no-mtp" in argv and "--allow-media" in argv, "wrong binary/mode")
        log = (model / "stderr.log").read_text()
        s.require("[q4t][vision-timing]" not in log and
                  "[q4t][vision-forward-timing]" not in log and
                  "shutdown complete (0 in-flight remaining)" in log,
                  "instrumented or incomplete shutdown")
        client_folder = folder / ("client-" + group)
        s.require(t.read(client_folder / "client-process.json")["returncode"] == 0,
                  "HTTP client failed")
        databases = list(client_folder.rglob("benchmark_data.db"))
        s.require(len(databases) == 1, "database coverage")
        with sqlite3.connect("file:" + str(databases[0]) + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("select * from result order by start_time").fetchall()
        selected = [r for r in frozen if r["group"] == group]
        s.require(len(rows) == len(selected) == 16, "HTTP count")
        s.require([r["case"] for r in selected] == [t.CASES[0], *t.CASES, *t.CASES, *t.CASES],
                  "frozen case order")
        for i, (row, expected) in enumerate(zip(rows, selected)):
            body = json.loads(row["request"])
            s.require(body == expected["request"] and row["success"], "request drift/HTTP failure")
            messages = pickle.loads(base64.b64decode(row["response_messages"]))
            choices = [c for m in messages for c in m.get("choices", [])]
            s.require(messages and all(m.get("id") == body["request_id"] and
                      m.get("model") == body["model"] and "error" not in m for m in messages),
                      "response identity")
            content = "".join(c["delta"].get("content") or "" for c in choices)
            finish = [c["finish_reason"] for c in choices if c.get("finish_reason")]
            actual = json.loads(content)
            s.Draft202012Validator(body["response_format"]["json_schema"]["schema"]).validate(actual)
            s.require(finish == ["stop"] and actual == expected["expected"], "quality/stop")
            signature = [content, finish, row["prompt_tokens"], row["completion_tokens"]]
            signatures.setdefault(expected["case"], signature)
            s.require(signatures[expected["case"]] == signature, "output/usage differs")
            latency, ttft = row["latency"], row["first_chunk_latency"]
            s.require(math.isfinite(latency) and math.isfinite(ttft) and 0 < ttft < latency,
                      "invalid timing")
            s.require(i == 0 or row["start_time"] >= rows[i - 1]["completed_time"],
                      "overlapping requests")
            raw_rows.append(dict(group=group, case=expected["case"], first_request=i == 0,
                latency=latency, ttft=ttft, decode_seconds=latency - ttft,
                decode_tokens=row["completion_tokens"] - 1, prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"], answer=actual))
    queries = [json.loads(x) for x in (t.FOLDER / "lease/nvml-queries.jsonl").read_text().splitlines()]
    s.require(queries and all(q["status"] == "OK" and set(q["pids"]) <= set(owned)
                             for q in queries), "foreign context/NVML failure")
    s.require(not queries[-1]["pids"], "final GPU not empty")
    with (s.ROOT / ".q4t-work/gpu-exclusive.lock").open("r") as lock:
        stat = os.fstat(lock.fileno())
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gpu = s.nvml_process_query.query(timeout=5)
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", plan["port"]))
        s.require(gpu["status"] == "OK" and not gpu["pids"] and
                  stat.st_dev == plan["lock_device"] and stat.st_ino == plan["lock_inode"],
                  "postflight GPU/lock")
        fcntl.flock(lock, fcntl.LOCK_UN)
    summaries = {}
    for group in t.GROUPS:
        summaries[group] = {}
        for case in t.CASES:
            rows = [r for r in raw_rows if r["group"] == group and r["case"] == case
                    and not r["first_request"]]
            s.require(len(rows) == 3, "formal repeat count")
            summaries[group][case] = dict(
                latency_mean=statistics.mean(r["latency"] for r in rows),
                latency_range=[min(r["latency"] for r in rows), max(r["latency"] for r in rows)],
                ttft_mean=statistics.mean(r["ttft"] for r in rows),
                decode_tps=sum(r["decode_tokens"] for r in rows) / sum(r["decode_seconds"] for r in rows))
    pairs = []
    for parent, candidate in plan["target"]["pairs"]:
        for case in t.CASES:
            old, new = summaries[parent][case], summaries[candidate][case]
            gain = 1 - new["latency_mean"] / old["latency_mean"]
            pairs.append(dict(parent=parent, candidate=candidate, case=case,
                latency_reduction=gain,
                ttft_reduction=1 - new["ttft_mean"] / old["ttft_mean"],
                decode_tps_gain=new["decode_tps"] / old["decode_tps"] - 1,
                target_case=case in plan["target"]["cases"],
                meets_investment_target=gain >= plan["target"]["minimum_latency_reduction"]))
    target_met = all(p["meets_investment_target"] for p in pairs if p["target_case"])
    audit = dict(status="PILOT_TARGET_MET_PENDING_CONTRACTS" if target_met else "NO_GO_PILOT_TARGET_NOT_MET",
        source_commit=plan["source_commit"], binaries={k:dict(path=p, sha256=plan["files"][p])
            for k, p in plan["binaries"].items()}, evidence_valid=True,
        measurement_complete="bounded_visual_pilot_only", statistics="inconclusive",
        performance_accepted=False, default_unchanged=True, counts=result["counts"],
        lease_seconds=lease_seconds, git_sources=git_count, summaries=summaries,
        pairs=pairs, rows=raw_rows, cleanup=dict(owned_pids=owned, all_gone=True,
            gpu=gpu, port_free=True, lock_available=True, lock_inode=stat.st_ino),
        limits=["Only two startups per identity; no confidence or noninferiority claim.",
                "No literal SSE DONE bytes in evalscope DB; stop/usage only.",
                "Polling cannot exclude interference shorter than its interval."])
    s.save(t.BASE / "audit.json", audit)
    return {k: v for k, v in audit.items() if k not in ("summaries", "rows", "cleanup")}


def seal():
    plan = t.read(t.FOLDER / "plan.json")
    paths = {Path(p) for p in plan["files"]}
    paths.update(p for directory in (t.FOLDER, t.BASE / "logs")
                 for p in directory.rglob("*") if p.is_file())
    paths.update((t.BASE / "audit.json", Path(__file__).resolve()))
    cas = t.BASE / "originals"
    cas.mkdir()
    files = {}
    for path in sorted(paths):
        digest = s.sha(path)
        if str(path) in plan["files"]:
            s.require(digest == plan["files"][str(path)], "input drift before seal")
        target = cas / digest
        if not target.exists():
            shutil.copyfile(path, target)
        s.require(s.sha(target) == digest, "sealed copy differs")
        files[str(path)] = digest
    s.save(t.BASE / "originals.json", dict(files=files))
    return dict(files=len(files), objects=len(set(files.values())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("review", "seal"))
    args = parser.parse_args()
    print(json.dumps(review() if args.action == "review" else seal(), ensure_ascii=False))
