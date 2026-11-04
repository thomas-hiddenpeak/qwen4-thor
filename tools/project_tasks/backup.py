"""Private consistent snapshots and explicit single-active-store handoff."""
import argparse
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import store, retention as r

SNAPSHOT = "q4t.project.snapshot.v1"
RESTORE = "q4t.project.restore.v1"
PLACEMENT = "q4t.project.store.placement.v1"
MAX_FILES = 700000
MAX_MANIFEST = 256 * 1024**2
MAX_HANDOFFS = 8


def check_placement(root, meta, *, logical_root=None):
    value = r.read(root / "restored.json")
    store.require(value["format"] == PLACEMENT and value["store_id"] == meta["store_id"] and
        value["root"] == str(logical_root or root) and value["identity"] == r.identity(root.stat()) and
        1 <= len(value["history"]) <= MAX_HANDOFFS, "restored store placement")
    previous = "0" * 64
    for number, event in enumerate(value["history"], 1):
        store.require(event["seq"] == number and event["previous"] == previous and
            event["digest"] == store.digest([number, event["payload"], previous]), "store placement chain")
        previous = event["digest"]
    store.require(value["history"][-1]["payload"]["destination"] == value["root"] and
        value["history"][-1]["payload"]["identity"] == value["identity"], "store placement destination")
    return value


def regular_tree(root, *, exclude=()):
    """Exact private tree, with bounded entry count; no links or special files."""
    store.private(root, True)
    directories, files = {".": r.identity(root.stat())}, {}
    pending = [root]
    while pending:
        folder = pending.pop()
        for path in sorted(folder.iterdir()):
            relative = path.relative_to(root).as_posix()
            if relative in exclude: continue
            store.require(len(files) + len(directories) < MAX_FILES and len(relative) <= 512,
                          "snapshot entry/path budget")
            if path.is_dir() and not path.is_symlink():
                directories[relative] = r.identity(store.private(path, True)); pending.append(path)
            else:
                info = store.private(path)
                store.require(info.st_size <= store.RESERVED_BYTES, "snapshot individual file size")
                files[relative] = dict(identity=r.identity(info), bytes=info.st_size, sha256=r.sha(path))
    return dict(directories=directories, files=files)


@contextmanager
def readonly_db(path):
    store.private(path)
    store.require(not os.path.lexists(path.with_name(path.name + "-journal")), "unsettled backup journal")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        store.require(db.execute("PRAGMA quick_check").fetchall() == [("ok",)], "backup database integrity")
        yield db
    finally: db.close()


