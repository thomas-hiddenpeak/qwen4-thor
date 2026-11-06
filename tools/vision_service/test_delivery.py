"""File-transaction failure/recovery tests; no model, HTTP or GPU."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.vision_service import delivery as d


class Delivery(unittest.TestCase):
    def setUp(self):
        scratch = d.BASE/'cpu/delivery-scratch'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        package, output = root/'package', root/'output'
        build = root/'build'
        build.mkdir()
        values = {}
        for name in ('previous', 'candidate'):
            directory = package/name
            directory.mkdir(parents=True)
            (directory/'q4t').write_text('#!/bin/sh\nprintf "Qwen4-Thor '+name+'\\n"\n')
            (directory/'q4t').chmod(0o700)
            cache = directory/'CMakeCache.txt'
            cache.write_text(name)
            (directory/'q4t.release.json').write_text(json.dumps(dict(
                binary_sha256=d.h.sha(directory/'q4t'), build_cache=str(cache),
                build_cache_sha256=d.h.sha(cache))))
            values[name] = d.hashes(directory)
        for name in d.NAMES:
            d.copy_file(package/'previous'/name, build/name)
        d.h.save(package/'pair.json', dict(old=values['previous'], new=values['candidate']))
        d.h.seal(package)
        patches = dict(ROOT=root, PACKAGE=package, OUTPUT=output,
                       MARKER=build/'vision-service-recovery.json',
                       OLD=values['previous'], NEW=values['candidate']['q4t'])
        for name, value in patches.items():
            p = patch.object(d, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(d, 'locked', contextlib.nullcontext)
        p.start()
        self.addCleanup(p.stop)

    def test_install_and_exact_pair_rollback(self):
        self.assertEqual(d.change()['status'], 'PASS_INSTALLED_PAIR')
        self.assertFalse(d.MARKER.exists())
        self.assertEqual(d.change(rollback=True)['status'], 'PASS_ROLLBACK')
        self.assertEqual(d.hashes(d.ROOT/'build'), d.OLD)

    def test_second_replace_failure_restores_pair_and_keeps_recovery(self):
        replace = d.h.replace_checked
        failed = False
        def fail_once(source, destination, expected):
            nonlocal failed
            if destination.name == 'q4t.release.json' and not failed:
                failed = True
                raise OSError('injected second replace failure')
            return replace(source, destination, expected)
        with patch.object(d.h, 'replace_checked', fail_once):
            with self.assertRaises(OSError):
                d.change()
        self.assertEqual(d.hashes(d.ROOT/'build'), d.OLD)
        self.assertTrue(d.MARKER.exists())
        self.assertEqual(d.change(rollback=True)['status'], 'PASS_ROLLBACK')
        self.assertFalse(d.MARKER.exists())

    def test_unknown_installed_pair_is_not_overwritten(self):
        path = d.ROOT/'build/q4t'
        path.write_bytes(b'foreign')
        with self.assertRaisesRegex(RuntimeError, 'unknown installed identity'):
            d.change()
        self.assertEqual(path.read_bytes(), b'foreign')
        self.assertFalse(d.MARKER.exists())

    def test_foreign_recovery_is_not_removed(self):
        marker = d.ROOT/'build/other-recovery.json'
        marker.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'foreign recovery'):
            d.change()
        self.assertEqual(d.hashes(d.ROOT/'build'), d.OLD)
        self.assertEqual(marker.read_text(), '{}')


if __name__ == '__main__':
    unittest.main()
