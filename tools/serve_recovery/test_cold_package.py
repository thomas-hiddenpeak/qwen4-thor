"""Reject stale cold candidates without rebuilding or mutating sealed roots."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_recovery import cold_acceptance as driver
from tools.serve_service import package as engine


class PackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=driver.ROOT/'.q4t-work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.origin = self.root/'sealed'
        (self.origin/'bin').mkdir(parents=True)
        (self.origin/'manifest.json').write_text('{"cpu_fixture":true}')
        (self.root/'control.py').write_text('current control source')
        (self.root/'guardian.cpp').write_text('current native source')
        self.manifest = dict(files={
            'source/control.py': engine.sha(self.root/'control.py')})
        self.native = dict(source_files={
            'guardian.cpp': engine.sha(self.root/'guardian.cpp')})
        for name, key in (('q4t-guardian', 'guardian_sha256'),
                          ('q4t-cuda-probe', 'cuda_probe_sha256'),
                          ('q4t-ingress', 'ingress_sha256'),
                          ('q4t-device-control', 'device_control_sha256')):
            path = self.origin/'bin'/name
            path.write_text('CPU fixture: '+name)
            self.native[key] = engine.sha(path)
            self.manifest['files']['bin/'+name] = engine.sha(path)
        self.persist()
        self.bundle = SimpleNamespace(
            verify=mock.Mock(return_value=self.manifest),
            SOURCES={'control.py'}, NATIVE_SOURCES={'guardian.cpp'})
        patch = mock.patch.object(driver, 'ROOT', self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def persist(self):
        (self.origin/'native-build.json').write_text(json.dumps(self.native))

    def review(self):
        return driver.reviewed_package(self.origin, self.bundle, engine)

    def test_reuses_verified_package_without_writing_or_substituting(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        manifest, receipt = self.review()
        self.assertIs(manifest, self.manifest)
        self.assertEqual(receipt, dict(package=str(self.origin),
            manifest_sha256=engine.sha(self.origin/'manifest.json')))
        self.bundle.verify.assert_called_once_with(
            self.origin, receipt['manifest_sha256'])
        self.assertEqual(before,
            {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_rejects_stale_and_incomplete_control_sources(self):
        for source in ({'source/control.py': '0'*64}, {},
                       {'source/control.py': engine.sha(self.root/'control.py'),
                        'source/unreviewed.py': '1'*64}):
            with self.subTest(source=source):
                self.manifest['files'] = {
                    k: v for k, v in self.manifest['files'].items()
                    if not k.startswith('source/')}
                self.manifest['files'].update(source)
                with self.assertRaisesRegex(RuntimeError, 'runtime source'):
                    self.review()

    def test_rejects_stale_or_incomplete_native_source_receipt(self):
        for source in ({'guardian.cpp': '0'*64}, {},
                       {'guardian.cpp': engine.sha(self.root/'guardian.cpp'),
                        'extra.cpp': '1'*64}):
            with self.subTest(source=source):
                self.native['source_files'] = source
                self.persist()
                with self.assertRaisesRegex(RuntimeError, 'native source'):
                    self.review()

    def test_rejects_native_build_or_artifact_identity_drift(self):
        self.native['device_control_sha256'] = '0'*64
        self.persist()
        with self.assertRaisesRegex(RuntimeError, 'native artifact'):
            self.review()
        self.native['device_control_sha256'] = self.manifest['files'][
            'bin/q4t-device-control']
        self.persist()
        (self.origin/'bin/q4t-guardian').write_text('different CPU fixture')
        with self.assertRaisesRegex(RuntimeError, 'native artifact'):
            self.review()

    def test_sealed_verification_failure_prevents_source_reuse(self):
        self.bundle.verify.side_effect = ValueError('sealed identity rejected')
        with self.assertRaisesRegex(ValueError, 'sealed identity rejected'):
            self.review()


if __name__ == '__main__':
    unittest.main()