def audit(root, meta, *, physical, logical_root=None):
    """Rebuild states from actual ledgers; never call a model or a tool."""
    from tools.project_tasks import engine, migration
    if physical: store.checked_root(root, allow_handoff=True, logical_root=logical_root)
    store.require(meta["format"] == store.FORMAT and meta["uid"] == os.getuid() and
        type(meta["max_tasks"]) is int and 1 <= meta["max_tasks"] <= 64 and
        meta["reserved_bytes_per_task"] == store.RESERVED_BYTES, "snapshot store policy")
    allowed = {".lock", "store.json", "tasks", "retention", "restored.json", "handoff.json", ".handoff.next"}
    store.require({path.name for path in root.iterdir()} <= allowed, "unknown snapshot root entry")
    current = engine.sources()
    known = {store.digest(current), *(store.digest(row["runtime_sources"]) for row in
        json.loads((ROOT / migration.REGISTRY).read_bytes())["releases"])}
    tasks = {}
    for folder in sorted((root / "tasks").iterdir()):
        store.require(re.fullmatch(r"[0-9a-f]{64}", folder.name) and len(tasks) < meta["max_tasks"], "snapshot active task capacity")
        r.files(folder)
        with readonly_db(folder / "ledger.db") as db:
            ledger = store.Ledger(folder, db, meta["store_id"])
            state = engine.replay(ledger); spec = state["execution_spec"]
            store.require(store.digest(spec["runtime_sources"]) in known, "unregistered snapshot task runtime")
            tasks[folder.name] = dict(state=state["phase"], revision=state["revision"],
                counters=migration.counters(state), meta_sha256=store.digest(ledger.meta),
                spec_sha256=store.digest(spec), public_sha256=store.digest(engine.public(ledger, state)))
    keys, policy = {}, None
    path = root / "retention"
    if path.exists():
        store.require({p.name for p in path.iterdir()} == {"catalog.db", "payloads"}, "unsettled retention layout")
        if physical: r.extension(root, meta)
        with readonly_db(path / "catalog.db") as db:
            policy = r.get_record(db, "settings", "policy")
            store.require(r.get_record(db, "control", "operation") is None, "retention operation pending")
            rows = db.execute("SELECT name,value FROM keys ORDER BY name").fetchmany(r.MAX_KEYS + 1)
        store.require(policy["format"] == r.FORMAT and policy["store_id"] == meta["store_id"] and
            policy["automatic_prune"] is False and policy["limits"] == r.limits(**policy["limits"]) and
            len(rows) + len(tasks) <= policy["limits"]["max_keys"], "snapshot retained policy/capacity")
        retained = set(); charge = r.CATALOG_RESERVE
        for name, raw in rows:
            store.require(re.fullmatch(r"[0-9a-f]{64}", name) and name not in tasks and len(raw.encode()) <= r.KEY_BYTES,
                          "snapshot key identity/size")
            value = json.loads(raw); state = r.key_state(value, meta, name); base = value["base"]
            store.require(store.digest(base["runtime_sources"]) in known, "unregistered archived runtime")
            keys[name] = dict(revision=state["revision"], payload=state["payload"], key_sha256=store.digest(value))
            charge += len(raw.encode())
            folder = path / "payloads" / name
            if state["payload"] == "purged":
                store.require(not os.path.lexists(folder), "pruned payload in backup"); continue
            retained.add(name)
            actual = r.files(folder)
            expected = state["files"]
            store.require(actual.keys() == expected.keys() and all(actual[file][key] == row[key]
                for file, row in expected.items() for key in (("bytes", "sha256", "identity") if physical else ("bytes", "sha256"))),
                "backup payload differs")
            if physical: store.require(r.identity(folder.stat()) == state["payload_identity"], "backup payload placement")
            charge += base["payload_bytes"]
            with readonly_db(folder / "ledger.db") as db:
                ledger = store.Ledger(folder, db, meta["store_id"])
                recovered = engine.public(ledger, engine.replay(ledger)); answer = recovered.pop("answer", None)
                store.require(recovered == base["terminal"] and store.digest(answer) == base["answer_sha256"] and
                    store.digest(ledger.meta) == base["meta_sha256"], "archived task reconstruction")
        store.require({folder.name for folder in (path / "payloads").iterdir()} == retained and
            charge <= policy["limits"]["max_bytes"], "snapshot retained payload/capacity")
    return dict(tasks=tasks, keys=keys, retention_limits=policy["limits"] if policy else None)


@contextmanager
def quiescent(root, *, allow_handoff=False):
    with store.root_lock(root, allow_handoff=allow_handoff) as (root, meta), ExitStack() as stack:
        for folder in sorted((root / "tasks").iterdir()):
            store.require(re.fullmatch(r"[0-9a-f]{64}", folder.name), "interrupted task creation")
            stack.enter_context(store.lock(folder / ".lock"))
            db = store.connection(folder); db.close()  # Recover a dead SQLite writer before taking the snapshot.
        yield root, meta


def capture(root, meta):
    summary = audit(root, meta, physical=True)
    return dict(source=str(root), store=meta, summary=summary,
                **regular_tree(root, exclude=("handoff.json", ".handoff.next")))


def safe_snapshot_path(root, output):
    store.require(not output.is_relative_to(root) and not root.is_relative_to(output), "snapshot/store path overlap")
    store.private(output.parent, True)


