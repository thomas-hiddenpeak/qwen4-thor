"""Exercise the publication entry point against real files for all operator pointers."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.serve_audit.refresh_release_handoff import refresh


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class OperatorHandoffTest(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.root = Path(area.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.matrix = self.docs / "serve-master-goals-v2.json"
        write(self.matrix, {"tasks": [{"id": "Q03", "current_day_handoff": {"revision": 2}},
                                      {"id": "R07", "current_cold_preflight": {"revision": 2}}]})
        self.status = self.docs / "STATUS.md"
        self.status.write_text("## 当前实际默认\n\nold\n\n## 全部任务进展与剩余缺口\n\n| Q02/Q03 | old |\n")
        build = self.root / "build"
        package = build / "service"
        package.mkdir(parents=True)
        manifest = package / "manifest.json"
        manifest.write_bytes(b"primary")
        (build / "q4t").write_bytes(b"engine")
        primary = digest(manifest)
        fallback = hashlib.sha256(b"fallback").hexdigest()
        issued = dict(status="PASS", desired="STOPPED", protected_files=13, model_starts=0,
                      profile_budget_preserved=True, primary=primary, fallback=fallback,
                      engine=digest(build/"q4t"), publication_revision=4)
        state = build / "service-native-state"
        write(state/"service-publication/policy.json", {"state": {"policy": {
            "phase":"stable", "revision":4, "current":{"manifest_sha256":primary}}}})
        write(state/"version-selection/policy.json", {"state":{"policy":{"registry":{"payloads":{
            "primary":{"manifest_sha256":primary}, "fallback":{"manifest_sha256":fallback}}}}}})
        self.descriptor = dict(package=str(package), manifest_sha256=primary)
        day = self.root / "independent-output/day/plan.json"
        cold = self.root / "cold/plan.json"
        write(day, dict(output=str(day.parent), package=self.descriptor, source_commit="source",
                        profile="day", day_run_owner="user", audio_cycles=[1], sources={},
                        protected=[dict(path=str(manifest), sha256=primary)]))
        write(cold, dict(output=str(cold.parent), package=self.descriptor, source_commit="source",
                         host_root_filter_installed=False, other_services_modified=False))
        write(day.parent/"handoff.json", dict(plan=str(day), user_runs_day=True,
                                               automatically_started=False))
        self.entry = day.parent/"run.sh"
        self.entry.write_text("#!/bin/sh\nexit 0\n")
        self.entry.chmod(0o700)
        self.receipt = self.docs/"delivery.json"
        self.data = dict(stage="stopped_default_and_operator_handoff", publication=issued,
                         required_methods=10, declared_methods=8,
                         day=dict(plan=str(day), plan_sha256=digest(day), source="source",
                                  operator=str(self.entry), operator_sha256=digest(self.entry),
                                  audio_pairs=1, first_hour_business=3, business_requests=12,
                                  checked_at="now", minimum_evidence_free_bytes=10, hours_executed=0),
                         cold=dict(plan=str(cold), plan_sha256=digest(cold), model_actions=0,
                                   host_units_modified=False))

    def update(self):
        write(self.receipt, self.data)
        refresh(self.root, self.root, self.receipt)
        return {t["id"]: t for t in json.loads(self.matrix.read_bytes())["tasks"]}

    def test_both_entries_and_workspace_follow_registered_primary(self):
        rows = self.update()
        self.assertEqual(rows["Q03"]["current_native_day_handoff"]["package"], self.descriptor)
        self.assertEqual(rows["Q03"]["current_native_day_handoff"]["workspace"], str(self.root))
        self.assertEqual(rows["Q03"]["current_day_handoff"]["revision"], 4)
        self.assertEqual(rows["R07"]["current_cold_preflight"]["plan"], self.data["cold"]["plan"])
        self.assertFalse(rows["R07"]["current_cold_preflight"]["executed"])
        self.assertEqual(rows, self.update())

    def assert_rejected(self):
        before = (self.matrix.read_bytes(), self.status.read_bytes())
        with self.assertRaises(ValueError):
            self.update()
        self.assertEqual(before, (self.matrix.read_bytes(), self.status.read_bytes()))

    def test_changed_operator_and_new_cold_digest_for_wrong_package(self):
        original = self.entry.read_bytes()
        self.entry.write_bytes(original+b"# changed")
        self.assert_rejected()
        self.entry.write_bytes(original)
        cold = Path(self.data["cold"]["plan"])
        plan = json.loads(cold.read_bytes())
        plan["package"]["manifest_sha256"] = self.data["publication"]["fallback"]
        write(cold, plan)
        self.data["cold"]["plan_sha256"] = digest(cold)
        self.assert_rejected()

    def test_an_executed_cold_campaign_cannot_be_advertised_as_preflight(self):
        self.data["cold"]["model_actions"] = 1
        self.assert_rejected()

    def test_completed_user_run_survives_repeat_refresh_and_revision_change(self):
        rows = self.update()
        rows["Q03"]["current_day_handoff"]["hours_run"] = 24
        write(self.matrix, {"tasks":list(rows.values())})
        self.assertEqual(self.update()["Q03"]["current_day_handoff"]["hours_run"], 24)
        rows["Q03"]["current_day_handoff"]["revision"] = 2
        write(self.matrix, {"tasks":list(rows.values())})
        final = self.update()["Q03"]
        self.assertEqual(final["current_day_handoff"]["hours_run"], 0)
        self.assertEqual(final["executed_operator_handoffs"][0]["hours_run"], 24)


if __name__ == "__main__":
    unittest.main()
