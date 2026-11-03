"""Terminal payload lifecycle, permanent deduplication and real process crashes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from tools.project_tasks import engine as e, store, runner, migration as m
from tools.project_tasks import retention as r, test_tasks as old, migration_fixtures as f


class Archives(unittest.TestCase):
    setUp = old.Tasks.setUp
    execute = old.Tasks.execute
    count = old.Tasks.count
    status = old.Tasks.status
    break_at = old.Tasks.break_at
    resolve = old.Tasks.resolve

    def enable(self, **options):
        return r.enable(self.root, **dict(dict(max_keys=2, min_retain_seconds=0), **options))

    def plan(self, kind="archive", binding=None):
        self.target = binding or self.binding
        self.plan_path = self.directory / (kind + "-" + str(time.time_ns()) + ".json")
        self.prepared = r.prepare(self.root, self.target, "key", kind, self.plan_path, "reviewed CPU operation")
        return self.prepared

    def apply(self, checkpoint=None):
        return r.apply(self.root, self.target, "key", self.plan_path,
                       self.prepared["plan_sha256"], checkpoint=checkpoint)

    def archive(self):
        self.enable(); self.done = self.execute(); self.plan(); return self.apply()

    def duplicate(self, binding=None):
        client = old.FakeClient(fail=True)
        binding = binding or self.binding
        with patch.object(e.ProjectRecords, "execute", side_effect=AssertionError("archived tool ran")):
            value = runner.submit(self.root, binding, "key", "read current", client, stream=False)
            resumed = runner.resume(self.root, binding, "key", client)
        self.assertEqual((value["state"], value["created"], resumed["state"]), ("ARCHIVED", False, "ARCHIVED"))
        self.assertNotIn("answer", value)
        self.assertEqual((client.cap_calls, client.calls, client.preflight_calls), (0, [], []))
        return value

    def test_enable_is_explicit_private_and_identity_preserving(self):
        before = (self.root / "store.json").read_bytes()
        self.assertTrue(self.enable()["created"])
        self.assertFalse(self.enable()["created"])
        self.assertEqual((self.root / "store.json").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "cannot be replaced"):
            self.enable(min_retain_seconds=1)
        self.assertEqual(r.status(self.root)["catalog_reserved_bytes"], 65 * 1024**2)
        self.assertEqual(r.status(self.root)["active_tasks"], 1)

    def test_unpublished_initialization_can_resume_without_hidden_keys(self):
        real = os.rename
        def stop(source, target):
            if Path(target).name == "retention": raise InterruptedError("before directory publication")
            return real(source, target)
        with patch.object(r.os, "rename", side_effect=stop), self.assertRaises(InterruptedError): self.enable()
        self.assertFalse((self.root / "retention").exists())
        self.assertEqual(self.status()["state"], "READY")
        self.assertTrue(self.enable()["created"])

    def test_accepted_old_reader_refuses_extended_store(self):
        historical = self.directory / "old-runtime"; f.extract_runtime(2, historical)
        root = historical / ".q4t-work/tasks"; store.init(root)
        r.enable(root)
        child = subprocess.run([sys.executable, "-B", "-c",
            "from tools.project_tasks import store; store.checked_root(" + repr(str(root)) + ")"],
            cwd=historical, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("unknown store entry", child.stderr)

    def test_archive_preserves_bytes_answer_and_no_execution(self):
        self.enable(); done = self.execute()
        before = r.files(self.root / "tasks" / self.name)
        self.plan(); value = self.apply()
        self.assertEqual((value["terminal_state"], value["payload_state"]), ("COMPLETED", "retained"))
        self.assertEqual(r.files(self.root / "retention/payloads" / self.name), before)
        self.assertEqual(r.lookup(self.root, self.binding, "key", include_answer=True)["answer"], done["answer"])
        self.assertTrue(self.apply()["already_applied"])
        self.duplicate()
        self.assertEqual(self.count(), 1)
        with patch.object(e, "sources", return_value={"future-runtime": "f" * 64}): self.duplicate()

    def test_pruning_frees_payload_but_never_the_key(self):
        self.archive(); before = r.status(self.root)
        self.plan("prune"); result = self.apply()
        self.assertEqual(result["payload_state"], "purged")
        self.assertFalse((self.root / "retention/payloads" / self.name).exists())
        after = r.status(self.root)
        self.assertEqual((after["active_tasks"], after["archived_keys"], after["retained_payloads"]), (0, 1, 0))
        self.assertLess(after["archive_bytes"], before["archive_bytes"])
        self.assertNotIn("answer", r.lookup(self.root, self.binding, "key", include_answer=True))
        self.assertTrue(self.apply()["already_applied"])
        self.duplicate()

    def test_recovered_active_slot_and_permanent_key_capacity(self):
        root = self.directory / "one-slot"; store.init(root, 1); self.root = root
        store.create(root, self.name, self.spec)
        self.archive(); self.plan("prune"); self.apply()
        beta = dict(self.binding, client_id="beta"); name = store.task_name("beta", "key")
        store.create(root, name, e.specification(beta, "read current", stream=False))
        with store.open_task(root, name) as ledger:
            self.assertEqual(e.run(ledger, beta, old.FakeClient(), records=self.records)["state"], "COMPLETED")
        path = self.directory / "beta-archive.json"
        plan = r.prepare(root, beta, "key", "archive", path, "release slot")
        r.apply(root, beta, "key", path, plan["plan_sha256"])
        client = old.FakeClient()
        with self.assertRaisesRegex(ValueError, "permanent idempotency key capacity"):
            runner.submit(root, self.binding, "new", "read current", client, stream=False)
        self.assertEqual((client.cap_calls, client.calls), (0, []))

    def test_conflicts_never_reexecute_after_prune(self):
        self.archive(); self.plan("prune"); self.apply()
        for changes in (dict(question="different"), dict(max_tokens=64), dict(max_requests=3),
                        dict(stream=True), dict(binding=dict(self.binding, key_sha256="e" * 64))):
            options = dict(binding=self.binding, question="read current", stream=False); options.update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "binding|conflicts"):
                runner.submit(self.root, options.pop("binding"), "key", options.pop("question"), old.FakeClient(), **options)

    def test_active_unknown_and_busy_tasks_protected(self):
        self.enable()
        with self.assertRaisesRegex(ValueError, "terminal"): self.plan()
        self.break_at("request")
        with self.assertRaisesRegex(ValueError, "terminal"): self.plan()
        with store.open_task(self.root, self.name), self.assertRaises(BlockingIOError): self.plan()
        self.resolve("abandon"); self.plan(); value = self.apply()
        self.assertEqual(value["terminal_state"], "ABANDONED")

    def test_tool_unknown_is_not_archived(self):
        self.enable(); self.break_at("tool_started")
        with self.assertRaisesRegex(ValueError, "terminal"): self.plan()
        self.assertEqual(self.count(), 0)

    def test_failed_terminal_is_archivable(self):
        self.enable(); value = self.execute(old.FakeClient(bad_citation=True))
        self.assertEqual(value["state"], "FAILED")
        self.plan(); self.assertEqual(self.apply()["terminal_state"], "FAILED")

    def test_minimum_retention_is_captured_and_enforced(self):
        self.enable(min_retain_seconds=60); self.execute(); self.plan(); self.apply()
        with self.assertRaisesRegex(ValueError, "has not elapsed"): self.plan("prune")
        now = time.time_ns()
        with patch.object(r.time, "time_ns", return_value=now + 61 * 10**9): self.plan("prune"); self.apply()
        self.assertEqual(self.duplicate()["payload_state"], "purged")

    def test_byte_capacity_refuses_before_moving_or_journaling(self):
        class Padded(old.FakeClient):
            def complete(self, request, *, sink):
                result = super().complete(request, sink=sink); sink(b" " * 600000); return result
        self.enable(max_bytes=r.CATALOG_RESERVE + 1024**2)
        self.execute(Padded()); self.plan()
        with self.assertRaisesRegex(ValueError, "byte capacity"): self.apply()
        self.assertEqual((r.status(self.root)["state"], r.status(self.root)["archived_keys"]), ("READY", 0))
        self.assertTrue((self.root / "tasks" / self.name).is_dir())

    def test_exact_digest_stale_content_and_unknown_entries_refused(self):
        self.enable(); self.execute(); self.plan()
        with self.assertRaisesRegex(ValueError, "plan/destination"):
            r.apply(self.root, self.binding, "key", self.plan_path, "0" * 64)
        wire = self.root / "tasks" / self.name / "wire-001.bin"
        original = wire.read_bytes(); wire.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "wire/response identity"): self.apply()
        wire.write_bytes(original)
        other = wire.parent / "unknown"; store.write_new(other, b"keep")
        with self.assertRaisesRegex(ValueError, "unknown task"): self.apply()
        self.assertEqual(other.read_bytes(), b"keep")

    def test_rekey_only_changes_bounded_binding_history(self):
        self.archive(); folder = self.root / "retention/payloads" / self.name; before = r.files(folder)
        for index in range(8):
            target = dict(self.binding, key_sha256=store.digest(["rotation", index]))
            path = self.directory / ("rotation-" + str(index) + ".json")
            plan = m.prepare(self.root, target, "key", path, "reviewed same-client key")
            self.assertTrue(m.apply(self.root, target, "key", path, plan["plan_sha256"])["migration_applied"])
            self.assertFalse(m.apply(self.root, target, "key", path, plan["plan_sha256"])["migration_applied"])
            with self.assertRaisesRegex(ValueError, "binding"): self.duplicate()
            self.binding = target; self.duplicate()
        self.assertEqual(r.files(folder), before)
        self.assertEqual(r.lookup(self.root, self.binding, "key", include_answer=True)["answer"], self.done["answer"])
        with self.assertRaisesRegex(ValueError, "rotation limit"):
            m.prepare(self.root, dict(self.binding, key_sha256="f" * 64), "key", self.directory / "ninth.json", "limit")

    def test_pruned_rekey_and_scope_changes(self):
        self.archive(); self.plan("prune"); self.apply()
        for field in ("ca_sha256", "model", "protocol_sha256"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "rotate"):
                self.plan("rekey", dict(self.binding, **{field: "f" * 64}))
        new = dict(self.binding, key_sha256="e" * 64); self.plan("rekey", new); self.apply()
        self.assertEqual(self.duplicate(new)["payload_state"], "purged")

    def kill(self, point):
        marker = self.directory / ("kill-" + point + ".json")
        with (self.directory / ("kill-" + point + ".log")).open("xb") as log:
            process = subprocess.Popen([sys.executable, "-B", str(store.ROOT / "tools/project_tasks/archive_worker.py"),
                str(self.root), "key", str(self.plan_path), self.prepared["plan_sha256"], str(marker), point],
                cwd=store.ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                until = time.monotonic() + 10
                while not marker.exists() and process.poll() is None and time.monotonic() < until: time.sleep(.01)
                self.assertTrue(marker.exists(), "worker checkpoint missing; inspect child log")
                value = json.loads(marker.read_bytes()); self.assertEqual(value["pid"], process.pid)
                process.kill(); self.assertEqual(process.wait(5), -signal.SIGKILL)
                store.write_new(self.directory / ("killed-" + point + ".json"), store.encode(dict(value, returncode=process.returncode)))
                return value
            finally:
                if process.poll() is None: process.kill(); process.wait(5)

    def test_real_sigkill_atomic_metadata_and_archive_boundaries(self):
        for point in ("metadata_before_commit", "operation_started", "payload_moved", "key_published", "operation_finished"):
            with self.subTest(point=point):
                if point != "metadata_before_commit": self.setUp()
                self.enable(); done = self.execute(); self.plan(); marker = self.kill(point)
                state = r.status(self.root)
                if point == "metadata_before_commit":
                    self.assertEqual((state["state"], state["archived_keys"]), ("READY", 0))
                    self.assertEqual(self.status()["answer"], done["answer"])
                    self.apply()
                else:
                    if point != "operation_finished":
                        self.assertEqual(state["state"], "RETENTION_PENDING")
                        with self.assertRaisesRegex(ValueError, "incomplete"): self.duplicate()
                        with self.assertRaisesRegex(ValueError, "digest"): r.finish(self.root, "0" * 64)
                    r.finish(self.root, marker["operation_sha256"])
                    self.assertTrue(r.finish(self.root, marker["operation_sha256"])["already_finished"])
                self.duplicate()
                self.assertEqual(r.lookup(self.root, self.binding, "key", include_answer=True)["answer"], done["answer"])

    def test_real_sigkill_partial_prune_and_finished_delete(self):
        for point in ("metadata_before_commit", "payload_file_removed", "payload_removed", "key_published"):
            with self.subTest(point=point):
                if point != "metadata_before_commit": self.setUp()
                self.archive(); self.plan("prune"); marker = self.kill(point)
                if point == "metadata_before_commit":
                    self.assertEqual(self.duplicate()["payload_state"], "retained"); self.apply()
                else:
                    with self.assertRaisesRegex(ValueError, "incomplete"): self.duplicate()
                    r.finish(self.root, marker["operation_sha256"])
                self.assertEqual(self.duplicate()["payload_state"], "purged")

    def test_real_sigkill_rekey_keeps_pending_access_closed(self):
        self.archive(); new = dict(self.binding, key_sha256="e" * 64); self.plan("rekey", new)
        marker = self.kill("key_published")
        with self.assertRaisesRegex(ValueError, "incomplete"): self.duplicate(new)
        r.finish(self.root, marker["operation_sha256"])
        self.duplicate(new)
        with self.assertRaisesRegex(ValueError, "binding"): self.duplicate()

    def test_partial_delete_never_follows_replaced_files(self):
        self.archive(); self.plan("prune"); marker = self.kill("payload_file_removed")
        folder = self.root / "retention/payloads" / self.name
        file = next(p for p in folder.iterdir() if p.name != ".lock")
        saved = self.directory / "saved-original"; file.rename(saved)
        sentinel = self.directory / "outside"; store.write_new(sentinel, b"must remain")
        file.symlink_to(sentinel)
        with self.assertRaisesRegex(ValueError, "private local"): r.finish(self.root, marker["operation_sha256"])
        self.assertEqual(sentinel.read_bytes(), b"must remain")
        file.unlink(); saved.rename(file); r.finish(self.root, marker["operation_sha256"])

    def test_actual_three_generations_remain_reconstructable(self):
        from tools.project_tasks import migration_report
        self.assertEqual(migration_report.verify()["records"], 468)
        for index in (0, 1):
            root = self.directory / ("historical-" + str(index)); store.init(root)
            item = f.clone_history(index, root, "key", None); r.enable(root)
            path = self.directory / ("historical-" + str(index) + ".json")
            prepared = r.prepare(root, item["binding"], "key", "archive", path, "accepted historical data")
            r.apply(root, item["binding"], "key", path, prepared["plan_sha256"])
            self.assertIn("Qwen4ExpForConditionalGeneration", r.lookup(root, item["binding"], "key", include_answer=True)["answer"])
        data = f.archive(2); root = self.directory / "historical-2"
        prefix = "historical-alpha/.q4t-work/tasks/"
        for name, raw in data.items():
            if name.startswith(prefix):
                dest = root / name.removeprefix(prefix); dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                store.write_new(dest, raw)
        meta = json.loads((root / "store.json").read_bytes())
        meta.update(root_device=root.stat().st_dev, root_inode=root.stat().st_ino)
        (root / "store.json").write_bytes(store.encode(meta))  # Fixture copy only.
        name = store.task_name("alpha", "migration-query")
        with store.open_task(root, name) as ledger:
            state = e.replay(ledger); binding = state["execution_spec"]["binding"]; answer = e.public(ledger, state)["answer"]
        r.enable(root); path = self.directory / "historical-2.json"
        prepared = r.prepare(root, binding, "migration-query", "archive", path, "accepted migrated data")
        r.apply(root, binding, "migration-query", path, prepared["plan_sha256"])
        self.assertEqual(r.lookup(root, binding, "migration-query", include_answer=True)["answer"], answer)


if __name__ == "__main__":
    unittest.main()
