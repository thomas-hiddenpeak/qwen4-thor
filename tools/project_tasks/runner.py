"""Explicit durable project queries. Never starts/stops services or retries."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import store, engine, transport


def submit(root, binding, key, question, client, **options):
    spec = engine.specification(binding, question, **options)
    name = store.task_name(binding["client_id"], key)
    _, created = store.create(root, name, spec)
    with store.open_task(root, name) as ledger:
        store.require(ledger.spec == spec, "idempotency key conflicts with existing task")
        state = engine.replay(ledger)
        result = (engine.run(ledger, binding, client) if created else
                  engine.public(ledger, state))
        return dict(result, created=created)


def resume(root, binding, key, client, **options):
    name = store.task_name(binding["client_id"], key)
    with store.open_task(root, name) as ledger:
        return engine.run(ledger, binding, client, **options)


def inspect(root, binding, key):
    name = store.task_name(binding["client_id"], key)
    try:
        with store.open_task(root, name, exclusive=False) as ledger:
            engine.check_binding(ledger, binding)
            return engine.public(ledger, engine.replay(ledger))
    except BlockingIOError:
        return dict(task_id=name, state="ACTIVE", automatic_retry=False)


def resolve(root, binding, key, decision, revision, reason):
    name = store.task_name(binding["client_id"], key)
    with store.open_task(root, name) as ledger:
        return engine.resolve(ledger, binding, decision, revision, reason)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "submit", "inspect", "resume", "resolve"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-tasks", type=int, default=16)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--key")
    parser.add_argument("--question")
    parser.add_argument("--max-requests", type=int, default=4)
    parser.add_argument("--max-tools", type=int, default=8)
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument("--decision", choices=("discard-request", "repeat-read", "abandon"))
    parser.add_argument("--revision")
    parser.add_argument("--reason")
    args = parser.parse_args()
    if args.action == "init":
        result = store.init(args.root, args.max_tasks)
    else:
        store.require(args.profile is not None and args.key is not None,
                      "private profile and idempotency key required")
        value, binding = transport.profile(args.profile)
        if args.action == "submit":
            result = submit(args.root, binding, args.key, args.question,
                transport.Client(value), max_requests=args.max_requests,
                max_tools=args.max_tools, stream=not args.no_stream)
        elif args.action == "inspect":
            result = inspect(args.root, binding, args.key)
        elif args.action == "resume":
            result = resume(args.root, binding, args.key, transport.Client(value))
        else:
            result = resolve(args.root, binding, args.key, args.decision,
                             args.revision, args.reason)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("state", "COMPLETED") == "COMPLETED" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        # Raw HTTP bodies and private profile values never enter terminal errors.
        print(json.dumps(dict(error=type(error).__name__, automatic_retry=False)), file=sys.stderr)
        raise SystemExit(1)
