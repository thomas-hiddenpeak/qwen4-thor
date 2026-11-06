"""Reconstruct quality, timing and custody from sealed raw evidence offline."""
import argparse
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess

from tools.vision_service import timing_stage as t, timing_analysis as a

s = t.s


def seal():
    plan = t.read(t.FOLDER / "plan.json")
    paths = {Path(p) for p in plan["files"]}
    paths.update(p for directory in (t.FOLDER, t.BASE / "logs")
                 for p in directory.rglob("*") if p.is_file())
    cas = t.BASE / "originals"
    cas.mkdir()
    files = {}
    for path in sorted(paths):
        digest = s.sha(path)
        if str(path) in plan["files"]:
            s.require(digest == plan["files"][str(path)], "source drift: " + str(path))
        target = cas / digest
        if not target.exists():
            shutil.copyfile(path, target)
        s.require(s.sha(target) == digest, "original copy differs")
        files[str(path)] = digest
    s.save(t.BASE / "originals.json", dict(files=files))
    return len(files)


def audit():
    files = t.read(t.BASE / "originals.json")["files"]
    cas = t.BASE / "originals"
    for digest in set(files.values()):
        s.require(s.sha(cas / digest) == digest, "original drift")

    def raw(name):
        path = Path(name)
        if not path.is_absolute():
            path = t.FOLDER / path
        return (cas / files[str(path)]).read_bytes()

    def read(name):
        return json.loads(raw(name))

    plan = read("plan.json")
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
        if not path.is_relative_to(s.ROOT):
            continue
        relative = str(path.relative_to(s.ROOT))
        if relative in blobs:
            data = raw(path)
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            s.require(blob == blobs[relative], "Git/source mismatch: " + relative)
            git_sources += 1
    result = read("execution/result.json")
    # Preserve the failed original gate. The only recovery allowed here is
    # offline use of already-finished HTTP/timing observations after the exact
    # known driver ledger defect; this is not a repaired whole-stage PASS.
    ledger_failure = (result["status"] == "FAILED" and
        "retained process identity drift" in (result["failure"] or "") and
        len(result["cleanup_errors"]) == 1 and
        "retained process identity drift" in result["cleanup_errors"][0])
    s.require(ledger_failure and not result["monitor_failures"],
              "unexpected phase failure; cannot reuse observations")
    for kind, count in result["counts"].items():
        attempts = [p for p in files if p.startswith(str(t.FOLDER / ("execution/attempt-" + kind + "-")))]
        s.require(len(attempts) == count <= plan["limits"][kind], "attempt count mismatch")
    s.require(result["counts"]["starts"] == 2 and result["counts"]["generations"] == 18 and
              result["counts"]["native"] == 0, "unexpected run size")
    events = [json.loads(line) for line in raw("lease/events.jsonl").splitlines()]
    s.require(events[0]["event"] == "lease_acquired" and events[-1]["event"] == "lease_released",
              "lease boundaries")
    s.require(read("lease/exit.json")["status"] == "COMMAND_FAILED", "environment invalid")
    samples = [e for e in events if e["event"] == "monitor"]
    s.require(samples and all(not e["conflicts"] and not e["unreadable_processes"] for e in samples),
              "guard conflict/monitor failure")
    lease_seconds = events[-1]["monotonic"] - events[0]["monotonic"]
    s.require(lease_seconds < plan["limits"]["seconds"], "lease budget")
    owned = []
    for mode in ("off", "on"):
        model = "execution/model-" + mode + "/"
        identity, wait = (read(model + f + ".json") for f in ("identity", "waitable"))
        s.require(wait["returncode"] == 0 and wait["identity"]["state"] == "Z",
                  "owned model did not become waitable with exit zero")
        for key in ("pid", "start_ticks", "pgid", "ppid"):
            s.require(identity[key] == wait["identity"][key], "process identity drift")
        if mode == "off":
            retained = read("execution/off-custody/custody.json")["retained"]
            reaped = read(model + "reaped.json")
            s.require(retained == [wait["identity"]] and reaped["pid"] == identity["pid"] and
                      reaped["returncode"] == 0, "first-mode custody differs")
        else:
            s.require(str(t.FOLDER / (model + "reaped.json")) not in files,
                      "original missing second reap receipt was rewritten")
        s.require(any(e["monotonic"] > wait["monotonic"] for e in samples), "no post-exit guard sample")
        owned.append(identity["pid"])
    queries = [json.loads(line) for line in raw("lease/nvml-queries.jsonl").splitlines()]
    s.require(queries and all(q["status"] == "OK" and set(q["pids"]) <= set(owned)
                             for q in queries), "NVML failure or foreign context")
    last = [json.loads(line) for line in queries[-1]["stdout"].splitlines()][-1]
    s.require(last["event"] == "result" and last["status"] == "OK" and not last["pids"] and
              last["monotonic"] > read("execution/model-on/waitable.json")["monotonic"],
              "no post-exit empty GPU observation")
    cleanup = t.read(t.BASE / "cleanup-after-stage.json")
    s.require([r["pid"] for r in cleanup["owned"]] == owned and
              all(not r["exists"] for r in cleanup["owned"]) and
              cleanup["gpu"]["status"] == "OK" and not cleanup["gpu"]["pids"] and
              cleanup["port_free"] and cleanup["lock_available"], "resources not released")
    s.require(not a.parse_records(raw("execution/model-off/stderr.log").decode()), "disabled timing logged")
    timings = a.parse_records(raw("execution/model-on/stderr.log").decode())
    rows = read("requests.json")
    expected_ids = {r["request"]["request_id"] for r in rows
                    if r["mode"] == "on" and a.input_counts(r["request"])["items"]}
    s.require(set(timings) == expected_ids, "timing record coverage")
    off, observations = [], []
    on_index = 0
    for row in rows:
        label = row["label"]
        body = read("execution/" + label + "-request.json")["body"]
        s.require(body == row["request"], "fixed input changed")
        transport = read("execution/" + label + "-transport.json")
        s.require(transport["status"] == 200, "HTTP failure")
        wire = io.BytesIO(raw("execution/" + label + "-wire.bin"))
        value = (s.client.read_sse if body.get("stream") else s.client.read_json)(wire)
        s.require(value.id == body["request_id"] and value.model == body["model"], "response identity")
        s.require(asdict(value) == read("execution/" + label + "-completion.json"), "derived response differs")
        actual = json.loads(value.message["content"])
        s.Draft202012Validator(body["response_format"]["json_schema"]["schema"]).validate(actual)
        pre = read("execution/" + label + "-preflight-wire.bin")
        s.require(actual == row["expected"] and value.finish_reason == "stop" and
                  value.usage["prompt_tokens"] == pre["prompt_tokens"], "quality/terminal/usage")
        same = (value.message, value.finish_reason, value.usage)
        if row["mode"] == "off":
            off.append(same)
        else:
            if on_index < len(off):
                s.require(same == off[on_index], "timing changed output")
                # Request identities differ intentionally; model input is identical.
                original = rows[on_index]["request"]
                for key in set(body) | set(original):
                    if key not in ("request_id", "cancel_token"):
                        s.require(body.get(key) == original.get(key), "mode input changed")
            on_index += 1
        timing = timings.get(body["request_id"])
        if timing:
            a.validate_request(timing, body)
            s.require(timing["total_ms"] < transport["seconds"] * 1000, "pipeline exceeds E2E")
        observations.append(dict(label=label, case=row["case"], mode=row["mode"],
            e2e_seconds=transport["seconds"], usage=value.usage, actual=actual, timing=timing))
    protocol = read(t.BASE / "engine/generated/q4t/server/chat_protocol_sources.json")
    protocol_sha = hashlib.sha256("".join(f"{name}:{protocol[name]}\n" for name in sorted(protocol)).encode()).hexdigest()
    for name, digest in protocol.items():
        s.require(plan["files"][str(s.ROOT / name)] == digest, "protocol source drift")
    return dict(status="SCOPED_TIMING_ACCEPTED_WITH_DRIVER_FAILURE", source_commit=plan["source_commit"],
        binary_sha256=plan["files"][plan["binary"]], protocol_sha256=protocol_sha,
        file_bindings=len(files), git_source_bindings=git_sources, counts=result["counts"],
        lease_seconds=lease_seconds, owned_pids=owned, final_gpu_empty=True,
        original_stage_status=result["status"], original_guard_status="COMMAND_FAILED",
        second_reap_receipt_missing=True, post_exit_nvml_empty=True,
        cleanup_observation_sha256=s.sha(t.BASE / "cleanup-after-stage.json"),
        verifier_sha256=s.sha(Path(__file__)),
        default_unchanged=True, performance_acceptance=False, observations=observations)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seal", "audit"))
    args = parser.parse_args()
    if args.action == "seal":
        print(seal())
    else:
        result = audit()
        s.save(t.BASE / "audit.json", result)
        print(json.dumps({k: v for k, v in result.items() if k != "observations"}, indent=2))
