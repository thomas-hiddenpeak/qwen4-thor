"""Historical tasks, atomic handoff, preserved uncertainty and real process death."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.project_tasks import engine as e, migration as m, runner, store
from tools.project_tasks import migration_fixtures as f, test_tasks as old


class Migrations(unittest.TestCase):
    setUp = old.Tasks.setUp
    execute = old.Tasks.execute
    status = old.Tasks.status
    count = old.Tasks.count
    break_at = old.Tasks.break_at

    def plan(self, binding=None, label="plan"):
        self.target = binding or dict(self.binding, key_sha256="e" * 64)
        self.plan_path = self.directory / (label + ".json")
        self.prepared = m.prepare(self.root, self.target, "key", self.plan_path, "explicit reviewed handoff")
        return self.prepared

    def apply(self):
        return m.apply(self.root, self.target, "key", self.plan_path, self.prepared["plan_sha256"])

    def test_key_rotation_preserves_meta_history_budget_and_idempotency(self):
        self.break_at("tool_result")
        with store.open_task(self.root, self.name) as ledger:
            before_meta, before_events = deepcopy(ledger.meta), ledger.events()
            before_wire = ledger.wire(1).read_bytes()
        self.plan(); value = self.apply()
        self.assertEqual((value["requests"], value["tool_attempts"], value["migrations"]), (1, 1, 1))
        with store.open_task(self.root, self.name) as ledger:
            self.assertEqual(ledger.meta, before_meta)
            self.assertEqual(ledger.events()[:-1], before_events)
            self.assertEqual(ledger.wire(1).read_bytes(), before_wire)
        with self.assertRaisesRegex(ValueError, "binding"):
            self.status()
        self.binding = self.target
        fake = old.FakeClient()
        result = self.execute(fake)
        self.assertEqual((result["state"], len(fake.calls), self.count(), result["runs"]), ("COMPLETED", 1, 1, 2))
        duplicate = runner.submit(self.root, self.binding, "key", "read current", fake, stream=False)
        self.assertEqual(duplicate, dict(result, created=False))
        self.assertEqual(len(fake.calls), 1)
        applied = self.apply()
        self.assertTrue(applied["already_applied"])
        self.assertEqual(applied["revision"], result["revision"])

    def test_no_change_plan_and_unknown_runtime_refused(self):
        with self.assertRaisesRegex(ValueError, "already has"):
            self.plan(self.binding)
        spec = deepcopy(self.spec); spec["runtime_sources"]["tools/project_tasks/engine.py"] = "f" * 64
        name = store.task_name("alpha", "unregistered"); store.create(self.root, name, spec)
        with self.assertRaisesRegex(ValueError, "unregistered"):
            m.prepare(self.root, self.target, "unregistered", self.directory / "unknown.json", "reason")

    def test_identity_scope_cannot_change(self):
        for key, value in (("client_id", "beta"), ("base_url", "https://other.test/v1"),
                           ("connect_host", "127.0.0.2"), ("model", "other"),
                           ("protocol_sha256", "f" * 64), ("template_sha256", "f" * 64), ("ca_sha256", "f" * 64)):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "migration may rotate"):
                m.same_identity(self.binding, dict(self.binding, **{key: value}))

    def test_target_spec_cannot_expand_budget_or_change_question(self):
        self.plan()
        original = json.loads(self.plan_path.read_bytes())
        for key, value in (("max_requests", 8), ("max_tools", 32), ("max_tokens", 64),
                           ("question", "different"), ("stream", True)):
            changed = deepcopy(original); changed["target_spec"][key] = value
            self.plan_path.write_bytes(store.encode(changed))
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.apply(self.root, self.target, "key", self.plan_path, store.digest(changed))
        self.assertNotIn("migrations", self.status())

    def test_wrong_digest_and_stale_revision_refused(self):
        self.plan()
        with self.assertRaisesRegex(ValueError, "plan changed"):
            m.apply(self.root, self.target, "key", self.plan_path, "0" * 64)
        result = self.execute()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.apply()
        self.assertEqual(self.status()["revision"], result["revision"])

    def test_task_and_store_destination_bound(self):
        self.plan()
        spec = deepcopy(self.spec); other = self.directory / "other"
        store.init(other); store.create(other, self.name, spec)
        with self.assertRaisesRegex(ValueError, "destination"):
            m.apply(other, self.target, "key", self.plan_path, self.prepared["plan_sha256"])
        with self.assertRaisesRegex(ValueError, "destination"):
            m.apply(self.root, self.target, "another-key", self.plan_path, self.prepared["plan_sha256"])

    def test_runtime_and_profile_cannot_change_after_plan(self):
        self.plan()
        with patch.object(e, "sources", return_value={}), self.assertRaisesRegex(ValueError, "runtime"):
            self.apply()
        with self.assertRaisesRegex(ValueError, "destination"):
            m.apply(self.root, dict(self.target, key_sha256="f" * 64), "key", self.plan_path, self.prepared["plan_sha256"])

    def test_exclusive_writer_blocks_plan_and_apply(self):
        self.plan()
        with store.open_task(self.root, self.name):
            with self.assertRaises(BlockingIOError):
                self.apply()
            with self.assertRaises(BlockingIOError):
                m.prepare(self.root, self.target, "key", self.directory / "busy.json", "reason")

    def test_unknown_model_or_tool_never_replayed_by_migration(self):
        for boundary in ("request", "tool_started"):
            with self.subTest(boundary=boundary):
                fixture = old.Tasks("test_complete_then_duplicate_is_read_only"); fixture.setUp()
                fixture.break_at(boundary)
                new = dict(fixture.binding, key_sha256="e" * 64); path = fixture.directory / "plan.json"
                plan = m.prepare(fixture.root, new, "key", path, "preserve uncertainty")
                value = m.apply(fixture.root, new, "key", path, plan["plan_sha256"])
                fake = old.FakeClient()
                again = runner.resume(fixture.root, new, "key", fake)
                self.assertEqual(again["state"], "REQUEST_UNKNOWN" if boundary == "request" else "TOOL_UNKNOWN")
                self.assertEqual(again["revision"], value["revision"])
                self.assertEqual((fake.cap_calls, len(fake.calls), fixture.count()), (0, 0, 0))

    def test_pending_approval_is_rechecked_after_migration(self):
        self.break_at("preflight_result")
        self.plan(); value = self.apply()
        self.assertEqual((value["state"], value["requests"]), ("PREFLIGHT_APPROVED", 0))
        self.binding = self.target; fake = old.FakeClient(); result = self.execute(fake)
        self.assertEqual((result["preflight_checks"], len(fake.preflight_calls)), (3, 2))

    def test_terminal_tasks_retain_answer_and_do_not_execute(self):
        done = self.execute()
        self.plan(); moved = self.apply()
        self.binding = self.target; fake = old.FakeClient(); after = self.execute(fake)
        self.assertEqual(after["answer"], done["answer"])
        self.assertEqual((after["revision"], len(fake.calls), fake.cap_calls), (moved["revision"], 0, 0))
        self.assertEqual(after["requests"], done["requests"])

    def test_migration_count_is_bounded_and_run_count_never_reset(self):
        for index in range(m.MAX_MIGRATIONS):
            self.plan(dict(self.binding, key_sha256=hashlib.sha256(str(index).encode()).hexdigest()), str(index))
            value = self.apply(); self.binding = self.target
            self.assertEqual((value["migrations"], value["runs"]), (index + 1, 0))
        with self.assertRaisesRegex(ValueError, "count exhausted"):
            self.plan(dict(self.binding, key_sha256="f" * 64), "too-many")

    def test_changed_unknown_wire_invalidates_reviewed_plan(self):
        fake = old.FakeClient(fail=True)
        with self.assertRaises(ConnectionError): self.execute(fake)
        self.plan()
        (self.root / "tasks" / self.name / "wire-001.bin").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.apply()

    def test_real_death_before_and_after_commit_is_recoverable(self):
        for boundary in ("before_commit", "after_commit"):
            with self.subTest(boundary=boundary):
                self.plan(dict(self.binding, key_sha256=hashlib.sha256(boundary.encode()).hexdigest()), boundary)
                marker = self.directory / (boundary + ".marker")
                script = """import json,sys,threading
