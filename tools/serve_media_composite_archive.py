"""Retain completed composite acceptance records without running the model."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.serve_media_composite import stage as s

h = s.h
DEST = ROOT / "docs/evidence/serve-media-composite-20261103"
RELEASE = ROOT / ".q4t-work/vllm-release-20261103"


def verify(staged=False):
    h.verify_seal(DEST)
    manifest = h.read(DEST / "manifest.json")
    expected = {row["member"]: row for row in manifest["records"]}
    h.require(len(expected) == len(manifest["records"]), "duplicate record")
    seen = set()
    with tarfile.open(DEST / "raw-records.tar.gz", "r:gz") as archive:
        for member in archive:
            h.require(member.isfile() and member.name in expected
                      and member.name not in seen
                      and not Path(member.name).is_absolute()
                      and ".." not in Path(member.name).parts,
                      "unexpected archive member")
            row = expected[member.name]
            raw = archive.extractfile(member).read()
            h.require(len(raw) == row["bytes"]
                      and hashlib.sha256(raw).hexdigest() == row["sha256"],
                      "archived bytes changed")
            h.check_identity(row)
            seen.add(member.name)
    h.require(seen == set(expected), "missing record")
    for row in manifest["local_artifacts"]:
        h.check_identity(row)
    for name, row in manifest["aliases"].items():
        h.check_identity(row)
        h.require(h.sha(DEST / name) == row["sha256"], "alias changed")
    files = [p for p in DEST.rglob("*") if p.is_file()]
    if staged:
        for path in files:
            raw = subprocess.check_output(
                ["git", "show", ":" + str(path.relative_to(ROOT))], cwd=ROOT)
            h.require(hashlib.sha256(raw).hexdigest() == h.sha(path),
                      "staged blob changed")
    binding = h.read(s.OUTPUT / "release/binding.json")["new"]
    s.a.verify_hashes(ROOT / "build", binding)
    h.require(not list((ROOT / "build").glob("*recovery*.json")),
              "pending deployment recovery")
    return dict(passed=True, raw_records=len(seen),
                local_artifacts=len(manifest["local_artifacts"]),
                git_files=len(files), git_bytes=sum(p.stat().st_size for p in files),
                staged_blobs=len(files) if staged else 0, installed=binding,
                additional_model_or_http_requests=0, whole_goal_complete=False)


def create():
    s.check()
    for path in (s.OUTPUT, s.BASE / "rollback-bundle"):
        h.verify_seal(path)
    h.require(h.read(s.OUTPUT / "finalized.json")["status"] == "DELIVERED",
              "delivery incomplete")
    h.require(h.read(s.BASE / "postflight.json")["passed"], "postflight failed")
    DEST.mkdir()
    selected = [("stage/" + str(p.relative_to(s.BASE)), p)
                for p in sorted(s.BASE.rglob("*")) if p.is_file()]
    # The copied external approval/claim records remain local. Keep only our
    # stop verification and terminal cleanup result in the project archive.
    for name in ("authorization.json", "release-verification.json",
                 "restoration-terminal.json", "properties-before.txt",
                 "container-before.json"):
        selected.append(("service-release/" + name, RELEASE / name))
    records, local = [], []
    with (DEST / "raw-records.tar.gz").open("xb") as stream:
        with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w|") as archive:
                for member, path in selected:
                    h.require(not path.is_symlink(), "evidence symlink")
                    row = h.identity(path)
                    with path.open("rb") as source:
                        magic = source.read(4)
                    if magic == b"\x7fELF" or path.suffix == ".bundle":
                        local.append(row)
                        continue
                    raw = path.read_bytes()
                    h.require(hashlib.sha256(raw).hexdigest() == row["sha256"],
                              "record changed during archive")
                    item = tarfile.TarInfo(member)
                    item.size, item.mode, item.mtime = len(raw), 0o600, 0
                    archive.addfile(item, io.BytesIO(raw))
                    records.append(dict(**row, member=member))
    aliases = {
        "plan.json": s.PACKAGE / "plan.json",
        "result.json": s.OUTPUT / "business/result.json",
        "guard.json": s.OUTPUT / "lease/exit.json",
        "finalized.json": s.OUTPUT / "finalized.json",
        "native.json": s.OUTPUT / "native/result.json",
        "device.json": s.OUTPUT / "native-device/result.json",
        "vision.json": s.OUTPUT / "native-vision/result.json",
        "health.json": s.OUTPUT / "health/result.json",
        "analysis.json": s.BASE / "analysis.json",
        "postflight.json": s.BASE / "postflight.json",
        "service-release.json": RELEASE / "release-verification.json",
    }
    for name, path in aliases.items():
        h.copy_checked(h.identity(path), DEST / name)
    h.save(DEST / "manifest.json", dict(
        records=records, local_artifacts=local,
        aliases={name: h.identity(path) for name, path in aliases.items()},
        archiver=h.identity(Path(__file__)), archiver_added_after_freeze=True,
        scope="Raw CPU, native, HTTP wire, faults, samples, release and rollback "
              "identities. Prior ENV_ABORT observations remain historical. "
              "No physical RAM bound, blanket log redaction, broad media "
              "quality, long-soak or performance acceptance."))
    (DEST / "README.md").write_text(
        "# Composite media acceptance evidence\n\n"
        "Raw evidence preserves CPU preparation, native probes, actual HTTP "
        "wire, fault/recovery cases, samples, delivery and known rollback "
        "identities. Executables and source bundles remain local.\n\n"
        "SOAR service cleanup was explicitly authorized; its external source "
        "and original approval/claim records were preserved. The stopped unit "
        "is persistently masked. Earlier GPU-busy observations remain intact.\n\n"
        "Injected allocation/device errors do not represent physical OOM or "
        "device damage. Cancellation is cooperative. Fixed media cases do not "
        "prove broad multimodal quality, physical RAM protection, long-term "
        "stability or performance equivalence.\n")
    h.seal(DEST)
    return verify()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify", "custody"))
    args = parser.parse_args()
    result = create() if args.action == "create" else verify(
        staged=args.action == "custody")
    if args.action == "custody":
        result["checker"] = h.identity(Path(__file__))
        h.save(ROOT / "docs/evidence/serve-media-composite-custody-20261103.json",
               result)
    print(json.dumps(result))
