"""Complete only the interrupted MTP pair; never rewrite the aborted stage."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from tools.evalscope import accept_vision_rowmax as prior

s = prior.s
read = prior.pilot.read
BASE = prior.BASE
FOLDER = BASE / "mtp-completion-01"
LIMITS = dict(starts=2, generations=52, controls=16, native=0, seconds=3600)
GROUPS = ("mtp-candidate", "mtp-parent")
REUSED = ("kernel", "hf-small", "hf-rectangular",
          "model-plain-parent", "model-plain-candidate")


def jsonlines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def verify_archive(manifest, cas):
    files = read(manifest)["files"]
    for digest in set(files.values()):
        s.require(s.sha(cas / digest) == digest, "sealed original changed")
    return files


def verify_lifetime(out, name, events, queries):
    folder = out / name
    identity, wait, reaped = (read(folder / (n + ".json"))
                              for n in ("identity", "waitable", "reaped"))
    custody = read(out / (name.removeprefix("model-") + "-custody/custody.json"))
    s.require(wait["returncode"] == 0 and wait["identity"]["state"] == "Z" and
              all(identity[k] == wait["identity"][k]
                  for k in ("pid", "ppid", "pgid", "start_ticks")) and
              custody["retained"] == [wait["identity"]] and
              reaped == dict(pid=identity["pid"], returncode=0),
              "incomplete lifetime: " + name)
    samples = [e for e in events if e["event"] == "monitor"]
    post = next(e for e in samples if e["monotonic"] > wait["monotonic"])
    s.require(all(not e["conflicts"] and not e["unreadable_processes"]
                  for e in samples if e["monotonic"] <= post["monotonic"]),
              "invalid monitor prefix")
    ack = custody["acknowledgement"]
    matching = [q for q in queries if q["request_id"] == ack["request_id"]]
    s.require(len(matching) == 1 and matching[0]["status"] == "OK" and
              identity["registered_monotonic"] <= ack["query_begin"] <=
              ack["query_end"] <= ack["monitor"]["monotonic"], "custody query")
    # Failures after this completed lifetime do not become successes. Only the
    # prefix through its post-exit observation is eligible for independent reuse.
    prefix = []
    for query in queries:
        api = [json.loads(x) for x in query.get("stdout", "").splitlines()]
        s.require(api, "missing NVML timeline")
        if api[0]["monotonic"] <= post["monotonic"]:
            s.require(query["status"] == "OK", "NVML gap in reused lifetime")
            prefix.append(query)
    owned = {read(p)["pid"] for p in out.glob("*/identity.json")}
    s.require(prefix and prefix[0]["pids"] == [] and
              all(set(q["pids"]) <= owned for q in prefix),
              "unexpected GPU process in reused prefix")
    return dict(pid=identity["pid"], through_monotonic=post["monotonic"],
                nvml_queries=len(prefix), custody_complete=True)


def verify_reuse(*, archive_only=False):
    seals = {}
    for manifest, cas in (("originals.json", "originals"),
                          ("accept-originals.json", "accept-originals")):
        seals[manifest] = verify_archive(BASE / manifest, BASE / cas)
    s.require(s.sha(BASE / "audit.json") ==
              seals["originals.json"][str(BASE / "audit.json")],
              "pilot audit differs from sealed original")
    original = read(prior.FOLDER / "plan.json")
    archived = seals["accept-originals.json"]
    # All selected originals must still equal the sealed bytes; source files
    # have legitimately advanced, so their provenance is checked from the CAS.
    for path, digest in archived.items():
        if Path(path).is_relative_to(prior.FOLDER) or path == str(BASE / "acceptance-audit.json"):
            s.require(s.sha(path) == digest, "original evidence changed: " + path)
    tree = subprocess.check_output(
        ["git", "ls-tree", "-r", "-z", original["source_commit"]], cwd=s.ROOT)
    blobs = {item.split(b"\t", 1)[1].decode(): item.split(b"\t", 1)[0].split()[2].decode()
             for item in tree.split(b"\0") if item}
    git_sources = 0
    runtime_drift = {}
    for name, digest in original["files"].items():
        s.require(archived[name] == digest, "original input/seal mismatch")
        path = Path(name)
        if path.is_relative_to(s.ROOT):
            relative = str(path.relative_to(s.ROOT))
            if relative in blobs:
                data = (BASE / "accept-originals" / digest).read_bytes()
                blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                s.require(blob == blobs[relative], "old Git source mismatch")
                git_sources += 1
            if relative == "CMakeLists.txt" or relative.startswith(("src/", "include/", "cmake/")):
                current = s.sha(path)
                if current != digest:
                    s.require(archive_only, "tested runtime source changed")
                    runtime_drift[relative] = dict(tested=digest, current=current)
    for identity in original["binaries"].values():
        s.require(s.sha(identity["path"]) == identity["sha256"], "tested binary changed")
    out = prior.FOLDER / "execution"
    s.require(read(prior.FOLDER / "lease/exit.json")["status"] == "ENV_ABORT" and
              read(out / "result.json")["status"] == "FAILED" and
              read(BASE / "decision.json")["performance_accepted"] is False,
              "original failed stage must remain unchanged")
    events = jsonlines(prior.FOLDER / "lease/events.jsonl")
    queries = jsonlines(prior.FOLDER / "lease/nvml-queries.jsonl")
    lives = {name: verify_lifetime(out, name, events, queries) for name in REUSED}
    failure_times = [json.loads(q["stdout"].splitlines()[0])["monotonic"]
                     for q in queries if q["status"] != "OK"]
    s.require(failure_times and max(v["through_monotonic"] for v in lives.values()) <
              min(failure_times), "reused evidence overlaps monitor failure")
    return dict(status="VERIFIED_COMPLETED_PREFIX_ONLY", git_sources=git_sources,
                lifetimes=lives, original_stage_status="ENV_ABORT",
                archive_only=archive_only, runtime_drift=runtime_drift,
                excluded=["original partial MTP", "diagnostic MTP samples"],
                seals={name: s.sha(BASE / name) for name in seals})


def prepare():
    reused = verify_reuse()
    old = read(prior.FOLDER / "plan.json")
    FOLDER.mkdir(mode=0o700)
    s.save(FOLDER / "reuse.json", reused)
    inputs = FOLDER / "inputs"
    inputs.mkdir()
    cases = copy.deepcopy(old["cases"])
    for case in cases:
        source = Path(case["path"])
        s.require(s.sha(source) == old["files"][str(source)], "frozen fixture changed")
        destination = inputs / source.name
        shutil.copyfile(source, destination)
        case["path"] = str(destination)
    paths = [s.ROOT / name for name in subprocess.check_output(
        ["git", "ls-files"], cwd=s.ROOT, text=True).splitlines()
        if name.startswith(("src/", "include/", "cmake/", "tools/", "tests/"))
        or name == "CMakeLists.txt"]
    paths += [Path(v["path"]) for v in old["binaries"].values()]
    paths += [BASE / n for n in ("audit.json", "acceptance-audit.json", "originals.json",
        "accept-originals.json", "decision.json")]
    paths += [prior.FOLDER / "plan.json", FOLDER / "reuse.json",
        s.ROOT / "docs/VISION_ROWMAX_COMPLETION_2026-11-07.md",
        s.ROOT / "build/q4t", s.ROOT / "build/q4t.release.json", prior.pilot.EVALSCOPE]
    paths += list(inputs.iterdir())
    paths += [p for p in (BASE / "completion-cpu").iterdir() if p.is_file()]
    package = prior.pilot.EVALSCOPE.parent.parent / "lib/python3.12/site-packages/evalscope"
    paths += sorted(package.rglob("*.py"))
    paths += [s.MODEL / n for n in ("config.json", "tokenizer.json",
        "model.safetensors.index.json", "preprocessor_config.json")]
    lock = (s.ROOT / ".q4t-work/gpu-exclusive.lock").stat()
    s.require((lock.st_dev, lock.st_ino) == (old["lock_device"], old["lock_inode"]),
              "shared lock replaced")
    s.save(FOLDER / "plan.json", dict(profile="vision-rowmax-mtp-completion-v1",
        limits=LIMITS, groups=GROUPS, cases=cases, quality=old["quality"],
        port=s.PORT, model=str(s.MODEL), binaries=old["binaries"],
        files={str(p): s.sha(p) for p in paths},
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=s.ROOT, text=True).strip(), lock_device=lock.st_dev, lock_inode=lock.st_ino,
        reuse=reused, performance_acceptance=False, default_unchanged=True))
    return "PREPARED_MTP_PAIR_ONLY"


class Run(prior.Run):
    limits = LIMITS
    groups = GROUPS
    modes = ("mtp",)

    def contracts(self):
        # No native GPU calls: these exact contracts completed before the abort.
        s.require(self.plan["reuse"]["status"] == "VERIFIED_COMPLETED_PREFIX_ONLY",
                  "missing independent reuse verification")


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "verify-reuse"))
    action = parser.parse_args().action
    result = {"prepare": prepare, "run": lambda: Run(FOLDER).run(),
              "verify-reuse": verify_reuse}[action]()
    print(json.dumps(result, ensure_ascii=False))