from pathlib import Path
from tools.project_tasks import migration,store
root,path,digest,stage,marker=sys.argv[1:]
binding=json.loads(Path(path).read_bytes())['target_spec']['binding']
def hook(kind,ledger):
    if kind==stage:
        store.write_new(Path(marker),b'reached')
        threading.Event().wait()
migration.apply(root,binding,'key',path,digest,checkpoint=hook)
"""
                process = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.root), str(self.plan_path),
                    self.prepared["plan_sha256"], boundary, str(marker)], cwd=store.ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 5
                    while not marker.exists() and process.poll() is None and time.monotonic() < deadline: time.sleep(.01)
                    self.assertTrue(marker.exists(), "child did not reach transaction boundary")
                    process.kill(); process.communicate(timeout=5)
                    self.assertEqual(process.returncode, -signal.SIGKILL)
                    value = self.apply()
                    self.assertEqual(value["already_applied"], boundary == "after_commit")
                    self.binding = self.target
                    result = subprocess.run([sys.executable, "-B", "-c",
                        "import json,sys;from tools.project_tasks import runner;"
                        "print(json.dumps(runner.inspect(sys.argv[1],json.loads(sys.argv[2]),'key')))",
                        str(self.root), json.dumps(self.binding)], cwd=store.ROOT, capture_output=True, text=True, check=True)
                    self.assertEqual(json.loads(result.stdout)["revision"], value["revision"])
                finally:
                    if process.poll() is None: process.kill(); process.communicate(timeout=5)

    def test_actual_historical_formats_preserve_saved_result_and_gain_preflight(self):
        for index in (0, 1):
            with self.subTest(release=index):
                root = self.directory / ("legacy-" + str(index)); store.init(root)
                fixture = f.clone_history(index, root, "key", "tool_result")
                binding = dict(fixture["binding"], key_sha256="e" * 64)
                path = self.directory / ("legacy-plan-" + str(index) + ".json")
                plan = m.prepare(root, binding, "key", path, "reviewed accepted historical runtime")
                applied = m.apply(root, binding, "key", path, plan["plan_sha256"])
                fake = old.FakeClient()
                def capabilities(**kwargs):
                    fake.cap_calls += 1; value = old.FakeClient().capabilities()
                    value["protocol_source_sha256"] = binding["protocol_sha256"]
                    value["template"]["reference_sha256"] = binding["template_sha256"]
                    return value
                def preflight(body, *, sink):
                    fake.preflight_calls.append(deepcopy(body)); value = old.preflight_response(body)
                    value["protocol_source_sha256"] = binding["protocol_sha256"]; sink(store.encode(value)); return value
                def complete(body, *, sink):
                    fake.calls.append(deepcopy(body))
                    raw = fixture["final_wire"].replace(fixture["final"]["id"].encode(), body["request_id"].encode())
                    return e.chat.read_sse(io.BytesIO(raw), sink=sink)
                fake.capabilities, fake.preflight, fake.complete = capabilities, preflight, complete
                with store.open_task(root, fixture["name"]) as ledger:
                    with patch.object(e.ProjectRecords, "execute", side_effect=AssertionError("known result reread")):
                        result = e.run(ledger, binding, fake)
                self.assertEqual((result["state"], result["requests"], result["saved_tool_results"], len(fake.calls)),
                                 ("COMPLETED", 2, 1, 1))
                self.assertEqual(result["preflight_checks"], applied["preflight_checks"] + 1)
                self.assertIn("Qwen4ExpForConditionalGeneration", result["answer"])

    def test_historical_unknown_state_needs_explicit_resolution(self):
        root = self.directory / "legacy-unknown"; store.init(root)
        fixture = f.clone_history(0, root, "key", "request")
        path = self.directory / "legacy-unknown.json"
        plan = m.prepare(root, fixture["binding"], "key", path, "preserve unknown generation")
        moved = m.apply(root, fixture["binding"], "key", path, plan["plan_sha256"])
        fake = old.FakeClient(); value = runner.resume(root, fixture["binding"], "key", fake)
        self.assertEqual((value["state"], fake.cap_calls, len(fake.calls)), ("REQUEST_UNKNOWN", 0, 0))
        resolved = runner.resolve(root, fixture["binding"], "key", "discard-request", moved["revision"], "explicit decision")
        self.assertEqual((resolved["state"], resolved["requests"], resolved["remaining_requests"]), ("READY", 1, 1))

    def test_accepted_archive_verifiers_still_reconstruct_actual_old_tasks(self):
        from tools.project_tasks import report, budget_report
        self.assertEqual(report.verify()["records"], 411)
        self.assertEqual(budget_report.verify()["records"], 263)

    def test_historical_source_extraction_matches_registered_bytes(self):
        for index in (0, 1):
            root = self.directory / ("runtime-" + str(index))
            receipt = f.extract_runtime(index, root)
            self.assertEqual(receipt["sources"], {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in f.release(index)["runtime_sources"]})


if __name__ == "__main__":
    unittest.main()