def copy_file(source, target, expected, checkpoint):
    """Continue only an exact prefix of a file in this owned private draft."""
    temporary = target.with_name(target.name + ".copy-part")
    if target.exists():
        store.require(not os.path.lexists(temporary) and store.private(target).st_size == expected["bytes"] and
            r.sha(target) == expected["sha256"], "published draft file differs")
        return
    store.require(not os.path.lexists(target), "draft target link")
    if not os.path.lexists(temporary): store.write_new(temporary, b"")
    size = store.private(temporary).st_size
    store.require(size <= expected["bytes"], "draft file prefix size")
    with source.open("rb") as original, temporary.open("rb") as partial:
        while chunk := partial.read(1024**2):
            store.require(original.read(len(chunk)) == chunk, "draft prefix differs")
        with temporary.open("ab") as output:
            while chunk := original.read(1024**2):
                store.require(output.tell() + len(chunk) <= expected["bytes"], "source grew during copy")
                output.write(chunk); output.flush(); os.fsync(output.fileno())
                checkpoint("copy_chunk", dict(path=str(target)))
    store.require(store.private(temporary).st_size == expected["bytes"] and r.sha(temporary) == expected["sha256"], "copied file identity")
    os.rename(temporary, target); store.sync(target.parent)
    checkpoint("file_copied", dict(path=str(target)))


def copy_tree(source, target, manifest, checkpoint):
    if not target.exists(): target.mkdir(mode=0o700)
    store.private(target, True)
    allowed_dirs = set(manifest["directories"])
    allowed_files = set(manifest["files"])
    for path in target.rglob("*"):
        name = path.relative_to(target).as_posix()
        if path.is_dir() and not path.is_symlink():
            store.require(name in allowed_dirs, "unknown draft directory"); store.private(path, True)
        else:
            store.private(path)
            store.require(name in allowed_files or name.removesuffix(".copy-part") in allowed_files, "unknown draft file")
    for name in sorted(allowed_dirs, key=lambda value: (len(Path(value).parts), value)):
        if name == ".": continue
        path = target / name
        if not path.exists(): path.mkdir(mode=0o700)
        store.private(path, True)
    for name, info in sorted(manifest["files"].items()): copy_file(source / name, target / name, info, checkpoint)
    for name in sorted(allowed_dirs, key=lambda value: len(Path(value).parts), reverse=True): store.sync(target / name)


def snapshot(root, output, max_bytes=2 * 1024**3, *, checkpoint=None):
    checkpoint = checkpoint or (lambda *args: None)
    output = store.local(output); root = store.local(root); safe_snapshot_path(root, output)
    store.require(type(max_bytes) is int and 1024**2 <= max_bytes <= 2**40, "snapshot byte budget")
    with quiescent(root) as (root, meta):
        current = capture(root, meta)
        if output.exists():
            store.private(output, True)
            store.require({p.name for p in output.iterdir()} <= {"manifest.json", "data", "seal.json"}, "unknown snapshot draft")
            value = r.read(output / "manifest.json", MAX_MANIFEST)
            store.require(value["capture"] == current and value["max_bytes"] == max_bytes, "snapshot source changed; preserve draft and use a new snapshot")
        else:
            value = dict(format=SNAPSHOT, snapshot_id=uuid.uuid4().hex, created_ns=time.time_ns(),
                         max_bytes=max_bytes, capture=current)
            total = sum(row["bytes"] for row in current["files"].values()) + len(store.encode(value)) + 4096
            store.require(total <= max_bytes and len(store.encode(value)) <= MAX_MANIFEST, "snapshot byte/manifest capacity")
            store.require(shutil.disk_usage(output.parent).free >= total, "snapshot disk headroom")
            output.mkdir(mode=0o700); r.save(output / "manifest.json", value, MAX_MANIFEST)
        checksum = store.digest(value)
        if (output / "seal.json").exists(): verify(output, checksum); return dict(snapshot=str(output), snapshot_sha256=checksum, already_created=True, no_execution=True)
        copy_tree(root, output / "data", current, checkpoint)
        store.require(capture(root, meta) == current, "source changed while snapshot was locked")
        store.require(audit(output / "data", meta, physical=False) == current["summary"], "snapshot state differs")
        checkpoint("before_seal", dict(snapshot_sha256=checksum))
        store.write_new(output / "seal.json", store.encode(dict(format=SNAPSHOT, snapshot_sha256=checksum)))
        store.sync(output); checkpoint("snapshot_sealed", dict(snapshot_sha256=checksum))
        verify(output, checksum)
        return dict(snapshot=str(output), snapshot_sha256=checksum, already_created=False, no_execution=True)


