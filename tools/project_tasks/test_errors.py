"""Public command contracts against real private stores, locks and SQLite."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import errno
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from tools.project_tasks import errors, store, engine as e, runner, transport
from tools.project_tasks import retention as r, backup as b, migration as m
from tools.project_tasks import test_tasks as old, migration_fixtures as f

CANARY = "PRIVATE_ERROR_CANARY_" + "x" * 32
CA = store.ROOT / ".q4t-work/serve-clients-managed-20261104/gateway-package/gateway/certificate.pem"


class Errors(unittest.TestCase):
    execute = old.Tasks.execute
    count = old.Tasks.count
    status = old.Tasks.status
    break_at = old.Tasks.break_at
    resolve = old.Tasks.resolve

    def setUp(self):
        old.Tasks.setUp(self)
        self.profile = self.directory / (CANARY + ".json")
        value = {k: v for k, v in self.binding.items() if k not in ("ca_sha256", "key_sha256")}
        value.update(api_key=CANARY, ca_file=str(CA))
        store.write_new(self.profile, store.encode(value))
        _, self.binding = transport.profile(self.profile)
        self.spec = e.specification(self.binding, "read current", stream=False)
        self.root = self.directory / "cli-store"
        store.init(self.root)
        store.create(self.root, self.name, self.spec)
        self.results = []

    def tearDown(self):
        store.write_new(self.directory / "commands.json", store.encode(self.results))

    def command(self, args, *, program="runner", code=None, expected=1):
        child = subprocess.run([sys.executable, "-B", store.ROOT / f"tools/project_tasks/{program}.py", *map(str, args)],
            cwd=store.ROOT, capture_output=True, text=True, timeout=10)
        self.results.append(dict(program=program, returncode=child.returncode, stdout=child.stdout, stderr=child.stderr))
        self.assertEqual(child.returncode, expected, child.stderr)
        if code is not None:
            self.assertEqual(child.stdout, "")
            self.check_error(json.loads(child.stderr), code)
            self.assertEqual(len(child.stderr.splitlines()), 1)
        return child

    def check_error(self, value, code):
        self.assertEqual(value["profile"], "q4t-project-error-v1")
        self.assertEqual(value["code"], code)
        self.assertFalse(value["automatic_retry"])
        self.assertNotIn(CANARY, json.dumps(value))
        self.assertEqual(set(value), {"profile", "error", "code", "category", "message",
            "next_action", "operation", "http_status", "automatic_retry"})
        self.assertEqual((value["category"], value["message"], value["next_action"]), errors.CATALOG[code])

    def cli(self, action, code=None, extra=(), expected=1, key="key"):
        return self.command([action, "--root", self.root, "--profile", self.profile,
            "--key", key, *extra], code=code, expected=expected)

    def captured(self, operation, code, expected=1):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err): result = errors.run(operation)
        self.assertEqual(result, expected); self.assertEqual(out.getvalue(), "")
        value = json.loads(err.getvalue()); self.check_error(value, code)
        self.results.append(dict(returncode=result, stdout=out.getvalue(), stderr=err.getvalue()))
        return value

    def test_parser_redacts_invalid_choices_types_unknown_duplicate_and_abbreviation(self):
        for args in ([CANARY], ["init", "--root", self.root, "--max-tasks", CANARY],
            ["init", "--root", self.root, "--unknown", CANARY],
            ["init", "--root", self.root, "--root", CANARY],
            ["init", "--roo", CANARY]):
            with self.subTest(args=args): self.command(args, code="invalid_arguments")
        for program in ("runner", "backup"):
            self.command([CANARY], program=program, code="invalid_arguments")
            self.assertIn("usage:", self.command(["--help"], program=program, expected=0).stdout)

    def test_metadata_commands_reject_inapplicable_options_before_creating_store(self):
        absent = self.directory / "absent"
        for option in ("--reason", "--profile", "--key", "--revision"):
            self.command(["init", "--root", absent, option, CANARY], code="invalid_arguments")
            self.assertFalse(absent.exists())
        for action in ("retention-status", "enable-retention"):
            self.command([action, "--root", self.root, "--key", CANARY], code="invalid_arguments")
        self.command(["backup", "--root", self.root, "--snapshot", absent, "--reason", CANARY],
            program="backup", code="invalid_arguments")
        self.assertFalse(absent.exists())

    def test_task_commands_reject_ignored_or_missing_controls_without_ledger_change(self):
        before = b.regular_tree(self.root)
        for action, extra in (("inspect", ("--reason", CANARY)), ("resume", ("--decision", "abandon")),
            ("submit", ()), ("resolve", ("--decision", "abandon")),
            ("plan-archive", ("--plan", self.directory / "plan")),
            ("apply-migration", ("--plan", self.directory / "plan", "--plan-sha256", "0"*64, "--reason", CANARY))):
            self.cli(action, "invalid_arguments", extra=extra)
        self.assertEqual(b.regular_tree(self.root), before)

    def test_profile_shape_and_private_path_are_distinct(self):
        self.profile.write_text(json.dumps({"secret": CANARY}))
        self.cli("inspect", "invalid_profile")
        self.profile.chmod(0o644)
        self.cli("inspect", "unsafe_path")

    def test_active_and_permanent_key_capacity_are_distinct(self):
        root = self.directory / "capacity"; store.init(root, 1)
        store.create(root, self.name, self.spec)
        self.root = root
        self.cli("submit", "task_capacity", key="new", extra=("--question", "read current"))
        self.root = self.directory / "cli-store"
        r.enable(self.root, max_keys=1)
        self.cli("submit", "key_capacity", key="new", extra=("--question", "read current"))
        self.assertEqual(self.count(), 0)

    def test_real_lock_busy_and_read_only_active_status(self):
        with store.open_task(self.root, self.name):
            self.cli("resume", "busy")
            active = json.loads(self.cli("inspect", expected=2).stdout)
            self.assertEqual(active["state"], "ACTIVE")
        self.assertEqual(self.status()["state"], "READY")

    def test_key_and_binding_conflict_do_not_mutate_or_execute(self):
        before = b.regular_tree(self.root)
        self.cli("submit", "key_conflict", extra=("--question", CANARY))
        changed = r.read(self.profile); changed["api_key"] += "changed"
        self.profile.write_bytes(store.encode(changed))
        self.cli("resume", "binding_changed")
        self.assertEqual(b.regular_tree(self.root), before)
        self.assertEqual(self.count(), 0)

    def test_actual_corrupt_sqlite_and_missing_metadata_are_redacted(self):
        (self.root / "tasks" / self.name / "ledger.db").write_bytes(CANARY.encode())
        self.cli("inspect", "integrity_error")
        (self.root / "store.json").write_bytes(b'{}')
        self.cli("inspect", "internal_error")

    def test_real_sqlite_busy_full_and_readonly_codes(self):
        path = self.directory / "sqlite.db"
        db = sqlite3.connect(path, isolation_level=None)
        db.execute("PRAGMA page_size=4096"); db.execute("CREATE TABLE records(value)")
        db.execute("BEGIN EXCLUSIVE")
        other = sqlite3.connect(path, timeout=0, isolation_level=None)
        try: self.captured(lambda: other.execute("INSERT INTO records VALUES (?)", (CANARY,)), "busy")
        finally: other.close(); db.execute("ROLLBACK")
        db.execute("PRAGMA max_page_count=2")
        self.captured(lambda: db.execute("INSERT INTO records VALUES (zeroblob(65536))"), "storage_full")
        db.close()
        readonly = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try: self.captured(lambda: readonly.execute("INSERT INTO records VALUES (?)", (CANARY,)), "storage_permission")
        finally: readonly.close()

    def test_missing_existing_paths_and_headroom(self):
        self.command(["init", "--root", self.root], code="already_exists")
        self.command(["retention-status", "--root", self.directory / CANARY], code="not_found")
        with patch.object(store.shutil, "disk_usage", return_value=type("Disk", (), {"free":0})()):
            self.captured(lambda: store.create(self.root, store.task_name("alpha", "new"), self.spec), "storage_full")

    def test_pending_retention_and_policy_conflict(self):
        self.execute(); r.enable(self.root, max_keys=1)
        self.command(["enable-retention", "--root", self.root, "--max-keys", "2"], code="policy_conflict")
        path = self.directory / "archive.json"
        plan = r.prepare(self.root, self.binding, "key", "archive", path, "reviewed test")
        def stop(kind, *_):
            if kind == "operation_started": raise InterruptedError(CANARY)
        with self.assertRaises(InterruptedError): r.apply(self.root, self.binding, "key", path, plan["plan_sha256"], checkpoint=stop)
        self.cli("inspect", "retention_pending")
        pending = r.control(self.root / "retention", "operation")
        self.command(["finish-retention", "--root", self.root, "--operation-sha256", "0"*64], code="plan_changed")
        r.finish(self.root, store.digest(pending))
        self.assertEqual(self.count(), 1)

    def test_nonterminal_retention_period_and_wrong_digest(self):
        r.enable(self.root)
        path = self.directory / "archive.json"
        extra=("--plan", path, "--reason", "reviewed test")
        self.cli("plan-archive", "task_not_terminal", extra=extra)
        self.execute()
        plan = json.loads(self.cli("plan-archive", extra=extra, expected=0).stdout)
        self.cli("apply-archive", "plan_changed", extra=("--plan", path, "--plan-sha256", "0"*64))
        self.cli("apply-archive", extra=("--plan", path, "--plan-sha256", plan["plan_sha256"]), expected=0)
        self.cli("plan-prune", "retention_period", extra=("--plan", self.directory / "prune.json", "--reason", "test"))

    def test_retired_source_and_stale_snapshot_are_distinct(self):
        snapshot = self.directory / "snapshot"; saved=b.snapshot(self.root, snapshot)
        destination = self.directory / "restored"; plan = self.directory / "restore.json"
        prepared = b.restore_plan(self.root, snapshot, saved["snapshot_sha256"], destination, plan, "test")
        b.restore_apply(plan, prepared["plan_sha256"])
        self.cli("inspect", "store_retired")
        self.root = destination
        self.assertEqual(json.loads(self.cli("inspect", expected=2).stdout)["state"], "READY")
        self.captured(lambda: b.restore_plan(self.root, snapshot, saved["snapshot_sha256"],
            self.directory / "again", self.directory / "stale.json", "test"), "snapshot_stale")

    def test_runtime_migration_and_stale_plan_without_execution(self):
        # Use the accepted 11e source map, preserving the task's business contract.
        with store.open_task(self.root, self.name) as ledger:
            meta = deepcopy(ledger.meta); meta["spec"]["runtime_sources"] = f.release(4)["runtime_sources"]
            ledger.db.execute("UPDATE meta SET value=?", (store.encode(meta).decode(),))
        self.cli("resume", "runtime_changed")
        path = self.directory / "migration.json"
        plan = m.prepare(self.root, self.binding, "key", path, "reviewed upgrade")
        self.cli("resolve", "runtime_changed", extra=("--decision", "abandon", "--revision", "0"*64, "--reason", "test"))
        result = m.apply(self.root, self.binding, "key", path, plan["plan_sha256"])
        self.assertEqual(result["state"], "READY")
        self.assertEqual(self.count(), 0)
        # A second credential migration becomes stale after a state change.
        rotated=dict(self.binding,key_sha256="e"*64); stale=self.directory / "stale-plan.json"
        planned=m.prepare(self.root,rotated,"key",stale,"test")
        self.execute()
        self.captured(lambda:m.apply(self.root,rotated,"key",stale,planned["plan_sha256"]),"plan_stale")
        f.extract_runtime(4, self.directory / "accepted-runtime")

    def test_unknown_after_transport_error_stays_unknown_and_never_retries(self):
        client=old.FakeClient(fail=True)
        self.captured(lambda:self.execute(client), "transport_error")
        before=self.status(); self.assertEqual(before["state"], "REQUEST_UNKNOWN")
        self.assertEqual((before["requests"],self.count()),(1,0))
        held=json.loads(self.cli("resume",expected=2).stdout)
        self.assertEqual(held,before)
        self.cli("resolve", "resolution_conflict", extra=("--decision","discard-request","--revision","0"*64,"--reason","test"))
        self.assertEqual(self.status(),before)
        self.assertEqual(len(client.calls),1)

    def test_remote_body_and_unexpected_exception_never_reach_terminal(self):
        for status, code in ((401,"remote_auth"),(403,"remote_auth"),(429,"remote_capacity"),
            (503,"remote_capacity"),(500,"remote_error"),(200,"remote_error")):
            def fail(): raise e.chat.RemoteError(status, store.encode(dict(error=dict(message=CANARY,code=CANARY,param=CANARY))))
            value=self.captured(fail,code); self.assertEqual(value["http_status"],status)
        for exception,code,exitcode in ((KeyError(CANARY),"internal_error",1),
            (TypeError(CANARY),"internal_error",1),(RuntimeError(CANARY),"internal_error",1),
            (OSError(errno.ENOSPC,CANARY),"storage_full",1),(PermissionError(errno.EACCES,CANARY),"storage_permission",1),
            (KeyboardInterrupt(CANARY),"interrupted",130)):
            def fail(): raise exception
            self.captured(fail,code,exitcode)

    def test_success_and_valid_noncompleted_exits_remain_distinct(self):
        ready=json.loads(self.cli("inspect", expected=2).stdout)
        self.assertEqual(ready["state"],"READY")
        self.execute()
        done=self.cli("inspect",expected=0)
        self.assertEqual(json.loads(done.stdout)["state"],"COMPLETED")
        self.assertEqual(done.stderr,"")

    def test_error_catalog_matches_published_schema(self):
        schema=json.loads((store.ROOT / "docs/project-task-errors-v1.schema.json").read_bytes())
        self.assertEqual(set(schema["properties"]["code"]["enum"]),set(errors.CATALOG))
        self.assertEqual(set(schema["properties"]["operation"]["enum"]),{None,*errors.OPERATIONS})
        for code in errors.CATALOG:
            self.check_error(errors.public(errors.ContractError(CANARY,code)),code)


if __name__ == "__main__":
    unittest.main()
