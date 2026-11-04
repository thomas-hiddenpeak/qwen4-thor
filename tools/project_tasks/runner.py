"""Explicit durable project queries. Never starts/stops services or retries."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import store, engine, transport, migration, retention, errors


def submit(root, binding, key, question, client, **options):
    spec = engine.specification(binding, question, **options)
    name = store.task_name(binding["client_id"], key)
    try:
        _, created = store.create(root, name, spec)
        with store.open_task(root, name) as ledger:
            state = engine.replay(ledger)
            store.require(state["execution_spec"] == spec, "idempotency key conflicts with existing task", code='key_conflict')
            result = (engine.run(ledger, binding, client) if created else engine.public(ledger, state))
            return dict(result, created=created)
    except store.ArchivedTask:
        return dict(retention.lookup(root, binding, key, spec=spec), created=False)


def resume(root, binding, key, client, **options):
    name = store.task_name(binding["client_id"], key)
    try:
        with store.open_task(root, name) as ledger:
            return engine.run(ledger, binding, client, **options)
    except store.ArchivedTask:
        return retention.lookup(root, binding, key)


def inspect(root, binding, key):
    name = store.task_name(binding["client_id"], key)
    try:
        with store.open_task(root, name, exclusive=False) as ledger:
            engine.check_binding(ledger, binding)
            return engine.public(ledger, engine.replay(ledger))
    except store.ArchivedTask:
        return retention.lookup(root, binding, key)
    except BlockingIOError:
        return dict(task_id=name, state="ACTIVE", automatic_retry=False)


def resolve(root, binding, key, decision, revision, reason):
    name = store.task_name(binding["client_id"], key)
    with store.open_task(root, name) as ledger:
        return engine.resolve(ledger, binding, decision, revision, reason)


def main():
    os.umask(0o077)
    parser = errors.Parser(description=__doc__)
    parser.add_argument("action", choices=("init", "submit", "inspect", "resume", "resolve",
        "plan-migration", "apply-migration", "enable-retention", "retention-status", "finish-retention",
        "plan-archive", "apply-archive", "plan-prune", "apply-prune", "inspect-archive"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-tasks", type=int)
    parser.add_argument("--max-keys", type=int)
    parser.add_argument("--archive-bytes", type=int)
    parser.add_argument("--min-retain-seconds", type=int)
    parser.add_argument("--operation-sha256")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--key")
    parser.add_argument("--question")
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--max-tools", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--no-stream", action="store_true", default=None)
    parser.add_argument("--decision", choices=("discard-request", "repeat-read", "abandon"))
    parser.add_argument("--revision")
    parser.add_argument("--reason")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    args = parser.parse_args()
    task = ("root", "profile", "key")
    requirements = {
        "init": (("root",), ("max_tasks",)),
        "enable-retention": (("root",), ("max_keys", "archive_bytes", "min_retain_seconds")),
        "retention-status": (("root",), ()),
        "finish-retention": (("root", "operation_sha256"), ()),
        "submit": ((*task, "question"), ("max_requests", "max_tools", "max_tokens", "no_stream")),
        "inspect": (task, ()), "resume": (task, ()), "inspect-archive": (task, ()),
        "resolve": ((*task, "decision", "revision", "reason"), ()),
    }
    for kind in ("migration", "archive", "prune"):
        requirements["plan-" + kind] = ((*task, "plan", "reason"), ())
        requirements["apply-" + kind] = ((*task, "plan", "plan_sha256"), ())
    errors.options(args, *requirements[args.action])
    if args.action == "init":
        result = store.init(args.root, 16 if args.max_tasks is None else args.max_tasks)
    elif args.action == "enable-retention":
        result = retention.enable(args.root, max_keys=4096 if args.max_keys is None else args.max_keys,
            max_bytes=1024**3 if args.archive_bytes is None else args.archive_bytes,
            min_retain_seconds=86400 if args.min_retain_seconds is None else args.min_retain_seconds)
    elif args.action == "retention-status":
        result = retention.status(args.root)
    elif args.action == "finish-retention":
        store.require(args.operation_sha256 is not None, "exact pending operation digest required")
        result = retention.finish(args.root, args.operation_sha256)
    else:
        store.require(args.profile is not None and args.key is not None,
                      "private profile and idempotency key required")
        value, binding = transport.profile(args.profile)
        if args.action == "submit":
            result = submit(args.root, binding, args.key, args.question,
                transport.Client(value), max_requests=4 if args.max_requests is None else args.max_requests,
                max_tools=8 if args.max_tools is None else args.max_tools,
                max_tokens=512 if args.max_tokens is None else args.max_tokens, stream=not args.no_stream)
        elif args.action == "inspect":
            result = inspect(args.root, binding, args.key)
        elif args.action == "resume":
            result = resume(args.root, binding, args.key, transport.Client(value))
        elif args.action == "plan-migration":
            store.require(args.plan is not None and args.plan_sha256 is None,
                          "new private plan path required", code='invalid_arguments')
            result = migration.prepare(args.root, binding, args.key, args.plan, args.reason)
        elif args.action == "apply-migration":
            store.require(args.plan is not None and args.plan_sha256 is not None,
                          "exact private plan and digest required")
            result = migration.apply(args.root, binding, args.key, args.plan, args.plan_sha256)
        elif args.action in ("plan-archive", "plan-prune"):
            store.require(args.plan is not None and args.plan_sha256 is None, "new private retention plan required")
            result = retention.prepare(args.root, binding, args.key, args.action.removeprefix("plan-"), args.plan, args.reason)
        elif args.action in ("apply-archive", "apply-prune"):
            store.require(args.plan is not None and args.plan_sha256 is not None, "exact retention plan and digest required")
            store.require(retention.read(args.plan)["kind"] == args.action.removeprefix("apply-"), "wrong retention operation", code='invalid_arguments')
            result = retention.apply(args.root, binding, args.key, args.plan, args.plan_sha256)
        elif args.action == "inspect-archive":
            result = retention.lookup(args.root, binding, args.key, include_answer=True)
        else:
            result = resolve(args.root, binding, args.key, args.decision,
                             args.revision, args.reason)
    print(json.dumps(result, ensure_ascii=False))
    completed = result.get("state", "COMPLETED") == "COMPLETED"
    metadata_only = args.action not in ("submit", "resume", "inspect") and result.get("no_execution")
    return 0 if completed or metadata_only else 2


if __name__ == "__main__":
    raise SystemExit(errors.run(main))
