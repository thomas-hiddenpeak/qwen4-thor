"""Terminal payload retention with permanent keys and explicit file transactions."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time

from tools.project_tasks import store

FORMAT = "q4t.project.retention.v1"
PLAN_FORMAT = "q4t.project.retention.plan.v1"
KEY_FORMAT = "q4t.project.archived.key.v1"
OP_FORMAT = "q4t.project.retention.operation.v1"
META_BYTES = 256 * 1024
KEY_BYTES = 64 * 1024
MAX_KEYS = 16384
CATALOG_BYTES = 32 * 1024**2
CATALOG_RESERVE = 2 * CATALOG_BYTES + 1024**2
TERMINAL = ("COMPLETED", "FAILED", "ABANDONED")


def identity(info):
    return [info.st_dev, info.st_ino]


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path, maximum=META_BYTES):
    info = store.private(path)
    store.require(info.st_size <= maximum, "retention metadata size")
    def pairs(items):
        value = {}
        for key, item in items:
            store.require(key not in value, "duplicate retention metadata")
            value[key] = item
        return value
    raw = Path(path).read_bytes()
    store.require(len(raw) <= maximum, "retention metadata size")
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: store.require(False, "nonfinite retention metadata"))


def save(path, value, maximum=META_BYTES):
    raw = store.encode(value)
    store.require(len(raw) <= maximum, "retention metadata size")
    store.write_new(path, raw)


def limits(max_keys, max_bytes, min_retain_seconds):
    store.require(type(max_keys) is int and 1 <= max_keys <= MAX_KEYS and
        type(max_bytes) is int and CATALOG_RESERVE + 1024**2 <= max_bytes <= 2**40 and
        type(min_retain_seconds) is int and 0 <= min_retain_seconds <= 10 * 365 * 86400,
        "invalid retention capacity/period", code='invalid_arguments')
    return dict(max_keys=max_keys, max_bytes=max_bytes, min_retain_seconds=min_retain_seconds)


@contextmanager
def catalog(path):
    store.require(store.private(path / "catalog.db").st_size <= CATALOG_BYTES, "catalog byte size")
    journal = path / "catalog.db-journal"
    if os.path.lexists(journal):
        store.require(store.private(journal).st_size <= CATALOG_BYTES + 1024**2, "catalog journal size")
    db = sqlite3.connect(path / "catalog.db", timeout=2, isolation_level=None)
    try:
        store.require(db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete", "catalog rollback journal")
        db.execute("PRAGMA synchronous=FULL")
        store.require(db.execute("PRAGMA max_page_count=8192").fetchone()[0] == 8192, "catalog page limit")
        store.require(db.execute("PRAGMA page_size").fetchone()[0] == 4096 and
            db.execute("PRAGMA quick_check").fetchall() == [("ok",)], "catalog integrity", code='integrity_error')
        yield db
    finally: db.close()


def get_record(db, table, name):
    store.require(table in ("settings", "keys", "control"), "catalog table")
    row = db.execute("SELECT value FROM " + table + " WHERE name=?", (name,)).fetchone()
    if row is None: return None
    store.require(len(row[0].encode()) <= (KEY_BYTES if table == "keys" else META_BYTES), "catalog record size")
    return json.loads(row[0])


def put_record(db, table, name, value):
    store.require(table in ("settings", "keys", "control"), "catalog table")
    raw = store.encode(value)
    store.require(len(raw) <= (KEY_BYTES if table == "keys" else META_BYTES), "catalog record size")
    db.execute("INSERT INTO " + table + " VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, raw.decode()))


def control(path, name):
    with catalog(path) as db: return get_record(db, "control", name)


def extension(root, meta):
    path = root / "retention"
    store.private(path, True)
    with catalog(path) as db: policy = get_record(db, "settings", "policy")
    store.require(policy["format"] == FORMAT and policy["store_id"] == meta["store_id"] and
        policy["root_identity"] == [meta["root_device"], meta["root_inode"]] and
        policy["directory_identity"] == identity(path.stat()) and policy["automatic_prune"] is False and
        policy["limits"] == limits(**policy["limits"]), "retention policy/store identity")
    for name in ("payloads",):
        store.require(identity(store.private(path / name, True)) == policy[name + "_identity"], "retention directory changed")
    store.require(identity(store.private(path / "catalog.db")) == policy["catalog_identity"], "catalog replaced")
    store.require({child.name for child in path.iterdir()} <=
        {"catalog.db", "catalog.db-journal", "payloads"},
        "unknown retention entry")
    return path, policy


def ready(path):
    store.require(control(path, "operation") is None, "retention operation incomplete; explicit finish required", code='retention_pending')


def enable(root, *, max_keys=4096, max_bytes=1024**3, min_retain_seconds=86400):
    selected = limits(max_keys, max_bytes, min_retain_seconds)
    root, meta = store.checked_root(root)
    with store.root_lock(root):
        if (root / "retention").exists():
            path, policy = extension(root, meta); ready(path)
            store.require(policy["limits"] == selected, "existing retention policy cannot be replaced", code='policy_conflict')
            return dict(enabled=True, created=False, limits=selected, no_execution=True)
        store.require(len(list((root / "tasks").iterdir())) <= max_keys, "existing tasks exceed key capacity", code='key_capacity')
        # Build beside the store. Before publication there are no archived keys,
        # so an interrupted setup cannot hide an executed task from old readers.
        draft = root.parent / (".q4t-retention-" + meta["store_id"])
        if not draft.exists(): draft.mkdir(mode=0o700)
        store.private(draft, True)
        store.require({child.name for child in draft.iterdir()} <= {"catalog.db", "catalog.db-journal", "payloads"}, "unknown retention draft")
        for name in ("payloads",):
            child = draft / name
            if not child.exists(): child.mkdir(mode=0o700)
            store.private(child, True)
            store.require(not list(child.iterdir()), "nonempty unpublished retention draft")
        if not (draft / "catalog.db").exists(): store.write_new(draft / "catalog.db", b"")
        policy = dict(format=FORMAT, store_id=meta["store_id"], root_identity=[meta["root_device"], meta["root_inode"]],
            directory_identity=identity(draft.stat()), catalog_identity=identity((draft / "catalog.db").stat()),
            payloads_identity=identity((draft / "payloads").stat()), limits=selected, automatic_prune=False)
        with catalog(draft) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            store.require(tables in (set(), {"settings", "keys", "control"}), "unknown catalog draft schema")
            if not tables:
                db.execute("BEGIN IMMEDIATE")
                try:
                    for table in ("settings", "keys", "control"):
                        db.execute("CREATE TABLE " + table + " (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    put_record(db, "settings", "policy", policy)
                    db.execute("COMMIT")
                except BaseException:
                    if db.in_transaction: db.execute("ROLLBACK")
                    raise
            store.require(get_record(db, "settings", "policy") == policy and
                db.execute("SELECT count(*) FROM keys").fetchone()[0] ==
                db.execute("SELECT count(*) FROM control").fetchone()[0] == 0, "catalog draft changed")
        store.sync(draft / "payloads"); store.sync(draft)
        os.rename(draft, root / "retention"); store.sync(root); store.sync(root.parent)
    return dict(enabled=True, created=True, limits=selected, no_execution=True)


def key_value(path, name):
    store.require(re.fullmatch(r"[0-9a-f]{64}", name), "archive key identity")
    with catalog(path) as db: return get_record(db, "keys", name)


def present(root, meta, name):
    if not (root / "retention").exists(): return False
    path, _ = extension(root, meta); ready(path)
    return key_value(path, name) is not None


def admit(root, meta):
    if not (root / "retention").exists(): return
    path, policy = extension(root, meta); ready(path)
    with catalog(path) as db: count = db.execute("SELECT count(*) FROM keys").fetchone()[0]
    store.require(count + len(list((root / "tasks").iterdir())) < policy["limits"]["max_keys"],
                  "permanent idempotency key capacity exhausted", code='key_capacity')


def files(folder, *, expected=None, partial=False, hashes=True):
    store.private(folder, True)
    found, total = {}, 0
    for path in sorted(folder.iterdir()):
        store.require(path.name in ("ledger.db", ".lock") or re.fullmatch(r"(?:wire|check)-[0-9]{3}\.bin", path.name),
                      "unknown archive payload entry")
        info = store.private(path)
        total += info.st_size
        store.require(total <= store.RESERVED_BYTES and len(found) < 42, "task archive byte/file budget")
        found[path.name] = dict(identity=identity(info), bytes=info.st_size)
        if hashes: found[path.name]["sha256"] = sha(path)
    if expected is not None:
        store.require((partial or found.keys() == expected.keys()) and
            all(name in expected and all(expected[name].get(key) == item for key, item in value.items())
                for name, value in found.items()), "archive payload changed")
    return found


def key_state(value, meta, name):
    from tools.project_tasks import migration
    store.require(set(value) == {"base", "events"} and len(value["events"]) <= 17, "archive key shape")
    base = value["base"]
    store.require(base["format"] == KEY_FORMAT and base["store_id"] == meta["store_id"] and base["task_id"] == name and
        base["terminal"]["state"] in TERMINAL and base["payload_bytes"] == sum(row["bytes"] for row in base["files"].values()),
        "archive key identity/terminal")
    state = dict(binding=deepcopy(base["binding"]), payload="retained", rekeys=0, revision=store.digest(base),
                 plan_ids=[base["archive_plan_sha256"]], placements=0,
                 files=deepcopy(base["files"]), payload_identity=base["payload_identity"])
    for seq, event in enumerate(value["events"], 1):
        kind, payload = event["kind"], event["payload"]
        store.require(event["seq"] == seq and event["previous"] == state["revision"] and
            event["digest"] == store.digest([seq, kind, payload, event["previous"]]) and
            migration.checksum(payload["plan_sha256"]) and isinstance(payload["reason"], str) and
            0 < len(payload["reason"]) <= 512, "archive key event chain")
        if kind == "prune":
            store.require(state["payload"] == "retained", "payload already pruned")
            state["payload"] = "purged"
        elif kind == "placement":
            store.require(state["payload"] == "retained" and state["placements"] < 8 and
                payload["before"] == store.digest(placement(state)) and
                set(payload["file_identities"]) == set(state["files"]), "archive placement transition")
            for item in (payload["directory_identity"], *payload["file_identities"].values()):
                store.require(isinstance(item, list) and len(item) == 2 and
                    all(type(number) is int and number >= 0 for number in item), "archive placement identity")
            for filename, item in payload["file_identities"].items():
                state["files"][filename]["identity"] = item
            state.update(payload_identity=payload["directory_identity"], placements=state["placements"] + 1)
        else:
            store.require(kind == "rekey" and state["rekeys"] < 8 and payload["before"] == state["binding"], "archive key rotation")
            migration.same_identity(state["binding"], payload["after"])
            store.require(payload["after"] != state["binding"], "empty archive key rotation")
            state.update(binding=deepcopy(payload["after"]), rekeys=state["rekeys"] + 1)
        state["revision"] = event["digest"]; state["plan_ids"].append(payload["plan_sha256"])
    return state


def placement(state):
    return {key: state[key] for key in ("files", "payload_identity")}


def load_key(path, meta, name):
    value = key_value(path, name)
    store.require(value is not None, "archived task key not found", code='not_found')
    return value, key_state(value, meta, name)


def business_contract(spec):
    value = deepcopy(spec)
    value.pop("runtime_sources"); value.pop("budget_profile", None)
    value["binding"].pop("key_sha256")
    return store.digest(value)


def public(value, state):
    base = value["base"]
    result = {key: item for key, item in base["terminal"].items() if key not in ("state", "revision", "allowed_actions")}
    result.update(state="ARCHIVED", terminal_state=base["terminal"]["state"],
        terminal_revision=base["terminal"]["revision"], archive_revision=state["revision"],
        payload_state=state["payload"], allowed_actions=["inspect-archive"] if state["payload"] == "retained" else [],
        automatic_retry=False, no_execution=True)
    return result


def lookup(root, binding, key, *, spec=None, include_answer=False):
    from tools.project_tasks import engine
    root, meta = store.checked_root(root)
    name = store.task_name(binding["client_id"], key)
    with store.root_lock(root, exclusive=False):
        path, _ = extension(root, meta); ready(path)
        value, state = load_key(path, meta, name)
        store.require(state["binding"] == binding, "archived task/client binding changed", code='binding_changed')
        if spec is not None:
            store.require(business_contract(spec) == value["base"]["contract_sha256"], "idempotency key conflicts with archived task", code='key_conflict')
        result = public(value, state)
        if include_answer and state["payload"] == "retained":
            folder = path / "payloads" / name
            store.require(identity(store.private(folder, True)) == state["payload_identity"], "archive directory changed")
            files(folder, expected=state["files"])
            with store.lock(folder / ".lock", exclusive=False):
                db = store.connection(folder)
                try:
                    ledger = store.Ledger(folder, db, meta["store_id"])
                    recovered = engine.public(ledger, engine.replay(ledger))
                finally: db.close()
            store.require(recovered["revision"] == value["base"]["terminal"]["revision"] and
                store.digest(recovered.get("answer")) == value["base"]["answer_sha256"], "archived answer changed")
            result["answer"] = recovered.get("answer")
        return result


def inventory(root, meta, path, policy):
    ready(path)
    keys, count, total = [], 0, CATALOG_RESERVE
    with catalog(path) as db:
        rows = db.execute("SELECT name,value FROM keys ORDER BY name").fetchmany(policy["limits"]["max_keys"] + 1)
    store.require(len(rows) <= policy["limits"]["max_keys"], "key capacity exceeded")
    for name, raw in rows:
        store.require(re.fullmatch(r"[0-9a-f]{64}", name) and len(raw.encode()) <= KEY_BYTES, "archive key identity/size")
        value = json.loads(raw); state = key_state(value, meta, name)
        total += len(raw.encode())
        folder = path / "payloads" / name
        if state["payload"] == "retained":
            store.require(identity(store.private(folder, True)) == state["payload_identity"], "payload identity")
            actual = files(folder, expected=state["files"], hashes=False)
            total += sum(row["bytes"] for row in actual.values()); count += 1
        else: store.require(not os.path.lexists(folder), "purged payload reappeared")
        keys.append(name)
    store.require({file.name for file in (path / "payloads").iterdir()} <= set(keys), "orphan archive payload")
    active = len(list((root / "tasks").iterdir()))
    store.require(total <= policy["limits"]["max_bytes"] and active + len(keys) <= policy["limits"]["max_keys"],
                  "retention capacity exceeded")
    return dict(active_tasks=active, archived_keys=len(keys), retained_payloads=count, archive_bytes=total,
        max_active_tasks=meta["max_tasks"], **policy["limits"], keys_never_expire=True,
        catalog_reserved_bytes=CATALOG_RESERVE, catalog_file_bytes=(path / "catalog.db").stat().st_size)


def status(root):
    root, meta = store.checked_root(root)
    with store.root_lock(root, exclusive=False):
        path, policy = extension(root, meta)
        operation = control(path, "operation")
        if operation is not None:
            return dict(state="RETENTION_PENDING", kind=operation["kind"], task_id=operation["name"],
                operation_sha256=store.digest(operation), no_execution=True)
        return dict(state="READY", **inventory(root, meta, path, policy), no_execution=True)


def skeleton(root, meta, path, name, kind, binding, reason):
    store.require(isinstance(reason, str) and 0 < len(reason) <= 512, "explicit retention reason required", code='invalid_arguments')
    return dict(format=PLAN_FORMAT, kind=kind, root=str(root), store_id=meta["store_id"],
        root_identity=[meta["root_device"], meta["root_inode"]], name=name,
        policy_sha256=store.digest(extension(root, meta)[1]), binding=deepcopy(binding), reason=reason)


def build(root, meta, path, policy, name, kind, binding, reason):
    from tools.project_tasks import engine, migration
    ready(path)
    plan = skeleton(root, meta, path, name, kind, binding, reason)
    inventory(root, meta, path, policy)
    if kind == "archive":
        store.require(key_value(path, name) is None, "task already archived", code='already_exists')
        folder = root / "tasks" / name
        store.private(folder, True)
        # Caller holds the task lock, so replay and a later rename see one task.
        db = store.connection(folder)
        try:
            ledger = store.Ledger(folder, db, meta["store_id"])
            state = engine.replay(ledger); spec = state["execution_spec"]
            store.require(state["phase"] in TERMINAL, "only terminal tasks may be archived", code='task_not_terminal')
            store.require(spec["binding"] == binding, "task/client binding changed", code='binding_changed')
            migration.supported(spec, engine.sources())
            terminal = engine.public(ledger, state)
            answer = terminal.pop("answer", None)
            plan.update(terminal=terminal, answer_sha256=store.digest(answer),
                meta_sha256=store.digest(ledger.meta), contract_sha256=business_contract(spec),
                runtime_sources=spec["runtime_sources"])
        finally: db.close()
        plan.update(payload_identity=identity(folder.stat()), files=files(folder))
    else:
        value, state = load_key(path, meta, name)
        plan["key_sha256"] = store.digest(value)
        if kind == "prune":
            store.require(state["binding"] == binding and state["payload"] == "retained", "payload/client state")
            store.require(time.time_ns() >= value["base"]["purge_after_ns"], "minimum payload retention has not elapsed", code='retention_period')
            folder = path / "payloads" / name
            store.require(identity(store.private(folder, True)) == state["payload_identity"], "payload directory changed")
            files(folder, expected=state["files"])
        else:
            store.require(kind == "rekey" and state["rekeys"] < 8, "archive key rotation limit", code='task_budget')
            migration.same_identity(state["binding"], binding)
            store.require(state["binding"] != binding, "archived key already has this credential", code='already_exists')
    return plan


def prepare(root, binding, key, kind, output, reason):
    root, meta = store.checked_root(root); name = store.task_name(binding["client_id"], key)
    output = store.local(output); store.private(output.parent, True)
    store.require(not output.is_relative_to(root), "retention plan must be outside managed store", code='unsafe_path')
    with store.root_lock(root), ExitStack() as stack:
        path, policy = extension(root, meta); ready(path)
        if kind == "archive": stack.enter_context(store.lock(root / "tasks" / name / ".lock"))
        plan = build(root, meta, path, policy, name, kind, binding, reason)
        save(output, plan)
    return dict(prepared=True, kind=kind, task_id=name, plan=str(output),
                plan_sha256=store.digest(plan), no_execution=True)


def add_event(value, meta, name, kind, payload):
    state = key_state(value, meta, name)
    seq = len(value["events"]) + 1; previous = state["revision"]
    updated = deepcopy(value)
    updated["events"].append(dict(seq=seq, kind=kind, payload=payload, previous=previous,
        digest=store.digest([seq, kind, payload, previous])))
    key_state(updated, meta, name)
    store.require(len(store.encode(updated)) <= KEY_BYTES, "archive key byte limit")
    return updated


def operation_for(root, meta, path, policy, plan):
    kind, name = plan["kind"], plan["name"]
    checksum = store.digest(plan)
    before = None
    if kind == "archive":
        now = time.time_ns()
        base = {key: deepcopy(plan[key]) for key in ("binding", "terminal", "answer_sha256", "meta_sha256",
                "contract_sha256", "runtime_sources", "payload_identity", "files")}
        base.update(format=KEY_FORMAT, store_id=meta["store_id"], task_id=name, created_ns=now,
            purge_after_ns=now + policy["limits"]["min_retain_seconds"] * 10**9,
            archive_plan_sha256=checksum, payload_bytes=sum(row["bytes"] for row in plan["files"].values()))
        after = dict(base=base, events=[])
    else:
        before, state = load_key(path, meta, name)
        payload = dict(plan_sha256=checksum, reason=plan["reason"])
        if kind == "rekey": payload.update(before=state["binding"], after=plan["binding"])
        after = add_event(before, meta, name, kind, payload)
    key_state(after, meta, name)
    size = len(store.encode(after)); store.require(size <= KEY_BYTES, "archive key size")
    usage = inventory(root, meta, path, policy)["archive_bytes"]
    delta = size - (len(store.encode(before)) if before is not None else 0)
    if kind == "archive": delta += after["base"]["payload_bytes"]
    if kind == "prune": delta -= before["base"]["payload_bytes"]
    store.require(usage + delta <= policy["limits"]["max_bytes"], "archive byte capacity exhausted", code='archive_capacity')
    store.require(shutil.disk_usage(root).free >= CATALOG_RESERVE, "insufficient retention transaction headroom", code='storage_full')
    return dict(format=OP_FORMAT, store_id=meta["store_id"], name=name, kind=kind, plan=plan,
                plan_sha256=checksum, before=before, after=after)


def reserve_operation(path, operation, checkpoint=None):
    receipt = dict(operation_sha256=store.digest(operation), plan_sha256=operation["plan_sha256"],
                   kind=operation["kind"], name=operation["name"], completed=True)
    with catalog(path) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            store.require(get_record(db, "control", "operation") is None, "another retention operation pending", code='retention_pending')
            # Reserve all key metadata before moving/deleting any payload.
            # Readers refuse while the operation is present. Completion only
            # deletes that row and cannot require a larger catalog allocation.
            put_record(db, "keys", operation["name"], operation["after"])
            put_record(db, "control", "operation", operation)
            put_record(db, "control", "last", receipt)
            if checkpoint: checkpoint("metadata_before_commit", operation)
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction: db.execute("ROLLBACK")
            raise
    store.sync(path)


def finish_locked(root, meta, path, operation, checkpoint=None):
    checkpoint = checkpoint or (lambda *args: None)
    name, kind, plan = operation["name"], operation["kind"], operation["plan"]
    store.require(operation["format"] == OP_FORMAT and operation["store_id"] == meta["store_id"] and
        name == plan["name"] and kind == plan["kind"] and operation["plan_sha256"] == store.digest(plan) and
        plan["store_id"] == meta["store_id"] and plan["root"] == str(root) and
        plan["root_identity"] == [meta["root_device"], meta["root_inode"]] and
        plan["policy_sha256"] == store.digest(extension(root, meta)[1]), "retention operation identity")
    key_state(operation["after"], meta, name)
    payload = path / "payloads" / name
    current = key_value(path, name)
    store.require(current == operation["after"], "reserved key changed during interrupted operation")
    with ExitStack() as stack:
        if kind == "archive":
            active = root / "tasks" / name
            store.require(os.path.lexists(active) != os.path.lexists(payload), "archive source/destination ambiguity")
            folder = active if active.exists() else payload
            store.require(identity(store.private(folder, True)) == plan["payload_identity"], "archive source directory changed")
            stack.enter_context(store.lock(folder / ".lock"))
            files(folder, expected=plan["files"])
            if folder == active:
                os.rename(active, payload); store.sync(active.parent); store.sync(payload.parent)
            checkpoint("payload_moved", operation)
        elif kind == "prune":
            prior = key_state(operation["before"], meta, name)
            if os.path.lexists(payload):
                store.require(identity(store.private(payload, True)) == prior["payload_identity"], "prune directory changed")
                remaining = files(payload, expected=prior["files"], partial=True)
                for filename in sorted(remaining, key=lambda item: (item == ".lock", item)):
                    file = payload / filename
                    store.require(identity(store.private(file)) == remaining[filename]["identity"] and
                                  sha(file) == remaining[filename]["sha256"], "prune file changed")
                    file.unlink(); store.sync(payload); checkpoint("payload_file_removed", operation)
                payload.rmdir(); store.sync(payload.parent)
            checkpoint("payload_removed", operation)
        else: store.require(kind == "rekey", "unknown retention operation")
        checkpoint("key_published", operation)
        receipt = dict(operation_sha256=store.digest(operation), plan_sha256=operation["plan_sha256"],
                       kind=kind, name=name, completed=True)
        with catalog(path) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                store.require(get_record(db, "control", "operation") == operation and
                    get_record(db, "control", "last") == receipt and get_record(db, "keys", name) == operation["after"],
                    "catalog transaction changed")
                db.execute("DELETE FROM control WHERE name='operation'")
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction: db.execute("ROLLBACK")
                raise
        store.sync(path)
        checkpoint("operation_finished", operation)
    return receipt


def apply(root, binding, key, plan_path, expected, *, checkpoint=None):
    from tools.project_tasks import migration
    plan_path = store.local(plan_path); plan = read(plan_path)
    root, meta = store.checked_root(root); name = store.task_name(binding["client_id"], key)
    store.require(migration.checksum(expected) and store.digest(plan) == expected and
        plan["format"] == PLAN_FORMAT and plan["root"] == str(root) and plan["store_id"] == meta["store_id"] and
        plan["root_identity"] == [meta["root_device"], meta["root_inode"]] and plan["name"] == name and
        plan["binding"] == binding, "retention plan/destination changed", code='plan_changed')
    with store.root_lock(root), ExitStack() as stack:
        path, policy = extension(root, meta)
        operation = control(path, "operation")
        if operation is not None:
            store.require(operation["plan_sha256"] == expected and operation["plan"] == plan, "different retention operation pending", code='retention_pending')
        else:
            ready(path)
            if key_value(path, name) is not None:
                value, state = load_key(path, meta, name)
                if expected in state["plan_ids"]:
                    store.require(state["binding"] == binding, "archived task/client binding changed", code='binding_changed')
                    return dict(public(value, state), applied=False, already_applied=True)
            if plan["kind"] == "archive": stack.enter_context(store.lock(root / "tasks" / name / ".lock"))
            current = build(root, meta, path, policy, name, plan["kind"], binding, plan["reason"])
            store.require(current == plan, "retention plan stale or task changed", code='plan_stale')
            operation = operation_for(root, meta, path, policy, plan)
            reserve_operation(path, operation, checkpoint)
            # Release the task lock before the transaction resumes it below;
            # the store lock and durable operation prevent ordinary lookup.
            stack.close()
            if checkpoint: checkpoint("operation_started", operation)
        finish_locked(root, meta, path, operation, checkpoint)
        value, state = load_key(path, meta, name)
        return dict(public(value, state), applied=True, already_applied=False)


def finish(root, expected, *, checkpoint=None):
    root, meta = store.checked_root(root)
    with store.root_lock(root):
        path, _ = extension(root, meta)
        operation = control(path, "operation")
        if operation is None:
            ready(path); previous = control(path, "last")
            store.require(previous is not None, "no retained operation receipt", code='not_found')
            store.require(previous["operation_sha256"] == expected and previous["completed"], "no matching completed operation", code='plan_changed')
            return dict(previous, already_finished=True, no_execution=True)
        store.require(store.digest(operation) == expected, "pending operation digest changed", code='plan_changed')
        return dict(finish_locked(root, meta, path, operation, checkpoint), already_finished=False, no_execution=True)
