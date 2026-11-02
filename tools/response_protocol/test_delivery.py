"""Recovery fault checks using disposable CPU fixtures, never real build/q4t."""
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import delivery as d


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        work = d.ROOT / 'build/chat-delivery-tests'
        work.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / '.q4t-work/chat-delivery-20261102'
        self.package = self.base / 'package'
        self.output = self.base / 'deployment'
        self.output.mkdir(parents=True)
        self.build = self.root / 'build'
        self.build.mkdir()
        self.marker = self.build / 'chat-delivery-recovery.json'
        self.addCleanup(patch.stopall)
        for key, value in dict(ROOT=self.root, BASE=self.base,
            PACKAGE=self.package, DEPLOYMENT=self.output, MARKER=self.marker).items():
            patch.object(d, key, value).start()
        for version in ['previous', 'candidate']:
            folder = self.package / version
            folder.mkdir(parents=True)
            (folder / 'q4t').write_text(version)
            (folder / 'CMakeCache.txt').write_text('cache-' + version)
            d.h.save(folder / 'q4t.release.json', dict(
                binary_sha256=d.h.sha(folder / 'q4t'),
                build_cache=str(folder / 'CMakeCache.txt'),
                build_cache_sha256=d.h.sha(folder / 'CMakeCache.txt')))
            patch.object(d, version.upper(), d.h.sha(folder / 'q4t')).start()
        d.h.save(self.package / 'tool-source.json', {})
        d.h.save(self.package / 'health-plan.json', dict(
            deadline_unix=time.time() + 1200, model_inputs={}))
        d.h.seal(self.package)
        self.install('candidate', 'candidate')

    def install(self, binary, metadata):
        shutil.copy2(self.package / binary / 'q4t', self.build / 'q4t')
        shutil.copy2(self.package / metadata / 'q4t.release.json',
                     self.build / 'q4t.release.json')

    def before(self):
        return {p.name: p.read_bytes() for p in self.build.iterdir()}

    def test_all_four_known_pairs_recover_exactly_under_lease(self):
        for binary in ['previous', 'candidate']:
            for metadata in ['previous', 'candidate']:
                self.install(binary, metadata)
                with patch.object(d.h, 'require_guard') as guard:
                    d.rollback(self.output)
                    guard.assert_called_once_with(self.output)
                self.assertFalse(self.marker.exists())
                for name in ['q4t', 'q4t.release.json']:
                    self.assertEqual((self.build / name).read_bytes(),
                        (self.package / 'previous' / name).read_bytes())

    def test_unknown_pair_is_refused_before_any_write(self):
        (self.build / 'q4t.release.json').write_text('foreign metadata')
        before = self.before()
        with patch.object(d.h, 'require_guard'), self.assertRaisesRegex(
                RuntimeError, 'unknown installed identity'):
            d.rollback(self.output)
        self.assertEqual(before, self.before())

    def test_tampered_backup_is_refused(self):
        (self.package / 'previous/q4t').write_text('corrupt')
        before = self.before()
        with patch.object(d.h, 'require_guard'), self.assertRaisesRegex(
                RuntimeError, 'sealed file differs'):
            d.rollback(self.output)
        self.assertEqual(before, self.before())

    def test_denied_lease_and_foreign_marker_preserve_files(self):
        before = self.before()
        with patch.object(d.h, 'require_guard', side_effect=RuntimeError('lease')):
            with self.assertRaisesRegex(RuntimeError, 'lease'):
                d.rollback(self.output)
        self.assertEqual(before, self.before())
        d.h.save(self.marker, dict(package='foreign'))
        before = self.before()
        with patch.object(d.h, 'require_guard'), self.assertRaisesRegex(
                RuntimeError, 'foreign recovery marker'):
            d.rollback(self.output)
        self.assertEqual(before, self.before())

    def test_partial_install_failure_restores_exact_pair(self):
        self.install('previous', 'previous')
        original = d.h.replace_checked
        calls = 0

        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('simulated metadata replace failure')
            return original(*args)

        before = self.before()
        with patch.object(d.h, 'require_guard'), \
                patch.object(d, 'install_interrupt_handlers'), \
                patch.object(d.h, 'replace_checked', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'simulated metadata'):
                d.deploy()
        self.assertEqual(before, self.before())
        state = d.h.read(self.output / 'deployment.json')
        self.assertEqual(state['status'], 'FAILED')
        self.assertEqual(state['rollback'], 'RESTORED')

    def test_environment_abort_cannot_finalize_or_clear_marker(self):
        (self.output / 'lease').mkdir()
        d.h.save(self.output / 'lease/exit.json', dict(status='ENV_ABORT',
                                                      child_returncode=0))
        d.h.save(self.marker, dict(package=str(self.package)))
        before = self.before()
        with self.assertRaisesRegex(RuntimeError, 'guard not clean'):
            d.finalize()
        self.assertEqual(before, self.before())

    def test_check_is_readonly_and_does_not_require_a_gpu_lease(self):
        before = self.before()
        with patch.object(d.h, 'require_guard') as guard:
            d.check()
            guard.assert_not_called()
        self.assertEqual(before, self.before())


if __name__ == '__main__':
    unittest.main()
