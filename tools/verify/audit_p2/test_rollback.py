"""CPU-only sealed rollback checks; all writes use disposable build fixtures."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import rollback


class RollbackTest(unittest.TestCase):
    def setUp(self):
        work = rollback.ROOT / 'build/audit-rollback-tests'
        work.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / '.q4t-work/audit-p2-20261031/package'
        self.build = self.root / 'build'
        self.build.mkdir()
        self.output = self.root / '.q4t-work/restore'
        self.output.mkdir(parents=True)
        self.addCleanup(patch.stopall)
        patch.object(rollback, 'ROOT', self.root).start()
        for version in ['previous', 'candidate']:
            folder = self.package / version
            folder.mkdir(parents=True)
            (folder / 'q4t').write_text(version)
            (folder / 'CMakeCache.txt').write_text('build-' + version)
            metadata = dict(binary_sha256=rollback.h.sha(folder / 'q4t'),
                            build_cache=str(folder / 'CMakeCache.txt'),
                            build_cache_sha256=rollback.h.sha(
                                folder / 'CMakeCache.txt'))
            (folder / 'q4t.release.json').write_text(json.dumps(metadata))
        rollback.h.seal(self.package)
        self.install('candidate')

    def install(self, version):
        for name in ['q4t', 'q4t.release.json']:
            shutil.copy2(self.package / version / name, self.build / name)

    def invoke(self, *extra):
        with patch.object(sys, 'argv', ['rollback.py', '--package',
                str(self.package), '--output', str(self.output), *extra]):
            rollback.main()

    def test_readonly_verifies_known_pairs_without_guard(self):
        for version in ['candidate', 'previous']:
            self.install(version)
            with patch.object(rollback.h, 'require_guard') as lease:
                self.invoke('--check')
                lease.assert_not_called()
            self.assertFalse((self.output / 'restored.json').exists())

    def test_unknown_installation_refused_before_any_write(self):
        (self.build / 'q4t').write_text('foreign')
        before = {p.name: p.read_bytes() for p in self.build.iterdir()}
        with self.assertRaisesRegex(RuntimeError, 'unknown installed identity'):
            self.invoke()
        self.assertEqual(before, {p.name: p.read_bytes()
                                  for p in self.build.iterdir()})

    def test_tampered_package_refused(self):
        (self.package / 'previous/q4t').write_text('damaged')
        with self.assertRaisesRegex(RuntimeError, 'sealed file differs'):
            self.invoke()
        self.assertEqual((self.build / 'q4t').read_text(), 'candidate')

    def test_restore_and_partial_pair_recovery_require_lease(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                self.output = self.root / '.q4t-work' / str(partial)
                self.output.mkdir()
                self.install('candidate')
                if partial:
                    shutil.copy2(self.package / 'previous/q4t', self.build / 'q4t')
                    (self.build / 'audit-p2-rollback-recovery.json').write_text(
                        json.dumps(dict(package=str(self.package))))
                with patch.object(rollback.h, 'require_guard') as lease:
                    self.invoke()
                    lease.assert_called_once_with(self.output)
                for name in ['q4t', 'q4t.release.json']:
                    self.assertEqual((self.build / name).read_bytes(),
                                     (self.package / 'previous' / name).read_bytes())
                self.assertFalse((self.build / 'audit-p2-rollback-recovery.json').exists())
                self.assertEqual(json.loads((self.output / 'restored.json').read_text())[
                    'status'], 'RESTORED')

    def test_foreign_recovery_marker_refused(self):
        marker = self.build / 'audit-p2-rollback-recovery.json'
        marker.write_text(json.dumps(dict(package='foreign')))
        with patch.object(rollback.h, 'require_guard'), \
                self.assertRaisesRegex(RuntimeError, 'foreign recovery'):
            self.invoke()
        self.assertEqual((self.build / 'q4t').read_text(), 'candidate')
        self.assertEqual(json.loads(marker.read_text())['package'], 'foreign')


if __name__ == '__main__':
    unittest.main()
