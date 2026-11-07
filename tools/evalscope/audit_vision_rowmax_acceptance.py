"""Reconstruct the completed full matrix and numerical custody from originals."""
import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess

from tools.evalscope import accept_vision_rowmax as t

s = t.s
read = t.pilot.read


def review():
    plan = read(t.FOLDER / "plan.json")
    out = t.FOLDER / "execution"
    result = read(out / "result.json")
    s.require(result["status"] == "CONTRACTS_AND_FULL_MATRIX_COLLECTED_PENDING_REVIEW"
              and not result["failure"] and not result["cleanup_errors"] and
              not result["monitor_failures"], "full stage incomplete")
    s.require(result["counts"] == dict(starts=4, generations=104, controls=8, native=3),
              "unexpected stage counts")
    for kind, count in result["counts"].items():
        s.require(len(list(out.glob(f"attempt-{kind}-*.json"))) == count <= t.LIMITS[kind],
                  "attempt ledger")
    for name, digest in plan["files"].items():
        s.require(s.sha(name) == digest, "input drift: " + name)
    tree = subprocess.check_output(["git", "ls-tree", "-r", "-z", plan["source_commit"]], cwd=s.ROOT)
    blobs = {}
    for item in tree.split(b"\0"):
        if item:
            header, name = item.split(b"\t", 1)
            blobs[name.decode()] = header.split()[2].decode()
    git_sources = 0
    for name in plan["files"]:
        path = Path(name)
        if path.is_relative_to(s.ROOT):
            relative = str(path.relative_to(s.ROOT))
            if relative in blobs:
                data = path.read_bytes()
                blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                s.require(blob == blobs[relative], "Git mismatch: " + relative)
                git_sources += 1
    events = [json.loads(x) for x in (t.FOLDER / "lease/events.jsonl").read_text().splitlines()]
    guard = read(t.FOLDER / "lease/exit.json")
    s.require(events[0]["event"] == "lease_acquired" and
              events[-1]["event"] == "lease_released" and
              events[-1]["status"] == guard["status"] == "COMPLETED" and
              guard["child_returncode"] == 0, "lease not complete")
    lease_seconds = events[-1]["monotonic"] - events[0]["monotonic"]
    s.require(0 < lease_seconds < t.LIMITS["seconds"], "lease budget")
    samples = [e for e in events if e["event"] == "monitor"]
    s.require(samples and all(not r["conflicts"] and not r["unreadable_processes"]
                             for r in samples), "environment invalid")
    owned = []
    for name in ("kernel", "hf-small", "hf-rectangular", *("model-" + g for g in t.GROUPS)):
        folder = out / name
        identity, wait, reaped = (read(folder / (n + ".json"))
                                  for n in ("identity", "waitable", "reaped"))
        custody_name = name.removeprefix("model-") + "-custody"
        custody = read(out / custody_name / "custody.json")
        s.require(wait["returncode"] == 0 and wait["identity"]["state"] == "Z" and
                  all(identity[k] == wait["identity"][k]
                      for k in ("pid", "ppid", "pgid", "start_ticks")) and
                  custody["retained"] == [wait["identity"]] and
                  reaped == dict(pid=identity["pid"], returncode=0) and
                  not Path(f"/proc/{identity['pid']}").exists(), "owned lifetime: " + name)
        s.require(any(e["monotonic"] > wait["monotonic"] for e in samples), "post-exit sample")
        owned.append(identity["pid"])
    queries = [json.loads(x) for x in (t.FOLDER / "lease/nvml-queries.jsonl").read_text().splitlines()]
    s.require(queries and all(q["status"] == "OK" and set(q["pids"]) <= set(owned)
                             for q in queries) and not queries[-1]["pids"], "GPU context observations")
    kernel = [json.loads(x) for x in (out / "kernel/stdout.log").read_text().splitlines()]
    shapes = read(t.BASE / "numerical-input/manifest.json")["planned_shapes"]
    expected = [(n, 1, 0) for n in shapes] + [(384, 4, 0)] + [(257, 1, p) for p in range(1, 7)]
    s.require([(r["spatial"], r["temporal"], r["pattern"]) for r in kernel[1:-1]] == expected,
              "numerical case coverage")
    s.require(all(r["mismatches"] == 0 and r["elements"] == r["spatial"] * r["temporal"] * 1152
                  and r["nonfinite"] == (r["elements"] if r["pattern"] in (2, 6) else 0)
                  for r in kernel[1:-1]), "numerical mismatch/boundary")
    s.require(kernel[-1] == dict(passed=True, cases=26, bf16_elements=19312128), "numerical total")
    hf = []
    for name in ("small", "rectangular"):
        log = (out / ("hf-" + name) / "stdout.log").read_text()
        matches = re.findall(r"\[(image|video)\] max_abs_diff = (\S+)  max_ref_abs = (\S+)  l2_rel = (\S+)", log)
        s.require([m[0] for m in matches] == ["image", "video"], "HF coverage")
        for case, maximum, _, relative in matches:
            file = f"hf-{name}-output-{case}.bf16"
            current, parent = out / file, t.VISION / "phase-04/execution" / file
            s.require(current.read_bytes() == parent.read_bytes() and
                      0 <= float(maximum) < .05 and 0 <= float(relative) < .05, "HF contract")
            hf.append(dict(shape=name, case=case, max_abs=float(maximum), rel_l2=float(relative),
                bytes=current.stat().st_size, bf16_sha256=s.sha(current), parent_equal=True))
    raw, summaries = {}, {}
    for group in t.GROUPS:
        raw[group], summaries[group] = {}, {}
        model = out / ("model-" + group)
        capacity = read(model / "capacity.json")
        s.require(capacity["matches_requested"] and
                  capacity["effective"] == dict(max_len=208896, max_seq=1, max_prefill=8192),
                  "effective capacity")
        argv = read(model / "command.json")["argv"]
        mode, arm = group.split("-")
        s.require(argv[0] == plan["binaries"][arm]["path"] and
                  ("--no-mtp" in argv) == (mode == "plain") and "--mtp" not in argv and
                  "--allow-media" not in argv, "mode identity")
        s.require(not any(k.startswith("Q4T_") for k in read(out / ("env-" + group + ".json"))),
                  "experiment environment")
        log = (model / "stderr.log").read_text()
        s.require("shutdown complete (0 in-flight remaining)" in log and
                  "[q4t][vision-timing]" not in log and "[q4t][vision-forward-timing]" not in log,
                  "shutdown/instrumentation")
        for case in plan["cases"]:
            folder = out / ("client-" + group) / case["id"]
            databases = list(folder.rglob("benchmark_data.db"))
            s.require(len(databases) == 1, "database coverage")
            with sqlite3.connect("file:" + str(databases[0]) + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                records = db.execute("select * from result order by start_time").fetchall()
            inputs = [json.loads(x) for x in Path(case["path"]).read_text().splitlines()]
            s.require(len(records) == len(inputs) == case["count"], "HTTP count")
            rows = []
            for i, (record, expected_body) in enumerate(zip(records, inputs)):
                body = json.loads(record["request"])
                s.require(record["success"] and all(body.get(k) == v for k, v in expected_body.items()) and
                          body["stream"] is True and body["max_tokens"] == case["tokens"] and
                          body["temperature"] == 0, "HTTP body/result")
                chunks = pickle.loads(base64.b64decode(record["response_messages"]))
                choices = [c for chunk in chunks for c in chunk.get("choices", [])]
                identity = t.http.response_identity(chunks)
                s.require(identity["response_id_valid"], "response IDs")
                text = "".join(c.get("delta", c.get("message", {})).get("content", "") for c in choices)
                finish = [c["finish_reason"] for c in choices if c.get("finish_reason")]
                prompt_sha = hashlib.sha256(body["prompt"].encode()).hexdigest()
                prompt, completion = record["prompt_tokens"], record["completion_tokens"]
                latency, ttft = record["latency"], record["first_chunk_latency"]
                s.require(case["min"] <= prompt <= case["max"] and
                          all(math.isfinite(v) for v in (latency, ttft)) and
                          0 < ttft < latency and
                          (i == 0 or record["start_time"] >= records[i - 1]["completed_time"]),
                          "HTTP length/timing/sequential")
                if case["id"] == "quality":
                    expected = plan["quality"][prompt_sha]
                    s.require(text.strip() == expected["expected"] and prompt == expected["length"]
                              and 0 < completion <= 32 and finish == ["stop"], "fixed quality")
                else:
                    s.require(completion == 256 and finish == ["length"], "output length")
                rows.append(dict(prompt_sha256=prompt_sha, prompt_tokens=prompt, completion_tokens=completion,
                    text=text, finish=finish, latency=latency, ttft=ttft,
                    first_request=case["id"] == "context-1024" and i == 0))
            raw[group][case["id"]] = rows
            if case["id"] != "quality":
                s.require(len({(r["text"], r["prompt_sha256"]) for r in rows}) == 1, "determinism")
                summaries[group][case["id"]] = dict(
                    latency_mean=statistics.mean(r["latency"] for r in rows),
                    ttft_mean=statistics.mean(r["ttft"] for r in rows),
                    decode_tps=sum(r["completion_tokens"] - 1 for r in rows) /
                               sum(r["latency"] - r["ttft"] for r in rows))
    comparisons = []
    for mode in ("plain", "mtp"):
        for case in plan["cases"]:
            old, new = raw[mode + "-parent"][case["id"]], raw[mode + "-candidate"][case["id"]]
            keys = ("prompt_sha256", "prompt_tokens", "completion_tokens", "text", "finish")
            s.require([[r[k] for k in keys] for r in old] == [[r[k] for k in keys] for r in new],
                      "parent/candidate contract")
            if case["id"] != "quality":
                a, b = summaries[mode + "-parent"][case["id"]], summaries[mode + "-candidate"][case["id"]]
                comparisons.append(dict(mode=mode, case=case["id"], parent=a, candidate=b,
                    ttft_gain=1 - b["ttft_mean"] / a["ttft_mean"],
                    decode_gain=b["decode_tps"] / a["decode_tps"] - 1,
                    latency_gain=1 - b["latency_mean"] / a["latency_mean"]))
    with (s.ROOT / ".q4t-work/gpu-exclusive.lock").open("r") as lock:
        stat = os.fstat(lock.fileno())
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gpu = s.nvml_process_query.query(timeout=5)
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", s.PORT))
        s.require(gpu["status"] == "OK" and not gpu["pids"] and stat.st_dev == plan["lock_device"]
                  and stat.st_ino == plan["lock_inode"], "postflight GPU/lock")
        fcntl.flock(lock, fcntl.LOCK_UN)
    audit = dict(status="PASS_CONTRACTS_AND_COMPLETE_MATRIX_PENDING_ENGINEERING_DECISION",
        source_commit=plan["source_commit"], binaries=plan["binaries"], counts=result["counts"],
        lease_seconds=lease_seconds, git_sources=git_sources, kernel=kernel, hf=hf,
        summaries=summaries, comparisons=comparisons, rows=raw, quality_checks=44,
        paired_responses=52, statistics="inconclusive", performance_accepted=False,
        default_unchanged=True, cleanup=dict(owned_pids=owned, all_gone=True, gpu=gpu,
            port_free=True, lock_available=True, lock_inode=stat.st_ino))
    s.save(t.BASE / "acceptance-audit.json", audit)
    return {k: v for k, v in audit.items() if k not in ("rows", "summaries", "cleanup", "kernel")}


def seal():
    plan = read(t.FOLDER / "plan.json")
    paths = {Path(p) for p in plan["files"]}
    paths.update(p for directory in (t.FOLDER, t.BASE / "logs")
                 for p in directory.rglob("*") if p.is_file() and p.name != "accept-seal.log")
    paths.update((t.BASE / "acceptance-audit.json", Path(__file__).resolve()))
    if (t.BASE / "abort-postflight.json").exists():
        paths.add(t.BASE / "abort-postflight.json")
    cas = t.BASE / "accept-originals"
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
    s.save(t.BASE / "accept-originals.json", dict(files=files))
    return dict(files=len(files), objects=len(set(files.values())))


def archive_abort():
    """Preserve an invalid phase; successful subresults never change its status."""
    plan = read(t.FOLDER / "plan.json")
    out = t.FOLDER / "execution"
    result = read(out / "result.json")
    guard = read(t.FOLDER / "lease/exit.json")
    s.require(guard["status"] == "ENV_ABORT" and result["status"] == "FAILED" and
              "CLEANUP_FAILED" in guard["reason"], "expected monitor abort")
    for name, digest in plan["files"].items():
        s.require(s.sha(name) == digest, "input drift: " + name)
    tree = subprocess.check_output(["git", "ls-tree", "-r", "-z", plan["source_commit"]], cwd=s.ROOT)
    blobs = {}
    for item in tree.split(b"\0"):
        if item:
            header, name = item.split(b"\t", 1)
            blobs[name.decode()] = header.split()[2].decode()
    git_sources = 0
    for name in plan["files"]:
        path = Path(name)
        if path.is_relative_to(s.ROOT):
            relative = str(path.relative_to(s.ROOT))
            if relative in blobs:
                data = path.read_bytes()
                blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                s.require(blob == blobs[relative], "Git mismatch: " + relative)
                git_sources += 1
    queries = [json.loads(x) for x in (t.FOLDER / "lease/nvml-queries.jsonl").read_text().splitlines()]
    failures = [r for r in queries if r["status"] != "OK"]
    s.require(len(failures) == 1 and failures[0]["status"] == "CLEANUP_FAILED",
              "unexpected monitor failure history")
    api = [json.loads(x) for x in failures[0]["stdout"].splitlines()]
    s.require(api[-1]["api"] == "init" and api[-1]["event"] == "begin", "failed NVML API")
    events = [json.loads(x) for x in (t.FOLDER / "lease/events.jsonl").read_text().splitlines()]
    s.require(events[0]["event"] == "lease_acquired" and
              events[-1]["event"] == "lease_released" and events[-1]["status"] == "ENV_ABORT",
              "abort release receipt")
    samples = [r for r in events if r["event"] == "monitor"]
    s.require(all(not r["conflicts"] and not r["unreadable_processes"] for r in samples),
              "additional environment failure")
    lives = {}
    for path in sorted(out.glob("*/identity.json")):
        folder = path.parent
        identity, wait = read(path), read(folder / "waitable.json")
        s.require(wait["returncode"] == 0 and all(identity[k] == wait["identity"][k]
                  for k in ("pid", "ppid", "pgid", "start_ticks")) and
                  not Path(f"/proc/{identity['pid']}").exists(), "owned process exit")
        name = folder.name
        custody = out / (name.removeprefix("model-") + "-custody/custody.json")
        complete = (folder / "reaped.json").exists() and custody.exists()
        s.require(complete == (name != "model-mtp-candidate"), "custody inventory changed")
        if complete:
            s.require(read(custody)["retained"] == [wait["identity"]] and
                      read(folder / "reaped.json") == dict(pid=identity["pid"], returncode=0),
                      "completed custody differs")
        lives[name] = dict(pid=identity["pid"], exited=True, waitable_returncode=0,
                          custody_complete=complete)
    kernel = [json.loads(x) for x in (out / "kernel/stdout.log").read_text().splitlines()]
    s.require(kernel[-1] == dict(passed=True, cases=26, bf16_elements=19312128) and
              all(r["mismatches"] == 0 for r in kernel[1:-1]), "direct numerical result")
    hf = []
    for name in ("small", "rectangular"):
        matches = re.findall(r"\[(image|video)\] max_abs_diff = (\S+)  max_ref_abs = (\S+)  l2_rel = (\S+)",
            (out / ("hf-" + name) / "stdout.log").read_text())
        s.require([m[0] for m in matches] == ["image", "video"], "HF case coverage")
        for case, maximum, _, relative in matches:
            file = f"hf-{name}-output-{case}.bf16"
            s.require((out / file).read_bytes() == (t.VISION / "phase-04/execution" / file).read_bytes()
                      and 0 <= float(maximum) < .05 and 0 <= float(relative) < .05, "HF result")
            hf.append(dict(shape=name, case=case, max_abs=float(maximum), rel_l2=float(relative),
                           bf16_sha256=s.sha(out / file), parent_equal=True))
    records, summaries, comparisons = {}, {}, []
    for group in t.GROUPS[:3]:
        records[group], summaries[group] = {}, {}
        for case in plan["cases"]:
            databases = list((out / ("client-" + group) / case["id"]).rglob("benchmark_data.db"))
            if not databases:
                continue
            s.require(len(databases) == 1, "multiple databases")
            with sqlite3.connect("file:" + str(databases[0]) + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                stored = db.execute("select * from result order by start_time").fetchall()
            inputs = [json.loads(x) for x in Path(case["path"]).read_text().splitlines()]
            rows = []
            for row, expected in zip(stored, inputs):
                body = json.loads(row["request"])
                s.require(row["success"] and all(body.get(k) == v for k, v in expected.items()) and
                          body["stream"] is True and body["max_tokens"] == case["tokens"] and
                          body["temperature"] == 0, "stored HTTP input/contract")
                chunks = pickle.loads(base64.b64decode(row["response_messages"]))
                choices = [c for chunk in chunks for c in chunk.get("choices", [])]
                s.require(t.http.response_identity(chunks)["response_id_valid"], "response IDs")
                text = "".join(c.get("delta", {}).get("content", "") for c in choices)
                finish = [c["finish_reason"] for c in choices if c.get("finish_reason")]
                digest = hashlib.sha256(body["prompt"].encode()).hexdigest()
                prompt, completion = row["prompt_tokens"], row["completion_tokens"]
                latency, ttft = row["latency"], row["first_chunk_latency"]
                s.require(case["min"] <= prompt <= case["max"] and 0 < ttft < latency and
                          all(math.isfinite(v) for v in (latency, ttft)), "length/timing")
                if case["id"] == "quality":
                    truth = plan["quality"][digest]
                    s.require(text.strip() == truth["expected"] and prompt == truth["length"] and
                              0 < completion <= 32 and finish == ["stop"], "quality result")
                else:
                    s.require(completion == 256 and finish == ["length"], "output contract")
                rows.append(dict(prompt_sha256=digest, prompt_tokens=prompt, completion_tokens=completion,
                                 text=text, finish=finish, latency=latency, ttft=ttft))
            s.require(len(stored) == len(rows) <= case["count"], "database coverage")
            if group.startswith("plain"):
                s.require(len(rows) == case["count"], "missing plain records")
            records[group][case["id"]] = rows
            if rows and case["id"] != "quality":
                s.require(len({(r["text"], r["prompt_sha256"]) for r in rows}) == 1, "determinism")
                summaries[group][case["id"]] = dict(
                    latency_mean=statistics.mean(r["latency"] for r in rows),
                    ttft_mean=statistics.mean(r["ttft"] for r in rows),
                    decode_tps=sum(r["completion_tokens"] - 1 for r in rows) /
                               sum(r["latency"] - r["ttft"] for r in rows))
    for case in plan["cases"]:
        old, new = (records[g][case["id"]] for g in ("plain-parent", "plain-candidate"))
        keys = ("prompt_sha256", "prompt_tokens", "completion_tokens", "text", "finish")
        s.require([[r[k] for k in keys] for r in old] == [[r[k] for k in keys] for r in new],
                  "plain parent/candidate differ")
        if case["id"] != "quality":
            a, b = (summaries[g][case["id"]] for g in ("plain-parent", "plain-candidate"))
            comparisons.append(dict(case=case["id"], parent=a, candidate=b,
                ttft_gain=1-b["ttft_mean"]/a["ttft_mean"],
                decode_gain=b["decode_tps"]/a["decode_tps"]-1,
                latency_gain=1-b["latency_mean"]/a["latency_mean"]))
    post = read(t.BASE / "abort-postflight.json")
    s.require(not any(post["owned_processes"].values()) and post["port_free"] and
              post["gpu"]["status"] == "OK" and not post["gpu"]["pids"] and
              post["lock_inode"] == plan["lock_inode"], "post-abort observation")
    received = sum((out / ("model-" + g) / "stderr.log").read_text().count("event=begin stage=validation")
                   for g in t.GROUPS[:3])
    complete = sum(len(rows) for cases in records.values() for rows in cases.values())
    s.require(received == 59 and complete == 58 and result["counts"] ==
              dict(starts=3, generations=61, controls=4, native=3), "attempt/actual ledger")
    audit = dict(status="ENV_ABORT_INCOMPLETE_ACCEPTANCE", performance_accepted=False,
        default_unchanged=True, source_commit=plan["source_commit"], binaries=plan["binaries"],
        counts_reserved=result["counts"], actual_chat_requests=received, completed_chat_records=complete,
        interrupted_chat_requests=1, not_issued_reserved_requests=2,
        lease_seconds=events[-1]["monotonic"]-events[0]["monotonic"], git_sources=git_sources,
        guard=guard, monitor_failure=failures[0], processes=lives, kernel=kernel, hf=hf,
        rows=records, summaries=summaries, plain_comparisons=comparisons,
        quality_checks=22, paired_plain_responses=26, postflight=post,
        limitations=["Entire acceptance lease remains ENV_ABORT; no full performance acceptance",
            "MTP candidate only has two complete tiers; parent was not started",
            "MTP custody/reaped receipt missing after monitor failure; not reconstructed as success",
            "Kernel NV_ERR_NO_MEMORY is concurrent evidence, not a demonstrated root cause",
            "Lock handed to another task; postflight does not assert it remains available"])
    s.save(t.BASE / "acceptance-audit.json", audit)
    return {k: v for k, v in audit.items() if k not in (
        "rows", "summaries", "kernel", "hf", "guard", "monitor_failure", "postflight", "binaries")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("review", "seal", "archive-abort"))
    args = parser.parse_args()
    action = {"review": review, "seal": seal, "archive-abort": archive_abort}[args.action]
    print(json.dumps(action(), ensure_ascii=False))
