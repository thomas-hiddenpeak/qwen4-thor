"""Audit the new MTP lease and reuse only sealed, completed old lifetimes.

The old ENV_ABORT and HOLD remain unchanged. No GPU queries or inference are
performed by this offline review; the strict lease owns GPU observations.
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
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess

from tools.evalscope import resume_vision_rowmax as r

t = r.prior
s = r.s
read = r.read
AUDIT = r.BASE / "completion-acceptance-audit.json"
MANIFEST = r.BASE / "completion-originals.json"


def numerical(out):
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
    return dict(kernel=kernel, hf=hf)


def matrix(plan, out, groups):
    raw, summaries = {}, {}
    for group in groups:
        raw[group], summaries[group] = {}, {}
        model = out / ("model-" + group)
        capacity = read(model / "capacity.json")
        s.require(capacity["matches_requested"] and
                  capacity["effective"] == dict(max_len=208896, max_seq=1, max_prefill=8192),
                  "effective capacity")
        argv = read(model / "command.json")["argv"]
        mode, arm = group.split("-")
        expected_argv = [plan["binaries"][arm]["path"], "serve", "--model-dir", plan["model"],
            "--host", "127.0.0.1", "--port", str(plan["port"]), "--max-seq", "1",
            "--max-prefill", "8192", "--max-len", "208896", "--max-tokens", "256"]
        if mode == "plain":
            expected_argv.append("--no-mtp")
        s.require(argv == expected_argv, "exact server command/mode identity")
        s.require(not any(k.startswith("Q4T_") for k in read(out / ("env-" + group + ".json"))),
                  "experiment environment")
        log = (model / "stderr.log").read_text()
        s.require("shutdown complete (0 in-flight remaining)" in log and
                  "[q4t][vision-timing]" not in log and "[q4t][vision-forward-timing]" not in log,
                  "shutdown/instrumentation")
        s.require(("MTP loaded (k=3 max_seq=1)" in log) == (mode == "mtp"),
                  "actual default MTP k=3 path")
        for case in plan["cases"]:
            folder = out / ("client-" + group) / case["id"]
            client = read(folder / "client-process.json")
            s.require(client["returncode"] == 0 and not client["failure"] and
                      not client["cleanup_errors"], "HTTP client exit")
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
                    start_time=record["start_time"], completed_time=record["completed_time"],
                    first_request=case["id"] == "context-1024" and i == 0))
            raw[group][case["id"]] = rows
            if case["id"] != "quality":
                s.require(len({(r["text"], r["prompt_sha256"]) for r in rows}) == 1, "determinism")
                summaries[group][case["id"]] = dict(
                    latency_mean=statistics.mean(r["latency"] for r in rows),
                    ttft_mean=statistics.mean(r["ttft"] for r in rows),
                    decode_tps=sum(r["completion_tokens"] - 1 for r in rows) /
                               sum(r["latency"] - r["ttft"] for r in rows))
    return raw, summaries


def source_identity(plan):
    tree = subprocess.check_output(
        ["git", "ls-tree", "-r", "-z", plan["source_commit"]], cwd=s.ROOT)
    blobs = {item.split(b"\t", 1)[1].decode(): item.split(b"\t", 1)[0].split()[2].decode()
             for item in tree.split(b"\0") if item}
    count = 0
    for name, digest in plan["files"].items():
        s.require(s.sha(name) == digest, "frozen input drift: " + name)
        path = Path(name)
        if path.is_relative_to(s.ROOT):
            relative = str(path.relative_to(s.ROOT))
            if relative in blobs:
                data = path.read_bytes()
                blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                s.require(blob == blobs[relative], "current Git source mismatch")
                count += 1
    return count


def postflight(plan, out, queries):
    # No second NVML stage: reuse the final strict observation, and distinguish
    # it explicitly from this later CPU-only port/process/lock observation.
    final = read(out / "gpu-final.json")
    s.require(final["status"] == "OK" and final["pids"] == [] and
              queries[-1]["status"] == "OK" and queries[-1]["pids"] == [],
              "missing final GPU observation")
    pids = {read(p)["pid"] for p in out.glob("*/identity.json")}
    pids.update(q["worker_pid"] for q in queries)
    for event in r.jsonlines(r.FOLDER / "lease/events.jsonl"):
        pids.add(event["pid"])
        if "child_pid" in event:
            pids.add(event["child_pid"])
    for p in out.rglob("client-process.json"):
        value = read(p)
        if "pid" in value:
            pids.add(value["pid"])
    s.require(all(not Path(f"/proc/{pid}").exists() for pid in pids),
              "own process remains present")
    with (s.ROOT / ".q4t-work/gpu-exclusive.lock").open("r") as lock:
        stat = os.fstat(lock.fileno())
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s.require((stat.st_dev, stat.st_ino) == (plan["lock_device"], plan["lock_inode"]),
                  "shared lock changed")
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", plan["port"]))
            sock.listen(1)
    return dict(own_pids=sorted(pids), all_absent=True, port_free=True,
                lock_available=True, lock_inode=stat.st_ino,
                gpu_visibility="Empty at strict lease exit; no new GPU query",
                gpu_final=final)


def review():
    reused = r.verify_reuse()
    plan = read(r.FOLDER / "plan.json")
    old_plan = read(t.FOLDER / "plan.json")
    out = r.FOLDER / "execution"
    result, guard = read(out / "result.json"), read(r.FOLDER / "lease/exit.json")
    s.require(result["status"] == "CONTRACTS_AND_FULL_MATRIX_COLLECTED_PENDING_REVIEW" and
              not result["failure"] and not result["cleanup_errors"] and
              not result["monitor_failures"], "MTP completion stage failed")
    counts = result["counts"]
    s.require(counts["starts"] == 2 and counts["generations"] == 52 and
              counts["native"] == 0 and 4 <= counts["controls"] <= r.LIMITS["controls"],
              "MTP counts differ")
    for kind, count in counts.items():
        s.require(len(list(out.glob(f"attempt-{kind}-*.json"))) == count <= r.LIMITS[kind],
                  "attempt budget")
    git_sources = source_identity(plan)
    s.require(plan["limits"] == r.LIMITS and plan["groups"] == list(r.GROUPS) and
              plan["binaries"] == old_plan["binaries"] and plan["quality"] == old_plan["quality"],
              "stage identity differs")
    for old, new in zip(old_plan["cases"], plan["cases"]):
        s.require({k: v for k, v in old.items() if k != "path"} ==
                  {k: v for k, v in new.items() if k != "path"} and
                  s.sha(old["path"]) == s.sha(new["path"]), "fixture drift")
    events = r.jsonlines(r.FOLDER / "lease/events.jsonl")
    queries = r.jsonlines(r.FOLDER / "lease/nvml-queries.jsonl")
    s.require(events[0]["event"] == "lease_acquired" and
              events[-1]["event"] == "lease_released" and
              events[-1]["status"] == guard["status"] == "COMPLETED" and
              guard["child_returncode"] == 0 and guard["compute_backend"] == "nvml" and
              guard["external_lease"] is False, "strict lease incomplete")
    seconds = events[-1]["monotonic"] - events[0]["monotonic"]
    s.require(0 < seconds < r.LIMITS["seconds"], "lease time budget")
    lives = {group: r.verify_lifetime(out, "model-" + group, events, queries)
             for group in r.GROUPS}
    owned = {life["pid"] for life in lives.values()}
    s.require(queries and all(q["status"] == "OK" and set(q["pids"]) <= owned
                             for q in queries), "NVML invalid / foreign GPU context")
    s.require(all(not e["conflicts"] and not e["unreadable_processes"]
                  for e in events if e["event"] == "monitor"), "monitor conflict")
    direct = numerical(t.FOLDER / "execution")
    raw, summaries = matrix(old_plan, t.FOLDER / "execution", t.GROUPS[:2])
    mtp_raw, mtp_summaries = matrix(plan, out, r.GROUPS)
    raw.update(mtp_raw)
    summaries.update(mtp_summaries)
    comparisons = []
    for mode in ("plain", "mtp"):
        for case in plan["cases"]:
            a, b = (raw[mode + "-" + arm][case["id"]] for arm in ("parent", "candidate"))
            keys = ("prompt_sha256", "prompt_tokens", "completion_tokens", "text", "finish")
            s.require([[v[k] for k in keys] for v in a] == [[v[k] for k in keys] for v in b],
                      "parent/candidate output differs")
            if case["id"] != "quality":
                parent, candidate = (summaries[mode + "-" + arm][case["id"]]
                                     for arm in ("parent", "candidate"))
                comparisons.append(dict(mode=mode, case=case["id"], parent=parent,
                    candidate=candidate, ttft_gain=1-candidate["ttft_mean"]/parent["ttft_mean"],
                    decode_gain=candidate["decode_tps"]/parent["decode_tps"]-1,
                    latency_gain=1-candidate["latency_mean"]/parent["latency_mean"]))
    for group in r.GROUPS:
        log = (out / ("model-" + group) / "stderr.log").read_text()
        s.require(log.count("event=begin stage=validation") == 26, "actual received count")
        caps = read(out / (group + "-caps-wire.bin"))
        s.require(caps["runtime"]["mtp_enabled"] and not caps["runtime"]["media_enabled"],
                  "actual MTP/media mode")
        metrics = sorted(out.glob(group + "-idle-*-wire.bin"))
        s.require(metrics, "missing idle observation")
        values = s.parse_metrics(metrics[-1].read_text())
        s.require(values["q4t_requests_total"] == values["q4t_requests_success_total"] == 26 and
                  values["q4t_requests_error_total"] == values["q4t_requests_aborted_total"] == 0,
                  "actual request outcomes")
    pilot = read(r.BASE / "audit.json")
    targets = [p for p in pilot["pairs"] if p["target_case"]]
    s.require(pilot["status"] == "PILOT_TARGET_MET_PENDING_CONTRACTS" and len(targets) == 4 and
              all(p["latency_reduction"] >= .03 for p in targets), "pilot investment gate")
    audit = dict(status="PASS_COMPLETE_MATRIX_WITH_EXPLICIT_PREFIX_REUSE_PENDING_DECISION",
        source_commit=plan["source_commit"], audit_tool=dict(path=str(Path(__file__).resolve()),
        sha256=s.sha(__file__)), binaries=plan["binaries"], git_sources=git_sources,
        new_counts=counts, lease_seconds=seconds, nvml_queries=len(queries),
        reused=reused, new_lifetimes=lives, original_stage_status="ENV_ABORT",
        rows=raw, summaries=summaries, comparisons=comparisons, quality_checks=44,
        paired_responses=52, numerical=direct, pilot_pairs=pilot["pairs"],
        statistics="inconclusive; one start per identity/mode, no loss margin or CI",
        performance_accepted=False, default_unchanged=True,
        cleanup=postflight(plan, out, queries),
        limits=["No statistical noninferiority; no cross-start variance estimate",
                "Old ENV_ABORT and incomplete MTP are not relabelled",
                "Polling cannot exclude shorter or invisible interference",
                "Pilot structured SSE decode is only a buffered send tail"])
    s.save(AUDIT, audit)
    return {k: v for k, v in audit.items() if k in (
        "status", "git_sources", "new_counts", "lease_seconds", "nvml_queries", "comparisons")}


def seal():
    plan = read(r.FOLDER / "plan.json")
    paths = {Path(p) for p in plan["files"]}
    paths.update(p for p in r.FOLDER.rglob("*") if p.is_file())
    paths.update(p for p in (r.BASE / "mtp-completion-postflight").rglob("*") if p.is_file())
    paths.update(p for p in (r.BASE / "completion-run.log", r.BASE / "completion-prepare.log",
                            r.BASE / "completion-postflight-02.log") if p.is_file())
    paths.update((AUDIT, Path(__file__).resolve()))
    cas = r.BASE / "completion-originals"
    cas.mkdir(mode=0o700)
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
    s.save(MANIFEST, dict(files=files))
    return dict(files=len(files), objects=len(set(files.values())),
                manifest=str(MANIFEST), sha256=s.sha(MANIFEST))


def archive_abort():
    """Preserve the failed attempt and distinguish server and client counts."""
    reused = r.verify_reuse()
    plan = read(r.FOLDER / "plan.json")
    out = r.FOLDER / "execution"
    result, guard = read(out / "result.json"), read(r.FOLDER / "lease/exit.json")
    s.require(result["status"] == "FAILED" and guard["status"] == "ENV_ABORT" and
              result["counts"] == dict(starts=1, generations=15, controls=0, native=0),
              "unexpected aborted attempt")
    git_sources = source_identity(plan)
    events = r.jsonlines(r.FOLDER / "lease/events.jsonl")
    queries = r.jsonlines(r.FOLDER / "lease/nvml-queries.jsonl")
    failures = [q for q in queries if q["status"] != "OK"]
    s.require(len(failures) == 1 and failures[0]["status"] == "TIMEOUT" and
              failures[0]["returncode"] == -9 and failures[0]["pids"] is None,
              "unexpected NVML failure")
    api = [json.loads(x) for x in failures[0]["stdout"].splitlines()]
    s.require(api[-1]["api"] == "init" and api[-1]["event"] == "begin",
              "failed NVML API changed")
    s.require(events[0]["event"] == "lease_acquired" and
              events[-1]["event"] == "lease_released" and
              events[-1]["status"] == "ENV_ABORT" and
              all(not e["conflicts"] and not e["unreadable_processes"]
                  for e in events if e["event"] == "monitor"), "abort environment history")
    model = out / "model-mtp-candidate"
    identity, wait = read(model / "identity.json"), read(model / "waitable.json")
    s.require(wait["returncode"] == 0 and all(identity[k] == wait["identity"][k]
              for k in ("pid", "ppid", "pgid", "start_ticks")) and
              not (model / "reaped.json").exists() and not (out / "custody.json").exists() and
              not (out / "mtp-candidate-custody").exists(), "failed custody inventory")
    s.require(all(set(q["pids"]) <= {identity["pid"]}
                  for q in queries if q["status"] == "OK"), "foreign observed GPU context")
    log = (model / "stderr.log").read_text()
    received = log.count("event=begin stage=validation")
    completed = log.count("outcome=success reason=completed")
    cancelled = log.count("outcome=cancelled reason=server_shutdown")
    s.require((received, completed, cancelled) == (15, 14, 1), "server counts")
    subset = dict(plan, cases=[c for c in plan["cases"]
                              if c["id"] in ("context-1024", "context-4096",
                                             "context-8192", "context-45056")])
    raw, summaries = matrix(subset, out, ("mtp-candidate",))
    for path in (out / "client-mtp-candidate/context-204800").rglob("benchmark_data.db"):
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
            s.require(db.execute("select count(*) from result").fetchone()[0] == 0,
                      "200K client records changed")
    s.require(sum(len(v) for v in raw["mtp-candidate"].values()) == 12 and
              not (out / "client-mtp-candidate/quality").exists() and
              not (out / "model-mtp-parent").exists(), "incomplete matrix inventory")
    postroot = r.BASE / "mtp-completion-postflight"
    post = read(postroot / "result.json")
    post_guard = read(postroot / "lease-02/exit.json")
    s.require(post_guard["status"] == "COMPLETED" and post_guard["child_returncode"] == 0 and
              post["own_pids_absent"] and not any(post["own_pids"].values()) and
              post["port_free"] and post["gpu"]["status"] == "OK" and post["gpu"]["pids"] == [] and
              post["original_custody_complete"] is False, "post-abort cleanup observation")
    with (s.ROOT / ".q4t-work/gpu-exclusive.lock").open("r") as lock:
        stat = os.fstat(lock.fileno())
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s.require((stat.st_dev, stat.st_ino) == (plan["lock_device"], plan["lock_inode"]),
                  "shared lock identity changed")
    for name, digest in post["default_pair"].items():
        s.require(s.sha(s.ROOT / "build" / name) == digest == plan["files"][str(s.ROOT / "build" / name)],
                  "default pair changed")
    audit = dict(status="ENV_ABORT_MTP_COMPLETION_INCOMPLETE", performance_accepted=False,
        default_unchanged=True, source_commit=plan["source_commit"], binaries=plan["binaries"],
        audit_tool=dict(path=str(Path(__file__).resolve()), sha256=s.sha(__file__)),
        git_sources=git_sources, new_counts=result["counts"], actual_chat_received=received,
        server_completed=completed, server_cancelled=cancelled, raw_client_records=12,
        completed_without_persisted_client_records=2, quality_records=0, parent_starts=0,
        lease_seconds=events[-1]["monotonic"]-events[0]["monotonic"],
        nvml_queries=len(queries), failed_query=failures[0], guard=guard,
        process=dict(pid=identity["pid"], waitable_returncode=0, custody_complete=False),
        reused=reused, rows=raw, summaries=summaries, postflight=post,
        lock_available_after_postflight=True, statistics="not comparable; missing parent",
        decision="HOLD_USER_REQUESTED_WAIT_FOR_ENVIRONMENT",
        limitations=["Original and new ENV_ABORT retained; neither is performance acceptance",
            "1K/4K/8K/44K each three persisted candidate records; no complete MTP lifecycle",
            "200K two server completions have no persisted client records; third cancelled",
            "No quality questions or parent startup; no deployment health",
            "Postflight first command had invalid Python -m path; original COMMAND_FAILED retained",
            "Corrected cleanup-only lease has no model/HTTP tests; does not repair custody",
            "User selected retaining candidate and waiting for environment; no automatic retry"])
    s.save(AUDIT, audit)
    return {k: v for k, v in audit.items() if k not in (
        "rows", "summaries", "postflight", "reused", "failed_query", "guard", "binaries")}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("review", "seal", "archive-abort"))
    action = parser.parse_args().action
    print(json.dumps({"review": review, "seal": seal, "archive-abort": archive_abort}[action](),
                     ensure_ascii=False))
