"""Archive a finished numeric-suffix candidate; never decide or deploy it.

Prepared while sampling. Do not execute before root confirms final shutdown.
Copies only independent files, never hardlinks/chmods/deletes original evidence.
Requires an existing final decision and integrity audit; preserves their fields.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil


BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
STAGE = BASE / "acceptance-v1"
PLAN_SHA256 = "dbdfea2061ad866db0f6ba83bb8106f654d753958c4484b8ca249f7999b14902"
FOUR_FIELDS = ("evidence_validity", "measurement_complete",
               "statistical_conclusion", "engineering_decision")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_new(path, value):
    with path.open("x") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def default_identity(plan):
    binary, sidecar = ROOT / "build/q4t", ROOT / "build/q4t.release.json"
    expected_sidecar = plan["files"][str(STAGE / "parent/q4t.release.json")]
    actual = dict(binary_path=str(binary), binary_sha256=sha(binary),
                  release_path=str(sidecar), release_sha256=sha(sidecar))
    require(actual["binary_sha256"] == plan["parent_sha256"],
            "default program differs from frozen parent; archive is pre-deployment only")
    require(actual["release_sha256"] == expected_sidecar,
            "default release metadata differs from frozen parent")
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path,
                        help="new directory within this stage, e.g. archive-no-go")
    args = parser.parse_args()
    destination = args.output.resolve()
    require(destination.parent == BASE and not destination.exists(),
            "archive must be a new direct child of this stage")
    receipt_path = BASE / (destination.name + "-receipt.json")
    require(not receipt_path.exists(), "archive receipt already exists")
    require(sha(STAGE / "frozen-plan.json") == PLAN_SHA256, "frozen plan changed")
    plan = read(STAGE / "frozen-plan.json")
    run, lease = Path(plan["output"]), Path(plan["lease"])
    state, guard = read(run / "state.json"), read(lease / "exit.json")
    require(state.get("status") != "RUNNING" and "finished" in state,
            "stage has not reached a terminal state")
    require(guard.get("status") in ("COMPLETED", "COMMAND_FAILED", "ENV_ABORT") and
            "finished" in guard, "outer GPU guard has not exited")
    events = [json.loads(line) for line in (lease / "events.jsonl").read_text().splitlines()
              if line.strip()]
    require(any(e.get("event") == "lease_released" for e in events),
            "shared exclusive lease has not been released")
    decision_path = args.decision.resolve()
    require(decision_path.is_relative_to(BASE), "decision must belong to this stage")
    decision = read(decision_path)
    require(all(k in decision for k in FOUR_FIELDS), "decision lacks four independent fields")
    engineering = str(decision["engineering_decision"])
    require(engineering and "PENDING" not in engineering and engineering != "None",
            "a final engineering decision is required; archive does not choose one")
    require(decision.get("candidate_sha256") == plan["candidate_sha256"] and
            decision.get("frozen_plan_sha256") == PLAN_SHA256,
            "decision does not bind this frozen candidate and plan")
    integrity_path = BASE / "audit-evidence.json"
    integrity = read(integrity_path)
    require(integrity["identities"]["plan_sha256"] == PLAN_SHA256,
            "integrity audit belongs to another plan")
    require(all(decision[k] == integrity[k] for k in
                ["evidence_validity", "measurement_complete"]),
            "decision disagrees with integrity/measurement audit")
    for name in ["analysis.json", "audit-evidence.json", "audit-costs.json",
                 "audit-runtime-static.json", "build-identity.json"]:
        require((BASE / name).is_file(), "final report missing: " + name)
    current_default = default_identity(plan)
    require(sha(Path(plan["candidate"])) == plan["candidate_sha256"],
            "candidate executable changed")
    raw_seal = read(run / "files.json")
    for name, digest in raw_seal.items():
        require(not Path(name).is_absolute() and ".." not in Path(name).parts,
                "unsafe raw seal entry")
        require(sha(run / name) == digest, "raw run evidence changed: " + name)
    actual_raw = {str(p.relative_to(run)) for p in run.rglob("*")
                  if p.is_file() and p.name != "files.json"}
    require(actual_raw == set(raw_seal), "raw run inventory changed")
    require(integrity["identities"]["raw_seal_sha256"] == sha(run / "files.json"),
            "integrity audit does not bind current raw seal")
    for name, digest in integrity["environment"]["identities"].items():
        require(sha(lease / name) == digest, "lease evidence changed after audit")

    sources = {}
    forbidden = [Path(plan["model"]).resolve(), Path(plan["artifact"]).resolve(),
                 ROOT / "reference"]

    def add(source, relative):
        source = Path(source)
        require(source.is_file() and not source.is_symlink(),
                "archive accepts only regular source files: " + str(source))
        require(not any(source.resolve().is_relative_to(p) for p in forbidden),
                "model/subset/reference source copy is prohibited")
        require(source.suffix not in (".safetensors", ".bf16", ".fp8", ".nvfp4"),
                "model weight payload is prohibited: " + str(source))
        relative = Path(relative)
        require(not relative.is_absolute() and ".." not in relative.parts,
                "unsafe archive destination")
        key = str(relative)
        require(key not in sources, "duplicate archive destination: " + key)
        sources[key] = source

    # All stage data, including parent binary/cache/sidecar, inputs, raw run,
    # lease, frozen source snapshot, decision and any stage-level reports.
    for source in sorted(STAGE.rglob("*")):
        require(not source.is_symlink(), "symlink in stage evidence")
        if source.is_file():
            add(source, Path("acceptance-v1") / source.relative_to(STAGE))
    for name in ["q4t", "q4t_host_tests", "numeric_replay", "CMakeCache.txt"]:
        add(BASE / "build" / name, Path("candidate") / name)
    # Retain actual compiler/link metadata without duplicating all build objects.
    for pattern in ["CMakeFiles/*/flags.make", "CMakeFiles/*/link.txt",
                    "CMakeFiles/*/includes_CUDA.rsp",
                    "CMakeFiles/*/CMakeCXXCompiler.cmake",
                    "CMakeFiles/*/CMakeCUDACompiler.cmake"]:
        for source in sorted((BASE / "build").glob(pattern)):
            add(source, Path("candidate/build-metadata") / source.relative_to(BASE / "build"))
    # Capture all current analysis/audit scripts, full/partial reports and logs;
    # partial evidence is retained and never replaces the final report.
    for source in sorted(BASE.iterdir()):
        if source.is_file() and source.suffix in (".py", ".json", ".log", ".md", ".txt"):
            if source.name.endswith("-receipt.json"):
                continue
            add(source, source.name)
    # Always retain the exact decision even if root placed it in a separate
    # review subdirectory rather than directly in BASE or STAGE.
    add(decision_path, "disposition/final-decision.json")
    external = {Path(record["path"]) for record in plan["reused_evidence"]["records"]}
    external.add(ROOT / ".q4t-work/suffix-k3-20261023/analyze.py")
    for name in plan["files"]:
        path = Path(name)
        if (path.is_relative_to(ROOT / ".q4t-work") and not path.is_relative_to(BASE)
                and not any(path.resolve().is_relative_to(p) for p in forbidden)):
            external.add(path)
    external_bindings = []
    for number, source in enumerate(sorted(external)):
        require(source.stat().st_size <= 32 * 1024 * 1024,
                "unexpected large external dependency; list it for review")
        relative = Path("external-evidence") / (str(number) + "-" + source.name)
        add(source, relative)
        external_bindings.append(dict(original=str(source), copy=str(relative), sha256=sha(source)))
    # Every executable source snapshot file is bound by its original checkout
    # path in plan.files. worktree.patch is auxiliary metadata, not executable.
    snapshot = Path(plan["source_snapshot"])
    for source in snapshot.rglob("*"):
        if source.is_file() and source.name != "worktree.patch":
            key = str(ROOT / source.relative_to(snapshot))
            require(key in plan["files"] and sha(source) == plan["files"][key],
                    "frozen source snapshot differs from plan: " + key)
    selected_sources = set(sources.values())
    for name, digest in plan["files"].items():
        source = Path(name)
        if source in selected_sources:
            require(sha(source) == digest,
                    "frozen archive input changed after audit: " + name)
    source_records = {name: dict(original=str(source), size=source.stat().st_size,
                                sha256=sha(source)) for name, source in sources.items()}
    logical_bytes = sum(r["size"] for r in source_records.values())
    require(shutil.disk_usage(BASE).free >= logical_bytes + 1024 * 1024 * 1024,
            "insufficient space for independent archive copies plus 1 GiB reserve")
    destination.mkdir()
    # Any failure leaves this new directory and originals untouched for review;
    # there is no automatic deletion, retry or overwrite/resume path.
    for name, source in sources.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        require(sha(target) == source_records[name]["sha256"], "copy identity failed: " + name)
    retention = dict(
        archive_kind="research candidate evidence; not a deployment package",
        created_utc=datetime.now(timezone.utc).isoformat(),
        decision={k: decision[k] for k in FOUR_FIELDS},
        decision_path=str(decision_path), decision_sha256=sha(decision_path),
        candidate_sha256=plan["candidate_sha256"], frozen_plan_sha256=PLAN_SHA256,
        original_stage=str(STAGE), original_paths_must_be_retained=True,
        source_records=source_records, external_evidence=external_bindings,
        model_read_only_reference=plan["model"], model_file_metadata=plan["model_file_metadata"],
        subset_read_only_reference=plan["artifact"],
        subset_manifest_sha256=plan["subset_manifest_sha256"],
        default_identity=current_default, deployment_performed=False,
        weight_payloads_copied=False,
        notes=["No original file is linked, chmodded, rewritten, moved or deleted.",
               "Absolute paths in original tools/evidence retain their meaning; originals remain required.",
               "The archive stores the supplied decision; it does not approve performance or release."])
    save_new(destination / "retention.json", retention)
    entries = [dict(path=str(p.relative_to(destination)), size=p.stat().st_size, sha256=sha(p))
               for p in sorted(destination.rglob("*")) if p.is_file()]
    save_new(destination / "files.json", entries)
    for entry in entries:
        p = destination / entry["path"]
        require(p.stat().st_size == entry["size"] and sha(p) == entry["sha256"],
                "final archive seal check failed: " + entry["path"])
    for name, source in sources.items():
        require(sha(source) == source_records[name]["sha256"],
                "source changed during archival: " + str(source))
    require(default_identity(plan) == current_default, "default identity changed during archive")
    receipt = dict(archive_path=str(destination), archive_files=len(entries),
                   archive_seal_sha256=sha(destination / "files.json"),
                   archive_seal_verified=True, copied_payload_bytes=logical_bytes,
                   candidate_sha256=plan["candidate_sha256"], frozen_plan_sha256=PLAN_SHA256,
                   decision_sha256=sha(decision_path),
                   decision_fields={k: decision[k] for k in FOUR_FIELDS},
                   default_identity=current_default, deployment_performed=False,
                   original_paths_retained=True, weight_payloads_copied=False)
    save_new(receipt_path, receipt)
    print(json.dumps(receipt, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
