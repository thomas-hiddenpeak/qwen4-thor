"""Explicit, revision-bound source upgrades and same-client API key rotation."""
from copy import deepcopy
import hashlib
import json
import re

from tools.project_tasks import budget, store

FORMAT = "q4t.project.task.migration.v1"
MAX_MIGRATIONS = 8
REGISTRY = "tools/project_tasks/compatible_runtimes.json"


def checksum(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def same_identity(before, after):
    store.require(set(before) == set(after) and checksum(after.get("key_sha256")) and
        {key: value for key, value in before.items() if key != "key_sha256"} ==
        {key: value for key, value in after.items() if key != "key_sha256"},
        "migration may rotate the key, not the client, TLS origin, CA, model or protocol", code='binding_changed')


def source_map(value):
    store.require(isinstance(value, dict) and 9 <= len(value) <= 64 and all(
        isinstance(name, str) and name.startswith("tools/") and ".." not in name.split("/") and
        len(name) <= 256 and checksum(digest) for name, digest in value.items()), "runtime identity map")


def transition(spec, target):
    """Validate a historical event without depending on today's runtime hash."""
    store.require(set(target) == set(spec) | {"budget_profile"}, "migration specification fields")
    same_identity(spec["binding"], target["binding"])
    source_map(spec["runtime_sources"]); source_map(target["runtime_sources"])
    store.require(spec.get("budget_profile") in (None, budget.PROFILE) and
        target["budget_profile"] == budget.PROFILE and
        {key: value for key, value in spec.items() if key not in
            ("binding", "runtime_sources", "budget_profile")} ==
        {key: value for key, value in target.items() if key not in
            ("binding", "runtime_sources", "budget_profile")}, "migration changes task contract/budgets")


def event(spec, state, value, task_id, store_id):
    store.require(set(value) == {"plan", "plan_sha256"} and
                  store.digest(value["plan"]) == value["plan_sha256"], "migration plan identity")
    plan = value["plan"]
    store.require(plan["format"] == FORMAT and plan["task_id"] == task_id and plan["store_id"] == store_id and
        plan["revision"] == state["revision"] and plan["source_spec"] == spec and
        plan["state"] == state["phase"] and plan["counters"] == counters(state) and
        state["migrations"] < MAX_MIGRATIONS and isinstance(plan["reason"], str) and
        0 < len(plan["reason"]) <= 512, "migration source state/reason")
    transition(spec, plan["target_spec"])
    return deepcopy(plan["target_spec"])


def counters(state):
    return {name: state[name] for name in ("requests", "tool_attempts", "tool_results", "runs", "preflight_checks")}


def supported(spec, current):
    if spec["runtime_sources"] == current and spec.get("budget_profile") == budget.PROFILE:
        return "current"
    registry = json.loads((store.ROOT / REGISTRY).read_bytes())
    store.require(registry["format"] == "q4t.project.task.compatibility.v1", "compatibility registry format")
    for release in registry["releases"]:
        if (spec["runtime_sources"] == release["runtime_sources"] and
                spec.get("budget_profile") == release["budget_profile"]):
            return release["release"]
    store.require(False, "unregistered task runtime; a reviewed adapter is required",
                  code="runtime_unsupported")


def build(ledger, state, binding, root, root_meta, reason):
    from tools.project_tasks import engine
    store.require(isinstance(reason, str) and 0 < len(reason) <= 512, "migration reason required", code='invalid_arguments')
    spec, current = state["execution_spec"], engine.sources()
    release = supported(spec, current)
    target = dict(deepcopy(spec), binding=deepcopy(binding), runtime_sources=current, budget_profile=budget.PROFILE)
    transition(spec, target)
    store.require(target != spec, "task already has this runtime and credential binding", code='already_exists')
    store.require(state["migrations"] < MAX_MIGRATIONS, "migration count exhausted", code='task_budget')
    files = {path.name: dict(bytes=store.private(path).st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest()) for path in ledger.path.iterdir()
        if path.name != ".lock"}
    store.require("ledger.db-journal" not in files, "unsettled SQLite journal")
    return dict(format=FORMAT, root=str(root), root_device=root_meta["root_device"],
        root_inode=root_meta["root_inode"], store_id=root_meta["store_id"], task_id=ledger.meta["task_id"],
        revision=state["revision"], state=state["phase"], counters=counters(state),
        source_release=release, source_spec=deepcopy(spec), target_spec=target,
        artifacts=files, reason=reason)


def prepare(root, binding, key, output, reason):
    from tools.project_tasks import engine, retention
    root, meta = store.checked_root(root)
    output = store.local(output)
    store.private(output.parent, True)
    store.require(not output.is_relative_to(root), "store contains only its managed task entries", code='unsafe_path')
    name = store.task_name(binding["client_id"], key)
    try:
        with store.open_task(root, name) as ledger:
            state = engine.replay(ledger)
            plan = build(ledger, state, binding, root, meta, reason)
            store.write_new(output, store.encode(plan))
    except store.ArchivedTask:
        return retention.prepare(root, binding, key, "rekey", output, reason)
    return dict(prepared=True, task_id=name, state=state["phase"], revision=state["revision"],
        plan=str(output), plan_sha256=store.digest(plan), source_release=plan["source_release"],
        key_changed=plan["source_spec"]["binding"] != binding, no_execution=True)


def apply(root, binding, key, path, expected, *, checkpoint=None):
    from tools.project_tasks import engine, retention
    path = store.local(path)
    store.require(store.private(path).st_size <= 65536 and checksum(expected), "bounded plan and exact digest required", code='invalid_arguments')
    plan = engine.chat.load_json(path.read_bytes())
    if plan.get("format") == retention.PLAN_FORMAT:
        store.require(plan["kind"] == "rekey", "only credential migration for archived tasks")
        result = retention.apply(root, binding, key, path, expected, checkpoint=checkpoint)
        result["migration_applied"] = result.pop("applied")
        return result
    store.require(store.digest(plan) == expected and plan["format"] == FORMAT, "migration plan changed", code='plan_changed')
    root, meta = store.checked_root(root)
    name = store.task_name(binding["client_id"], key)
    store.require(plan["root"] == str(root) and plan["store_id"] == meta["store_id"] and
        plan["root_device"] == meta["root_device"] and plan["root_inode"] == meta["root_inode"] and
        plan["task_id"] == name and plan["target_spec"]["binding"] == binding and
        plan["target_spec"]["runtime_sources"] == engine.sources(), "migration destination/runtime changed", code='plan_changed')
    with store.open_task(root, name) as ledger:
        state = engine.replay(ledger)
        applied = [row for row in ledger.events() if row["kind"] == "migration"]
        if any(row["payload"]["plan_sha256"] == expected for row in applied):
            engine.check_binding(ledger, binding, state)
            return dict(engine.public(ledger, state), migration_applied=False, already_applied=True, no_execution=True)
        store.require(plan == build(ledger, state, binding, root, meta, plan["reason"]),
                      "migration plan is stale or task evidence changed", code='plan_stale')
        payload = dict(plan=plan, plan_sha256=expected)
        engine.validate_next(ledger, "migration", payload)
        if checkpoint: checkpoint("before_commit", ledger)
        ledger.append("migration", payload)
        if checkpoint: checkpoint("after_commit", ledger)
        state = engine.replay(ledger)
        return dict(engine.public(ledger, state), migration_applied=True, already_applied=False, no_execution=True)
