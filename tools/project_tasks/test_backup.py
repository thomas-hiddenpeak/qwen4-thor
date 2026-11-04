"""Whole-store consistency, single-active handoff and actual process death."""
from contextlib import contextmanager
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

from tools.project_tasks import backup as b, engine as e, migration as m, retention as r, runner, store
from tools.project_tasks import test_tasks as old, migration_fixtures as f


class Backups(unittest.TestCase):
    setUp = old.Tasks.setUp
    execute = old.Tasks.execute
    count = old.Tasks.count
    status = old.Tasks.status
    break_at = old.Tasks.break_at
    resolve = old.Tasks.resolve

    def snapshot(self, label="snapshot"):
        self.snapshot_path = self.directory / label
        self.snapshot_receipt = b.snapshot(self.root, self.snapshot_path)
        return self.snapshot_receipt

    def plan(self, label="restore"):
        self.destination = self.directory / label
        self.plan_path = self.directory / (label + ".json")
        self.prepared = b.restore_plan(self.root, self.snapshot_path, self.snapshot_receipt["snapshot_sha256"],
            self.destination, self.plan_path, "reviewed same-host handoff")
        return self.prepared

    def apply(self, checkpoint=None):
        return b.restore_apply(self.plan_path, self.prepared["plan_sha256"], checkpoint=checkpoint)

    def archived(self, key="archived", *, prune=False):
        r.enable(self.root, max_keys=8, min_retain_seconds=0)
        name = store.task_name("alpha", key); store.create(self.root, name, self.spec)
        with store.open_task(self.root, name) as ledger:
            done = e.run(ledger, self.binding, old.FakeClient(), records=self.records)
        for kind in (["archive", "prune"] if prune else ["archive"]):
            path = self.directory / (key + "-" + kind + ".json")
            plan = r.prepare(self.root, self.binding, key, kind, path, "CPU retention fixture")
            r.apply(self.root, self.binding, key, path, plan["plan_sha256"])
        return name, done

    def test_snapshot_sealed_exact_and_not_executable(self):
        self.execute(); before = b.regular_tree(self.root)
        receipt = self.snapshot(); value = b.verify(self.snapshot_path, receipt["snapshot_sha256"])
        self.assertEqual(value["capture"]["directories"], before["directories"])
        self.assertEqual(value["capture"]["files"], before["files"])
        self.assertEqual(b.regular_tree(self.root), before)
        self.assertTrue(b.snapshot(self.root, self.snapshot_path)["already_created"])
        with self.assertRaisesRegex(ValueError, "identity"): store.checked_root(self.snapshot_path / "data")

    def test_busy_store_or_task_cannot_be_snapshotted(self):
        with store.open_task(self.root, self.name), self.assertRaises(BlockingIOError): self.snapshot()
        self.assertFalse((self.directory / "snapshot").exists())
        with store.lock(self.root / ".lock"), self.assertRaises(BlockingIOError): self.snapshot()

    def test_snapshot_budget_rejects_before_copy(self):
        class Padded(old.FakeClient):
            def complete(self, request, *, sink):
                result = super().complete(request, sink=sink); sink(b" " * 600000); return result
        self.execute(Padded())
        with self.assertRaisesRegex(ValueError, "byte/manifest capacity"):
            b.snapshot(self.root, self.directory / "small", max_bytes=1024**2)
        self.assertFalse((self.directory / "small").exists())

    def test_unknown_files_links_or_pending_retention_refuse(self):
        retained, _ = self.archived()
        odd = self.root / "unknown"; store.write_new(odd, b"keep")
        with self.assertRaisesRegex(ValueError, "unknown store"): self.snapshot()
        odd.unlink()
        folder = self.root / "retention/payloads" / retained
        file = folder / "wire-001.bin"; saved = self.directory / "wire"; file.rename(saved); file.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, "private local"): self.snapshot()
        file.unlink(); saved.rename(file)
        path = self.directory / "pending.json"; plan = r.prepare(self.root, self.binding, "archived", "prune", path, "pending fixture")
        def stop(kind, _):
            if kind == "operation_started": raise InterruptedError("CPU pending")
        with self.assertRaises(InterruptedError): r.apply(self.root, self.binding, "archived", path, plan["plan_sha256"], checkpoint=stop)
        with self.assertRaisesRegex(ValueError, "operation pending"): self.snapshot()

    def test_stale_snapshot_and_plan_cannot_drop_new_work(self):
        self.snapshot(); self.plan(); self.execute()
        with self.assertRaisesRegex(ValueError, "stale"): self.apply()
        with self.assertRaisesRegex(ValueError, "stale"): self.plan("another")
        self.assertFalse((self.root / "handoff.json").exists())
        self.assertEqual(self.status()["state"], "COMPLETED")

    def test_targets_digest_parent_and_source_are_bound(self):
        self.snapshot(); self.plan()
        with self.assertRaisesRegex(ValueError, "plan digest"): b.restore_apply(self.plan_path, "0" * 64)
        self.destination.mkdir(mode=0o700); marker = self.destination / "owned"; store.write_new(marker, b"untouched")
        with self.assertRaisesRegex(ValueError, "destination exists"): self.apply()
        self.assertEqual(marker.read_bytes(), b"untouched")
        parent = self.directory / "target-parent"; parent.mkdir(mode=0o700)
        plan_path = self.directory / "parent-plan.json"
        value = b.restore_plan(self.root, self.snapshot_path, self.snapshot_receipt["snapshot_sha256"], parent / "store", plan_path, "parent proof")
        parent.rename(self.directory / "old-parent"); parent.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "binding"): b.restore_apply(plan_path, value["plan_sha256"])

    def test_missing_source_is_not_a_disaster_restore_approval(self):
        self.snapshot(); self.plan(); saved = self.directory / "source-offline"; self.root.rename(saved)
        with self.assertRaises(FileNotFoundError): self.apply()
        self.assertFalse(self.destination.exists()); saved.rename(self.root)

    def test_snapshot_corruption_and_overlap_refused(self):
        self.execute(); self.snapshot(); self.plan()
        wire = self.snapshot_path / "data/tasks" / self.name / "wire-001.bin"
        wire.write_bytes(wire.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "content differs"): self.apply()
        self.assertFalse(self.destination.exists())
        with self.assertRaisesRegex(ValueError, "overlap"): b.snapshot(self.root, self.root / "nested")

    def test_handoff_reuses_known_result_and_preserves_all_original_events(self):
        self.break_at("tool_result")
        before = b.regular_tree(self.root); self.snapshot(); self.plan(); self.apply()
        source = self.root; self.root = self.destination
        for name, row in before["files"].items():
            if name.startswith("tasks/"): self.assertEqual(r.sha(self.root / name), row["sha256"])
        (self.project / "docs/current.md").write_text("changed source\n")
        fake = old.FakeClient(); answer = self.execute(fake)
        self.assertEqual((answer["state"], len(fake.calls), self.count()), ("COMPLETED", 1, 1))
        self.assertIn("initial value", answer["answer"])
        with self.assertRaisesRegex(ValueError, "retired"): runner.resume(source, self.binding, "key", old.FakeClient())
        final = b.regular_tree(self.root)
        self.assertTrue(self.apply()["already_applied"])
        self.assertEqual(b.regular_tree(self.root), final)

    def test_unknown_request_and_tool_remain_unknown_after_restore(self):
        for point, phase in (("request", "REQUEST_UNKNOWN"), ("tool_started", "TOOL_UNKNOWN")):
            with self.subTest(point=point):
                if point != "request": self.setUp()
                self.break_at(point); before = self.status(); self.snapshot(); self.plan(); self.apply()
                client = old.FakeClient(); result = runner.resume(self.destination, self.binding, "key", client)
                self.assertEqual((result, result["state"]), (before, phase))
                self.assertEqual((client.calls, client.preflight_calls, client.cap_calls), ([], [], 0))

    def test_retained_payload_and_pruned_key_survive_restore(self):
        retained, completed = self.archived(); purged, _ = self.archived("purged", prune=True)
        before = r.key_value(self.root / "retention", retained)
        deleted = r.key_value(self.root / "retention", purged)
        self.snapshot(); self.plan(); self.apply(); self.root = self.destination
        after = r.key_value(self.root / "retention", retained)
        self.assertEqual((after["base"], after["events"][:-1]), (before["base"], before["events"]))
        self.assertEqual(after["events"][-1]["kind"], "placement")
        self.assertEqual(r.key_value(self.root / "retention", purged), deleted)
        self.assertEqual(r.lookup(self.root, self.binding, "archived", include_answer=True)["answer"], completed["answer"])
        fake = old.FakeClient()
        for key in ("archived", "purged"):
            self.assertEqual(runner.submit(self.root, self.binding, key, "read current", fake, stream=False)["state"], "ARCHIVED")
        path = self.directory / "new-prune.json"; plan = r.prepare(self.root, self.binding, "archived", "prune", path, "restored payload")
        r.apply(self.root, self.binding, "archived", path, plan["plan_sha256"])
        new = dict(self.binding, key_sha256="e" * 64); path = self.directory / "new-key.json"
        plan = m.prepare(self.root, new, "archived", path, "restored same-client key")
        m.apply(self.root, new, "archived", path, plan["plan_sha256"])
        self.assertEqual(r.lookup(self.root, new, "archived")["payload_state"], "purged")
        self.assertEqual((fake.calls, fake.cap_calls), ([], 0))

    def test_restore_can_be_backed_up_again_with_bounded_history(self):
        retained, answer = self.archived()
        for number in range(8):
            self.snapshot("snapshot-" + str(number)); self.plan("target-" + str(number)); self.apply(); self.root = self.destination
            value = r.key_value(self.root / "retention", retained)
            self.assertEqual(r.key_state(value, store.checked_root(self.root)[1], retained)["placements"], number + 1)
            self.assertEqual(r.lookup(self.root, self.binding, "archived", include_answer=True)["answer"], answer["answer"])
        self.snapshot("snapshot-limit")
        with self.assertRaisesRegex(ValueError, "handoff limit"): self.plan("ninth")

    def test_stale_check_cannot_enter_retired_source_after_lock(self):
        self.snapshot(); self.plan(); real = store.lock; armed = True
        @contextmanager
        def race(path, **options):
            nonlocal armed
            if Path(path) == self.root / ".lock" and armed:
                armed = False; self.apply()
            with real(path, **options): yield
        with patch.object(store, "lock", race), self.assertRaisesRegex(ValueError, "retired"):
            store.create(self.root, store.task_name("alpha", "new"), self.spec)
        self.assertEqual(len(list((self.destination / "tasks").iterdir())), 1)

    def test_new_and_accepted_old_code_reject_unpublished_candidate(self):
        self.archived(); self.snapshot(); self.plan()
        def check(kind, value):
            if kind not in ("placement_before_commit", "placement_committed", "candidate_ready"): return
            candidate = self.destination.parent / (".q4t-restore-" + self.prepared["plan_sha256"]) / "candidate"
            with self.assertRaises(ValueError): store.checked_root(candidate)
        self.apply(check)
        old_runtime = self.directory / "old-runtime"; f.extract_runtime(3, old_runtime)
        # Check using the accepted old store module against the actual new root;
        # only its artifact-path base is adapted by the test harness.
        script = "from pathlib import Path; from tools.project_tasks import store; store.ROOT=Path(" + repr(str(store.ROOT)) + "); store.checked_root(" + repr(str(self.destination)) + ")"
        child = subprocess.run([sys.executable, "-B", "-c", script], cwd=old_runtime, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(child.returncode, 0); self.assertIn("unknown store entry", child.stderr)

    def kill(self, action, point):
        marker = self.directory / (action + "-" + point + ".json")
        source, other = (self.root, self.directory / "snapshot") if action == "backup" else (self.plan_path, self.prepared["plan_sha256"])
        with (self.directory / (action + "-" + point + ".log")).open("xb") as log:
            child = subprocess.Popen(list(map(str, [sys.executable, "-B", store.ROOT / "tools/project_tasks/backup_worker.py",
                action, source, other, marker, point])), cwd=store.ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                until = time.monotonic() + 15
                while not marker.exists() and child.poll() is None and time.monotonic() < until: time.sleep(.01)
                self.assertTrue(marker.exists(), "checkpoint absent; inspect child log")
                value = json.loads(marker.read_bytes()); self.assertEqual(value["pid"], child.pid)
                child.kill(); self.assertEqual(child.wait(5), -signal.SIGKILL)
                store.write_new(self.directory / ("killed-" + action + "-" + point + ".json"), store.encode(dict(value, returncode=child.returncode)))
                return value
            finally:
                if child.poll() is None: child.kill(); child.wait(5)

    def test_real_snapshot_death_and_same_source_continuation(self):
        for point in ("copy_chunk", "before_seal", "snapshot_sealed"):
            with self.subTest(point=point):
                if point != "copy_chunk": self.setUp()
                self.execute(); before = b.regular_tree(self.root); self.kill("backup", point)
                receipt = self.snapshot()
                self.assertEqual(b.verify(self.snapshot_path, receipt["snapshot_sha256"])["capture"]["files"], before["files"])
                self.assertEqual(b.regular_tree(self.root), before)

    def test_interrupted_snapshot_refuses_later_source_state(self):
        self.break_at("tool_result"); self.kill("backup", "copy_chunk"); self.execute()
        with self.assertRaisesRegex(ValueError, "source changed"): self.snapshot()
        self.assertFalse((self.directory / "snapshot/seal.json").exists())

    def test_real_restore_death_at_copy_placement_and_fence(self):
        for point in ("copy_chunk", "placement_before_commit", "placement_committed", "candidate_ready", "fence_before_publish", "source_fenced", "destination_published"):
            with self.subTest(point=point):
                if point != "copy_chunk": self.setUp()
                retained, answer = self.archived(); self.break_at("tool_result"); before = self.status()
                self.snapshot(); self.plan(); self.kill("restore", point)
                if point in ("fence_before_publish", "source_fenced", "destination_published"):
                    with self.assertRaisesRegex(ValueError, "retired"): self.status()
                else: self.assertEqual(self.status(), before)
                self.apply()
                self.assertEqual(runner.inspect(self.destination, self.binding, "key"), before)
                self.assertEqual(r.lookup(self.destination, self.binding, "archived", include_answer=True)["answer"], answer["answer"])
                self.assertTrue(self.apply()["already_applied"])

    def test_partial_draft_corruption_preserves_source(self):
        self.execute(); self.snapshot(); self.plan(); marker = self.kill("restore", "copy_chunk")
        partial = Path(marker["value"]["path"] + ".copy-part")
        raw = partial.read_bytes(); partial.write_bytes(b"x" + raw[1:])
        with self.assertRaisesRegex(ValueError, "prefix differs"): self.apply()
        self.assertEqual(self.status()["state"], "COMPLETED")
        self.assertFalse(self.destination.exists())

    def test_previous_four_archives_still_verify(self):
        from tools.project_tasks import report, budget_report, migration_report, archive_report
        for module, count in ((report, 411), (budget_report, 263), (migration_report, 468), (archive_report, 402)):
            with self.subTest(count=count): self.assertEqual(module.verify()["records"], count)


if __name__ == "__main__":
    unittest.main()