def verify(path, expected):
    from tools.project_tasks import migration
    path = store.local(path); store.private(path, True)
    store.require({p.name for p in path.iterdir()} == {"manifest.json", "data", "seal.json"}, "incomplete snapshot")
    value = r.read(path / "manifest.json", MAX_MANIFEST)
    store.require(migration.checksum(expected) and store.digest(value) == expected and value["format"] == SNAPSHOT and
        r.read(path / "seal.json") == dict(format=SNAPSHOT, snapshot_sha256=expected), "snapshot seal/digest")
    captured = value["capture"]; actual = regular_tree(path / "data")
    store.require(actual["directories"].keys() == captured["directories"].keys() and actual["files"].keys() == captured["files"].keys() and
        all(row[key] == captured["files"][name][key] for name, row in actual["files"].items() for key in ("bytes", "sha256")), "snapshot content differs")
    store.require(audit(path / "data", captured["store"], physical=False) == captured["summary"], "snapshot semantic state")
    total = sum(row["bytes"] for row in actual["files"].values()) + len(store.encode(value)) + 4096
    store.require(total <= value["max_bytes"] <= 2**40, "snapshot byte capacity")
    return value


def restore_plan(root, snapshot_path, snapshot_sha256, destination, output, reason):
    root, destination = store.local(root), store.local(destination)
    snapshot_path, output = store.local(snapshot_path), store.local(output)
    snapshot_value = verify(snapshot_path, snapshot_sha256)
    store.private(destination.parent, True); store.private(output.parent, True)
    store.require(isinstance(reason, str) and 0 < len(reason) <= 512, "explicit restore reason")
    for protected in (root, snapshot_path):
        store.require(not destination.is_relative_to(protected) and not protected.is_relative_to(destination) and
            not output.is_relative_to(protected), "restore path overlap")
    store.require(not os.path.lexists(destination) and not output.is_relative_to(destination), "new restore destination required")
    with quiescent(root) as (root, meta):
        current = capture(root, meta)
        store.require(current == snapshot_value["capture"], "snapshot is stale; source changed")
        previous = r.read(root / "restored.json") if (root / "restored.json").exists() else None
        store.require(previous is None or len(previous["history"]) < MAX_HANDOFFS, "store handoff limit")
        plan = dict(format=RESTORE, source=str(root), source_identity=r.identity(root.stat()),
            store_id=meta["store_id"], snapshot=str(snapshot_path), snapshot_sha256=snapshot_sha256,
            destination=str(destination), parent_identity=r.identity(destination.parent.stat()),
            source_capture_sha256=store.digest(current), previous_placement=previous, reason=reason)
        r.save(output, plan)
    return dict(prepared=True, plan=str(output), plan_sha256=store.digest(plan), no_execution=True)


def replace_owned(path, before, after):
    """Idempotent replacement of one exact owned metadata record."""
    if path.exists():
        current = r.read(path)
        if current == after: return
        store.require(current == before, "restore metadata changed")
    else: store.require(before is None, "missing restore metadata")
    temporary = path.with_name(path.name + ".restore-next")
    finish_file(temporary, after)
    os.replace(temporary, path); store.sync(path.parent)


