"""Verify generation and fallback identities before replacing the STATUS handoff."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.serve_audit.refresh_release_handoff import refresh


class ReleaseHandoffTest(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.root = Path(area.name)
        self.docs = self.root / 'docs'
        self.docs.mkdir()
        self.status = self.docs / 'STATUS.md'
        self.original = ('history\n\n## 当前实际默认\n\nold entry\n\n'
                         '## 全部任务进展与剩余缺口\n\n| Q02/Q03 | old |\n')
        self.status.write_text(self.original)
        self.receipt = self.docs / 'delivery.json'
        self.build = self.root / 'build'
        (self.build / 'service').mkdir(parents=True)
        (self.build / 'q4t').write_bytes(b'engine')
        (self.build / 'service/manifest.json').write_bytes(b'primary')
        self.primary = hashlib.sha256(b'primary').hexdigest()
        self.fallback = hashlib.sha256(b'fallback').hexdigest()
        self.publication = dict(status='PASS', desired='STOPPED', model_starts=0,
                                publication_revision=24, protected_files=13,
                                profile_budget_preserved=True, primary=self.primary,
                                fallback=self.fallback, engine=hashlib.sha256(b'engine').hexdigest())
        self.publish = dict(phase='stable', revision=24,
                            current={'manifest_sha256': self.primary})
        self.persist('service-publication', self.publish)
        self.versions = dict(registry={'payloads': {
            'primary': {'manifest_sha256': self.primary},
            'fallback': {'manifest_sha256': self.fallback}}})
        self.persist('version-selection', self.versions)

    def persist(self, name, policy):
        path = self.build / 'service-native-state' / name / 'policy.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'state': {'policy': policy}}))

    def run_refresh(self, stage='issued', matched=19):
        self.receipt.write_text(json.dumps(dict(stage=stage, publication=self.publication,
                                                required_methods=21, declared_methods=matched)))
        refresh(self.root, self.root, self.receipt)

    def test_all_issued_receipts_refresh_idempotently(self):
        for stage in ('issued', 'device_issued_handoffs_prepared', 'stopped_default_and_operator_handoff'):
            with self.subTest(stage=stage):
                self.run_refresh(stage)
                first = self.status.read_text()
                self.run_refresh(stage)
                self.assertEqual(self.status.read_text(), first)
                self.assertTrue(first.startswith('history\n\n'))
                for token in ('rev24', self.primary, self.fallback, self.publication['engine'],
                              '21 个方法中的 19 个', '(delivery.json)'):
                    self.assertIn(token, first)

    def test_same_binary_and_packages_on_another_revision(self):
        self.persist('service-publication', self.publish | {'revision': 26})
        with self.assertRaisesRegex(ValueError, 'registration'):
            self.run_refresh()
        self.assertEqual(self.original, self.status.read_text())

    def test_inflight_publication_preserves_handoff(self):
        self.persist('service-publication', self.publish | {'phase': 'prepared'})
        with self.assertRaisesRegex(ValueError, 'registration'):
            self.run_refresh()
        self.assertEqual(self.original, self.status.read_text())

    def test_registration_must_bind_both_payloads(self):
        for role in ('primary', 'fallback'):
            with self.subTest(role=role):
                versions = json.loads(json.dumps(self.versions))
                versions['registry']['payloads'][role]['manifest_sha256'] = '0' * 64
                self.persist('version-selection', versions)
                with self.assertRaisesRegex(ValueError, 'registration'):
                    self.run_refresh()
                self.assertEqual(self.original, self.status.read_text())

    def test_candidate_and_invalid_method_counts_preserve_handoff(self):
        for stage, count in [('candidate', 19), ('issued', 22), ('issued', True)]:
            with self.subTest(stage=stage, count=count):
                with self.assertRaises(ValueError):
                    self.run_refresh(stage, count)
                self.assertEqual(self.original, self.status.read_text())


if __name__ == '__main__':
    unittest.main()
