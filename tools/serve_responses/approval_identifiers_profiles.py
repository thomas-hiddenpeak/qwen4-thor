"""Verify that an issued MCP approval keeps its owner, target and decision."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = "approval_identifiers_handoff"
RELEASE = lifecycle.ROOT / "docs/evidence/serve-conversation-compaction-20261126/delivery.json"
PROOF = lifecycle.ROOT / "docs/evidence/serve-mcp-approval-identifiers-20261126/business.json"
LEGACY = PROOF.with_name("legacy-source.json")
ACTOR = Path(__file__).with_name("approval_identifiers_handoff_sdk.py")


def seed(source, store, witness=None):
    """Copy the closed historical fixture into a fresh private test store."""
    source, store = Path(source), Path(store)
    if witness is None:
        witness = package.read(LEGACY)
    package.require(not store.exists() and not store.is_symlink(),
                    "an uninitialized test store is required")
    store.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
    with (source / ".q4t-lock").open("rb") as lock, tempfile.TemporaryDirectory(
            dir=store.parent) as temporary:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        files = {p.name: p for p in source.iterdir() if p.name != ".q4t-lock"}
        members = witness["records"]
        package.require(files.keys() == members.keys(), "historical object set changed")
        staging = Path(temporary) / "objects"
        staging.mkdir(mode=0o700)
        size = 0
        for name, original in files.items():
            package.require(original.is_file() and not original.is_symlink(),
                            "expected an original regular file")
            output = staging / name
            with original.open("rb") as src, os.fdopen(os.open(
                    output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as dst:
                shutil.copyfileobj(src, dst)
            package.require(package.sha(output) == members[name], "historical object changed")
            size += output.stat().st_size
        package.require(size == witness["source_bytes"], "incomplete historical store")
        staging.rename(store)


def prepare(out, build, deployment, interpreter, source):
    direct = package.read(PROOF)
    legacy = package.read(LEGACY)
    version = subprocess.check_output([interpreter, "-IB", "-c",
        "import openai; print(openai.__version__)"], text=True, timeout=15).strip()
    package.require(direct["run"]["status"] == legacy["run"]["status"] == "PASS"
                    and version == direct["client"]["sdk"] == legacy["sdk"]
                    and direct["client"]["dual_field_readback"] is True
                    and direct["peer_reads"] == legacy["peer_reads"] == 1
                    and direct["product"]["engine"] == package.sha(build / "q4t")
                    and direct["product"]["ingress"] == package.sha(build / "q4t-ingress")
                    and legacy["release"] == package.read(RELEASE)["publication"]["primary"],
                    "SDK, approval fixture or binaries differ from verified identities")
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.COMPACT_REVIEWED,
                                KEY, [PROOF, LEGACY, RELEASE, ACTOR, Path(__file__)])
    store = out / "state/application-objects"
    seed(source, store)
    for name in ("primary", "light"):
        path = out / name / "plan.json"
        plan = package.read(path)
        command = plan["command"]
        package.require(Path(command[command.index("--object-store-dir") + 1]) == store,
                        "the capacity must reopen the same object store")
        plan[KEY]["interpreter"] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None):
    source = package.read(LEGACY)
    saved = row["saved"]
    hashes = row["wire_sha256"]
    original = ("approved_inputs", "denied_inputs", "approved_response", "denied_response")
    items = ("approved", "denied", "page")
    labels = set(original) | {"create", "independent"} | {"current_" + i for i in items}
    if previous is not None:
        labels.update("restored_" + i for i in items)
    package.require(row["sdk"] == source["sdk"] and row["independent_chat"] == "720041"
                    and package.same_json(row["original"], source["original"])
                    and package.same_json([saved["peer"], row["decision_conflict"], row["mismatch"]],
                                          [404, 409, 400])
                    and re.fullmatch(r"conv_[A-Za-z0-9_-]+", saved["conversation"])
                    and hashes.keys() == labels
                    and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes.values())
                    and all(hashes["current_" + i] == saved[i + "_sha256"] for i in items),
                    "original decision, scope, conflict or capture changed")
    if previous is None:
        package.require(row["restored"] is None, "unexpected restored conversation")
    else:
        prior = previous["saved"]
        package.require(package.same_json(row["restored"], prior)
                        and saved["conversation"] != prior["conversation"]
                        and package.same_json(previous["original"], source["original"])
                        and all(hashes[key] == previous["wire_sha256"][key] for key in original)
                        and all(hashes["restored_" + i] == prior[i + "_sha256"] for i in items),
                        "reopening changed the original conversation, item or response bytes")


def exercise(params, interpreter, errors, captures, previous=None):
    source = package.read(LEGACY)
    request = dict(params, capture=str(captures), citation=source["citation"], original=source["original"])
    if previous is not None:
        request["previous"] = previous["saved"]
    probe = subprocess.run([interpreter, "-IB", ACTOR], input=json.dumps(request), text=True,
                           stdout=subprocess.PIPE, stderr=errors, timeout=160, check=True)
    row = json.loads(probe.stdout)
    check(row, previous)
    return row
