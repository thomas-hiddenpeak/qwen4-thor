"""Accepted historical source and task data, copied only into new private fixtures."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tarfile

from tools.project_tasks import engine, migration, store

BASE = store.ROOT / ".q4t-work/serve-task-migration-20261104"


def release(index):
    return json.loads((store.ROOT / migration.REGISTRY).read_bytes())["releases"][index]


def archive(index):
    selected = release(index)
    raw = (store.ROOT / selected["evidence"]).read_bytes()
    store.require(hashlib.sha256(raw).hexdigest() == selected["archive_sha256"], "historical archive identity")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as source:
        return {member.name: source.extractfile(member).read() for member in source if member.isfile()}


def extract_runtime(index, root):
    root = store.local(root); root.mkdir(mode=0o700)
    data, selected = archive(index), release(index)
    for name, checksum in selected["runtime_sources"].items():
        candidates = [raw for path, raw in data.items() if path.endswith("/source/" + name) and
                      hashlib.sha256(raw).hexdigest() == checksum]
        store.require(candidates, "accepted source snapshot unavailable: " + name)
        target = root / name; target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        store.write_new(target, candidates[0])
    return dict(root=str(root), release=selected["release"], sources=selected["runtime_sources"])


def clone_history(index, root, key, boundary):
    """Rebase actual events to a new fixture store; never change original evidence."""
    data = archive(index)
    exported_path = ("tls-correction/execution/known-ledger.json" if index == 0 else "execution/alpha-ledger.json")
    exported = json.loads(data[exported_path])
    spec = exported["meta"]["spec"]
    name = store.task_name(spec["binding"]["client_id"], key)
    store.create(root, name, spec)
    events = []
    for event in exported["events"]:
        events.append(deepcopy(event))
        if event["kind"] == boundary:
            break
    store.require(boundary is None or events[-1]["kind"] == boundary, "historical boundary absent")
    prefix = ("tls-correction/" if index == 0 else "") + "tasks/tasks/" + exported["meta"]["task_id"] + "/"
    with store.open_task(root, name) as ledger:
        for stem, kind in (("wire", "request"), ("check", "preflight_started")):
            for number in range(1, sum(row["kind"] == kind for row in events) + 1):
                file = f"{stem}-{number:03d}.bin"
                store.write_new(ledger.path / file, data[prefix + file])
        for row in events:
            if row["kind"] in ("preflight_reset", "resolve"):
                previous = ledger.events()[-1]["digest"] if ledger.events() else ledger.initial
                row["payload"]["revision" if row["kind"] == "preflight_reset" else "expected_revision"] = previous
            ledger.append(row["kind"], row["payload"])
        engine.replay(ledger)
    response = [row for row in exported["events"] if row["kind"] == "response"][-1]["payload"]["completion"]
    return dict(name=name, binding=spec["binding"], spec=spec, final=response,
                final_wire=data[prefix + "wire-002.bin"])