def placement_value(plan, checksum, identity):
    history = deepcopy(plan["previous_placement"]["history"]) if plan["previous_placement"] else []
    previous = history[-1]["digest"] if history else "0" * 64
    payload = dict(source=plan["source"], snapshot_sha256=plan["snapshot_sha256"],
        destination=plan["destination"], identity=identity, plan_sha256=checksum, reason=plan["reason"])
    number = len(history) + 1
    history.append(dict(seq=number, previous=previous, payload=payload, digest=store.digest([number, payload, previous])))
    return dict(format=PLACEMENT, store_id=plan["store_id"], root=plan["destination"], identity=identity, history=history)


def rebind(candidate, snapshot_value, plan, checksum, checkpoint):
    source = Path(plan["snapshot"]) / "data"
    # Before root identity is changed the copied store fails its inode check;
    # afterwards this extra entry keeps every ordinary reader closed until the
    # destination-bound placement record has been durably installed.
    blocker = candidate / ".restore-block.json"
    finish_file(blocker, dict(plan_sha256=checksum, destination=plan["destination"]))
    old_meta = snapshot_value["capture"]["store"]
    new_meta = dict(old_meta, root_device=candidate.stat().st_dev, root_inode=candidate.stat().st_ino)
    replace_owned(candidate / "store.json", old_meta, new_meta)
    path = candidate / "retention"
    if path.exists():
        with readonly_db(source / "retention/catalog.db") as original:
            old_policy = r.get_record(original, "settings", "policy")
            rows = original.execute("SELECT name,value FROM keys ORDER BY name").fetchall()
        policy = dict(old_policy, root_identity=r.identity(candidate.stat()), directory_identity=r.identity(path.stat()),
            catalog_identity=r.identity((path / "catalog.db").stat()), payloads_identity=r.identity((path / "payloads").stat()))
        with r.catalog(path) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                store.require(r.get_record(db, "settings", "policy") in (old_policy, policy), "restored policy changed")
                r.put_record(db, "settings", "policy", policy)
                for name, raw in rows:
                    before = json.loads(raw); state = r.key_state(before, old_meta, name); after = before
                    if state["payload"] == "retained":
                        folder = path / "payloads" / name
                        actual = r.files(folder)
                        store.require(actual.keys() == state["files"].keys() and all(row[key] == state["files"][file][key]
                            for file, row in actual.items() for key in ("bytes", "sha256")), "restored payload bytes differ")
                        payload = dict(plan_sha256=checksum, reason=plan["reason"], before=store.digest(r.placement(state)),
                            directory_identity=r.identity(folder.stat()), file_identities={file: row["identity"] for file, row in actual.items()})
                        after = r.add_event(before, old_meta, name, "placement", payload)
                    store.require(r.get_record(db, "keys", name) in (before, after), "restored key changed")
                    r.put_record(db, "keys", name, after)
                checkpoint("placement_before_commit", dict(plan_sha256=checksum))
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction: db.execute("ROLLBACK")
                raise
        store.sync(path)
    checkpoint("placement_committed", dict(plan_sha256=checksum))
    placed = placement_value(plan, checksum, r.identity(candidate.stat()))
    replace_owned(candidate / "restored.json", plan["previous_placement"], placed)
    store.private(blocker); blocker.unlink(); store.sync(candidate)
    summary = audit(candidate, new_meta, physical=True, logical_root=Path(plan["destination"]))
    store.require(summary["tasks"] == snapshot_value["capture"]["summary"]["tasks"] and
        summary["keys"].keys() == snapshot_value["capture"]["summary"]["keys"].keys() and
        all(row["payload"] == snapshot_value["capture"]["summary"]["keys"][name]["payload"]
            for name, row in summary["keys"].items()), "restored tasks or permanent keys changed")
    return new_meta, summary


def finish_file(path, value):
    """An interrupted fence stays closed; continue only its exact byte prefix."""
    raw = store.encode(value)
    if path.exists():
        store.private(path)
        partial = path.read_bytes(); store.require(raw.startswith(partial), "handoff metadata differs")
        with path.open("ab") as output:
            output.write(raw[len(partial):]); output.flush(); os.fsync(output.fileno())
    else: store.write_new(path, raw)
    store.sync(path.parent)


