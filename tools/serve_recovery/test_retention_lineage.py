"""Legacy delivery protection and sealed deployment paths; CPU fixtures only."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from tools.serve_recovery import retention as r
from tools.serve_recovery import test_retention as fixtures


class LineageTests(unittest.TestCase):
    setUp = fixtures.RetentionTests.setUp
    write = fixtures.RetentionTests.write
    run_fixture = fixtures.RetentionTests.run_fixture
    archive = fixtures.RetentionTests.archive

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def chain(self, receipt):
        root = self.directory / 'release.json'
        self.write(root, dict(rollback_receipt=str(receipt),
                             rollback_sha256=self.digest(receipt)))
        sources = {}
        return r.dependencies(self.state, anchors=[root], records=sources), sources

    def test_legacy_rollback_formats_keep_pinned_metadata_and_packages(self):
        for kind in ('files', 'service_only_bool', 'service_only_digest',
                     'observation', 'preserved'):
            with self.subTest(kind=kind):
                directory = self.directory / kind; directory.mkdir(mode=0o700)
                package = directory / 'old-package'; package.mkdir(mode=0o700)
                names = ('q4t.release.json', 'service.release.json', 'service.json')
                for name in names:
                    self.write(directory / name, dict(package=str(package)))
                hashes = {n: self.digest(directory / n) for n in names}
                if kind == 'files':
                    value = dict(files=hashes)
                elif kind.startswith('service_only'):
                    value = dict(files={n: hashes[n] for n in names[1:]},
                        engine_unchanged=True if kind.endswith('bool') else
                            dict(binary_sha256='a' * 64, release_sha256='b' * 64))
                elif kind == 'observation':
                    value = dict(metadata_sha256=hashes[names[1]],
                                 descriptor_sha256=hashes[names[2]])
                else:
                    value = dict(preserved=[dict(path=str(self.directory / n),
                        rollback=str(directory / n), sha256=hashes[n]) for n in names])
                receipt = directory / 'receipt.json'; self.write(receipt, value)
                paths, sources = self.chain(receipt)
                self.assertIn(package, paths)
                for name in names[1:]:
                    self.assertIn(directory / name, paths)
                    self.assertEqual(sources[str(directory / name)], hashes[name])
                self.write(directory / 'service.release.json', dict(package=str(package), changed=True))
                with self.assertRaisesRegex(ValueError, 'digest changed'):
                    self.chain(receipt)

    def test_nested_inherited_hash_is_enforced_and_conflicts_are_rejected(self):
        old = self.directory / 'old.json'; self.write(old, dict(package=str(self.directory)))
        root = self.directory / 'root.json'
        self.write(root, dict(previous_release=str(old), inherited_acceptance=dict(
            release=dict(path=str(old), sha256=self.digest(old)))))
        self.assertIn(old, r.dependencies(self.state, anchors=[root]))
        self.write(old, dict(package=str(self.directory), changed=True))
        with self.assertRaisesRegex(ValueError, 'digest changed'):
            r.dependencies(self.state, anchors=[root])
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            r.pinned_digest([dict(path=str(old), sha256='a' * 64),
                             dict(path=str(old), sha256='b' * 64)], old)

    def test_mutable_legacy_lineage_invalidates_plan_without_changing_pins(self):
        run_id, path = self.run_fixture(); self.archive(run_id)
        old = self.directory / 'legacy.json'; self.write(old, dict(note='original'))
        old.chmod(0o664)  # Historical owner-held metadata is a protection input.
        root = self.directory / 'root.json'; self.write(root, dict(previous_release=str(old)))
        original = r.dependencies
        def anchored(state, **options):
            return original(state, anchors=[root], **options)
        with mock.patch.object(r, 'dependencies', side_effect=anchored):
            plan = r.plan(self.state, limits=r.policy(max_bytes=1))
            self.assertEqual(plan['remove'][0]['run_id'], run_id)
            self.assertEqual(plan['dependency_sources'][str(old)], self.digest(old))
            pins_before = anchored(self.state)
            self.write(old, dict(note='changed')); old.chmod(0o664)
            self.assertEqual(anchored(self.state), pins_before)
            with self.assertRaisesRegex(ValueError, 'plan or protections changed'):
                r.apply(self.state, plan)
        self.assertTrue(path.exists())
        self.assertFalse((self.state / 'retention' / (run_id + '.retire.json')).exists())

    def test_deployment_repository_is_bound_to_manifest_not_packaged_module_path(self):
        self.assertEqual(r.repository(self.state), fixtures.ROOT)
        manifest = self.directory / 'package/manifest.json'
        self.write(manifest, dict(repository=str(self.directory)))
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            r.repository(self.state)
        descriptor = json.loads((self.state / 'service.json').read_text())
        descriptor['manifest_sha256'] = self.digest(manifest)
        self.write(self.state / 'service.json', descriptor)
        with self.assertRaisesRegex(ValueError, 'outside deployment repository'):
            r.repository(self.state)


if __name__ == '__main__':
    unittest.main()
