"""Rebuild both exact qualifications from public evidence, without model work."""
import argparse
import copy
import hashlib
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from tools.serve_ops import contracts
from tools.serve_recovery import profile_evidence, profile_mcp_reuse, profile_record
from tools.serve_recovery.profile_mcp_source import BINDING_GUARD


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def member(archive, name, expected=None):
    with tarfile.open(archive, "r:gz") as source:
        item = source.getmember(name)
        if not item.isfile() or item.size > 16 << 20:
            raise ValueError("Invalid bounded evidence member: " + name)
        raw = source.extractfile(item).read()
    if expected and digest(raw) != expected:
        raise ValueError("Evidence member identity changed: " + name)
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if not any(output.is_relative_to(ROOT / name)
               for name in ("build", ".q4t-work")) or output.exists():
        parser.error("use a new directory under build/ or .q4t-work/")
    accepted = json.loads((HERE / "acceptance.json").read_text())
    archive = HERE / "evidence.tar.gz"
    if digest(archive.read_bytes()) != accepted["archive_sha256"]:
        raise ValueError("Delivery archive identity changed")
    records = member(archive, "qualification/qualified-records.json")["records"]
    plan = member(archive, "qualification/plan.json")
    qualification = member(archive, "qualification/acceptance.json")
    registry = member(archive, "qualification/qualified/registry.json")
    output.mkdir(parents=True, mode=0o700)
    result = {}
    for name in ("primary", "light"):
        record = records[name]
        previous = ROOT / record["prior_public_archive"]
        witness = ROOT / record["actual_new_witness_archive"]
        for path, expected in ((previous, record["prior_public_archive_sha256"]),
                               (witness, record["actual_new_witness_archive_sha256"])):
            if digest(path.read_bytes()) != expected:
                raise ValueError("Public source archive changed")
        value = member(previous, record["prior_member"],
                       BINDING_GUARD["profiles"][name]["prior_record_sha256"])
        old_business = copy.deepcopy(value["business"])
        config = member(archive, "qualification/" + name + "-config.json")
        value.update(
            identity=plan["identity"],
            capabilities=member(archive, "qualification/" + name + "-capabilities.json"),
            configuration=contracts.configuration(config, version=5),
            startup_memory=config["startup_memory"]["checks"]["before_allocations"],
            environment=qualification["environment"])
        value = profile_mcp_reuse.attach_binding_guard(
            value, member(witness, record["witness_member"]))
        if value["business"] != old_business:
            raise ValueError("Original business or failure changed")
        path = output / (name + ".json")
        profile_record.save(path, value)
        actual = digest(path.read_bytes())
        if actual != record["sha256"] or path.stat().st_size != record["bytes"]:
            raise ValueError("Reconstructed qualification differs")
        profile_evidence.validate(profile_record.read(path), registry, name, actual)
        result[name] = dict(sha256=actual, bytes=path.stat().st_size)
    print(json.dumps(dict(status="PASS_EXACT_PUBLIC_RECONSTRUCTION", records=result)))


if __name__ == "__main__":
    main()