def restore_apply(plan_path, expected, *, checkpoint=None):
    from tools.project_tasks import migration
    checkpoint = checkpoint or (lambda *args: None)
    plan_path = store.local(plan_path); plan = r.read(plan_path)
    store.require(migration.checksum(expected) and store.digest(plan) == expected and plan["format"] == RESTORE and
        isinstance(plan["reason"], str) and 0 < len(plan["reason"]) <= 512, "restore plan digest/reason")
    root, destination = store.local(plan["source"]), store.local(plan["destination"])
    snapshot_path = store.local(plan["snapshot"])
    snapshot_value = verify(snapshot_path, plan["snapshot_sha256"])
    store.require(plan["store_id"] == snapshot_value["capture"]["store"]["store_id"] and
        snapshot_value["capture"]["source"] == str(root) and
        store.digest(snapshot_value["capture"]) == plan["source_capture_sha256"] and
        r.identity(store.private(destination.parent, True)) == plan["parent_identity"], "restore source/destination binding")
    for protected in (root, snapshot_path):
        store.require(not destination.is_relative_to(protected) and not protected.is_relative_to(destination), "restore overlap")
    work = destination.parent / (".q4t-restore-" + expected)
    candidate = work / "candidate"
    with quiescent(root, allow_handoff=True) as (root, meta):
        store.require(r.identity(root.stat()) == plan["source_identity"] and meta["store_id"] == plan["store_id"], "restore source identity")
        current = capture(root, meta)
        store.require(current == snapshot_value["capture"], "snapshot is stale; preserve the original source")
        if not work.exists():
            store.require(not (root / "handoff.json").exists() and not os.path.lexists(destination), "handoff work missing or destination exists")
            need = sum(row["bytes"] for row in current["files"].values()) + r.CATALOG_RESERVE
            store.require(shutil.disk_usage(destination.parent).free >= need, "restore disk headroom")
            work.mkdir(mode=0o700); store.write_new(work / ".lock", b"")
            r.save(work / "job.json", dict(plan=plan, plan_sha256=expected, identity=r.identity(work.stat())))
        store.private(work, True)
        store.require({p.name for p in work.iterdir()} <= {".lock", "job.json", "candidate", "copied.json", "ready.json", "receipt.json"}, "unknown restore work")
        job = r.read(work / "job.json")
        store.require(job == dict(plan=plan, plan_sha256=expected, identity=r.identity(work.stat())), "restore work binding")
        with store.lock(work / ".lock"):
            if destination.exists():
                store.require(not candidate.exists() and (work / "ready.json").exists(), "ambiguous restore destination")
                ready = r.read(work / "ready.json", MAX_MANIFEST)
                fence = dict(format=RESTORE, plan_sha256=expected, destination=str(destination),
                    identity=ready["identity"], ready_sha256=store.digest(ready), snapshot_sha256=plan["snapshot_sha256"])
                store.require(r.read(root / "handoff.json") == fence and r.identity(store.private(destination, True)) == ready["identity"], "destination does not match source fence")
                _, target_meta = store.checked_root(destination)
                placed = check_placement(destination, target_meta)
                store.require(placed["history"][-1]["payload"]["plan_sha256"] == expected, "published destination changed")
                # Its applications may already have written new events. Never
                # compare against or overwrite the original restored snapshot.
                receipt = dict(restored=True, plan_sha256=expected, destination=str(destination),
                    store_id=meta["store_id"], source_retired=True, no_execution=True)
                finish_file(work / "receipt.json", receipt)
                return dict(receipt, already_applied=True)
            store.require(not os.path.lexists(destination), "restore destination link")
            if not (work / "copied.json").exists():
                store.require(not (root / "handoff.json").exists(), "source fenced before completed candidate")
                copy_tree(snapshot_path / "data", candidate, current, checkpoint)
                store.require(audit(candidate, meta, physical=False) == current["summary"], "candidate copy differs")
                store.write_new(work / "copied.json", store.encode(dict(snapshot_sha256=plan["snapshot_sha256"], identity=r.identity(candidate.stat()))))
            store.require(r.read(work / "copied.json") == dict(snapshot_sha256=plan["snapshot_sha256"], identity=r.identity(candidate.stat())), "copied candidate identity")
            if not (work / "ready.json").exists():
                new_meta, summary = rebind(candidate, snapshot_value, plan, expected, checkpoint)
                ready = dict(identity=r.identity(candidate.stat()), store=new_meta, summary=summary, tree=regular_tree(candidate))
                r.save(work / "ready.json", ready, MAX_MANIFEST)
            else: ready = r.read(work / "ready.json", MAX_MANIFEST)
            store.require(regular_tree(candidate) == ready["tree"] and
                audit(candidate, ready["store"], physical=True, logical_root=destination) == ready["summary"], "ready candidate changed")
            checkpoint("candidate_ready", dict(plan_sha256=expected))
            store.require(capture(root, meta) == snapshot_value["capture"], "source changed before handoff")
            fence = dict(format=RESTORE, plan_sha256=expected, destination=str(destination), identity=ready["identity"],
                ready_sha256=store.digest(ready), snapshot_sha256=plan["snapshot_sha256"])
            if (root / "handoff.json").exists():
                store.require(r.read(root / "handoff.json") == fence, "different source handoff")
            else:
                finish_file(root / ".handoff.next", fence)
                checkpoint("fence_before_publish", dict(plan_sha256=expected))
                os.rename(root / ".handoff.next", root / "handoff.json"); store.sync(root)
            checkpoint("source_fenced", dict(plan_sha256=expected))
            os.rename(candidate, destination); store.sync(work); store.sync(destination.parent)
            checkpoint("destination_published", dict(plan_sha256=expected))
            receipt = dict(restored=True, plan_sha256=expected, destination=str(destination),
                store_id=meta["store_id"], source_retired=True, no_execution=True)
            finish_file(work / "receipt.json", receipt)
            return dict(receipt, already_applied=False)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "verify", "plan-restore", "apply-restore"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--snapshot-sha256")
    parser.add_argument("--max-bytes", type=int)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--reason")
    args = parser.parse_args()
    if args.action == "backup":
        store.require(args.root is not None and args.snapshot is not None and all(v is None for v in
            (args.snapshot_sha256, args.destination, args.plan, args.plan_sha256, args.reason)), "backup arguments")
        result = snapshot(args.root, args.snapshot, 2 * 1024**3 if args.max_bytes is None else args.max_bytes)
    elif args.action == "verify":
        store.require(args.snapshot is not None and args.snapshot_sha256 is not None and all(v is None for v in
            (args.root, args.max_bytes, args.destination, args.plan, args.plan_sha256, args.reason)), "verify arguments")
        value = verify(args.snapshot, args.snapshot_sha256)
        result = dict(verified=True, snapshot_sha256=args.snapshot_sha256, store_id=value["capture"]["store"]["store_id"], no_execution=True)
    elif args.action == "plan-restore":
        store.require(all(v is not None for v in (args.root, args.snapshot, args.snapshot_sha256, args.destination, args.plan, args.reason)) and
            args.max_bytes is None and args.plan_sha256 is None, "restore plan arguments")
        result = restore_plan(args.root, args.snapshot, args.snapshot_sha256, args.destination, args.plan, args.reason)
    else:
        store.require(args.plan is not None and args.plan_sha256 is not None and all(v is None for v in
            (args.root, args.snapshot, args.snapshot_sha256, args.max_bytes, args.destination, args.reason)), "restore apply arguments")
        result = restore_apply(args.plan, args.plan_sha256)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try: main()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(json.dumps(dict(error=type(error).__name__, automatic_retry=False)), file=sys.stderr)
        raise SystemExit(1)
