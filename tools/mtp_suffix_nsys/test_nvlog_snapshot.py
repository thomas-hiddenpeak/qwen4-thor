"""CPU-only fixed-file contracts; no process, collector, GPU or sensor access."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('nvlog_snapshot_under_test',
    Path(__file__).with_name('nvlog_snapshot.py'))
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class SnapshotContracts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='nvlog snapshot ')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source, self.copy = self.root / 'nvlog.log', self.root / 'prefix.log'
        self.initial = (b'I00:00:00.001| Injection | fixture pid=123 tid=123 | '
                        b'ReportCUDADone: DONE.\n')
        self.source.write_bytes(self.initial)

    def test_independent_readonly_copy_and_final_identity(self):
        result = module.snapshot(self.source, self.copy)
        self.assertEqual(self.copy.read_bytes(), self.initial)
        self.assertNotEqual(self.source.stat().st_ino, self.copy.stat().st_ino)
        self.assertEqual(self.copy.stat().st_mode & 0o222, 0)
        self.assertEqual(result['captured_sha256'], hashlib.sha256(self.initial).hexdigest())
        final = module.reconcile(result, self.source, 123)
        self.assertTrue(final['target_records_equal'])
        self.assertEqual(final['appended_bytes'], 0)
        self.assertFalse(final['trace_complete'])
        self.assertFalse(final['warning_waiver'])

    def test_tail_append_during_snapshot_is_preserved_separately(self):
        original, calls = module._prefix, []
        tail = b'E00:00:00.002| nsys pid=456 tid=456 | storage unavailable\nNo such file\n'
        def append(*args, **kwargs):
            result = original(*args, **kwargs)
            if not calls:
                with self.source.open('ab') as stream:
                    stream.write(tail)
            calls.append(True)
            return result
        with patch.object(module, '_prefix', side_effect=append):
            receipt = module.snapshot(self.source, self.copy)
        self.assertEqual(self.copy.read_bytes(), self.initial)
        final = module.reconcile(receipt, self.source, 123)
        self.assertTrue(final['target_records_equal'])
        self.assertEqual(final['appended_bytes'], len(tail))
        self.assertEqual(len(final['appended_errors']), 2)
        self.assertIn('No such file', final['appended_errors'][1]['text'])

    def test_prefix_rewrite_or_truncation_rejected_and_own_partial_removed(self):
        original = module._prefix
        for kind in ('rewrite', 'truncate'):
            with self.subTest(kind=kind):
                self.source.write_bytes(self.initial)
                calls = []
                def mutate(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if not calls:
                        self.source.write_bytes(b'X' * len(self.initial) if kind == 'rewrite' else b'X')
                    calls.append(True)
                    return result
                with patch.object(module, '_prefix', side_effect=mutate):
                    with self.assertRaises(ValueError):
                        module.snapshot(self.source, self.copy)
                self.assertFalse(self.copy.exists())

    def test_wrong_types_and_ambiguous_paths_fail_without_blocking(self):
        for kind in ('symlink', 'fifo', 'directory'):
            with self.subTest(kind=kind):
                path = self.root / kind
                if kind == 'symlink': path.symlink_to(self.source)
                elif kind == 'fifo': os.mkfifo(path)
                else: path.mkdir()
                with self.assertRaises(ValueError):
                    module.snapshot(path, self.copy)
        with self.assertRaises(ValueError):
            module.snapshot(self.source, self.source)
        linked = self.root / 'linked-parent'
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            module.snapshot(linked / 'nvlog.log', self.copy)

    def test_duplicate_destination_does_not_overwrite_or_remove(self):
        for kind in ('file', 'symlink', 'fifo'):
            with self.subTest(kind=kind):
                if kind == 'file': self.copy.write_bytes(b'keep')
                elif kind == 'symlink': self.copy.symlink_to(self.source)
                else: os.mkfifo(self.copy)
                with self.assertRaises(FileExistsError):
                    module.snapshot(self.source, self.copy)
                self.assertTrue(self.copy.exists())
                if kind == 'file': self.assertEqual(self.copy.read_bytes(), b'keep')
                self.copy.unlink()
        self.assertEqual(self.source.read_bytes(), self.initial)

    def test_added_target_loss_errors_and_boundary_completion_remain_explicit(self):
        self.source.write_bytes(self.initial + b'I00:00:00.002| fixture pid=123 ')
        receipt = module.snapshot(self.source, self.copy)
        tail = (b'tid=123 | NVTXFinish\n'
                b'E00:00:00.003| fixture pid=123 tid=123 | CUPTI dropped 2\n'
                b'W00:00:00.004| nsys pid=456 tid=456 | warning remains\n')
        with self.source.open('ab') as stream: stream.write(tail)
        result = module.reconcile(receipt, self.source, 123)
        self.assertTrue(result['snapshot_has_partial_last_line'])
        self.assertFalse(result['target_records_equal'])
        self.assertEqual(len(result['appended_loss']), 1)
        self.assertEqual(len(result['appended_errors']), 1)
        self.assertEqual(len(result['appended_warnings']), 1)
        self.assertEqual(len(result['final_target']['nvtx_terminal']), 1)
        self.assertEqual(result['qualification'], 'NOT_EVALUATED')

    def test_reconcile_rejects_tampered_copy_prefix_or_source_identity(self):
        for kind in ('copy', 'prefix', 'identity', 'other-path'):
            with self.subTest(kind=kind):
                self.source.write_bytes(self.initial)
                receipt = module.snapshot(self.source, self.copy)
                path = self.source
                if kind == 'copy':
                    os.chmod(self.copy, 0o600)
                    self.copy.write_bytes(b'X' * len(self.initial))
                    os.chmod(self.copy, 0o444)
                elif kind == 'prefix':
                    before = self.source.stat()
                    self.source.write_bytes(b'X' * len(self.initial))
                    os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
                elif kind == 'identity':
                    self.source.rename(self.root / 'old-source')
                    self.source.write_bytes(self.initial)
                else:
                    path = self.root / 'other-source'
                    path.write_bytes(self.initial)
                with self.assertRaises(ValueError):
                    module.reconcile(receipt, path, 123)
                self.copy.unlink()
        self.source.write_bytes(self.initial)
        receipt = module.snapshot(self.source, self.copy)
        original = module._prefix
        def final_grows(fd, length, **kwargs):
            result = original(fd, length, **kwargs)
            if os.fstat(fd).st_ino == self.source.stat().st_ino:
                with self.source.open('ab') as stream:
                    stream.write(b'late final append\n')
            return result
        with patch.object(module, '_prefix', side_effect=final_grows):
            with self.assertRaises(ValueError):
                module.reconcile(receipt, self.source, 123)

    def test_capacity_and_replaced_path_rejected_without_foreign_cleanup(self):
        for maximum in (True, 0, -1, module.MAX_BYTES + 1):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                module.snapshot(self.source, self.copy, max_bytes=maximum)
        with self.assertRaises(ValueError):
            module.snapshot(self.source, self.copy, max_bytes=1)
        original, calls = module._prefix, []
        def replace(*args, **kwargs):
            result = original(*args, **kwargs)
            if not calls:
                self.source.rename(self.root / 'pinned-original')
                self.source.write_bytes(self.initial)
                self.copy.rename(self.root / 'our-partial')
                self.copy.write_bytes(b'foreign replacement')
            calls.append(True)
            return result
        with patch.object(module, '_prefix', side_effect=replace):
            with self.assertRaises(ValueError):
                module.snapshot(self.source, self.copy)
        self.assertEqual(self.copy.read_bytes(), b'foreign replacement')


if __name__ == '__main__':
    unittest.main(verbosity=2)
