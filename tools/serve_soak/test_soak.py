"""Real file-rotation and fail-closed duration/evidence gates; no GPU or HTTP."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from bounded_log import BoundedLog
from tools.serve_soak import evidence as e, stage
from tools.serve_service import package as pkg, service


class Case(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'build/serve-soak-tests'
        parent.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def copier(self, rotation=31, maximum=4096):
        source = self.root/'source.jsonl'
        copy = e.StreamCopy(source, self.root/'copy.jsonl',
                            rotation=rotation, maximum=maximum)
        self.addCleanup(self.close_copy, copy)
        return source, copy

    @staticmethod
    def close_copy(copy):
        if not copy.closed:
            try:
                copy.close()
            except (e.EvidenceAbort, OSError):
                pass

    def test_rotation_is_copied_byte_for_byte_including_split_utf8(self):
        source, reader = self.copier()
        writer = BoundedLog(source, 31)
        expected = b''
        reader.poll()  # Before the controller has created its log.
        for n in range(20):
            text = json.dumps(dict(n=n, text='中文'), ensure_ascii=False)+'\n'
            # Small writes ensure at most one unseen rotation between polls.
            for part in (text[:9], text[9:]):
                writer.write(part)
                expected += part.encode()
                reader.poll()
        original = {p.name:p.read_bytes() for p in self.root.glob('source*')}
        result = reader.close()
        self.assertEqual((self.root/'copy.jsonl').read_bytes(), expected)
        self.assertEqual(result['bytes'], len(expected))
        self.assertGreater(result['rotations'], 1)
        self.assertEqual(original, {p.name:p.read_bytes() for p in self.root.glob('source*')})

    def test_missed_rotations_are_not_silently_joined(self):
        source, reader = self.copier()
        writer = BoundedLog(source, 31)
        writer.write('x'*31); reader.poll()
        writer.write('y'*70)
        with self.assertRaisesRegex(e.EvidenceAbort, 'generations missing'):
            reader.poll()

    def test_initial_missing_prefix_is_rejected(self):
        source, reader = self.copier()
        BoundedLog(source, 31).write('a'*40)
        with self.assertRaisesRegex(e.EvidenceAbort, 'prefix unobserved'):
            reader.poll()

    def test_rotation_rename_gap_keeps_old_descriptor(self):
        source, reader = self.copier()
        source.write_bytes(b'a'*31); reader.poll()
        source.rename(source.with_name(source.name+'.previous'))
        reader.poll()
        source.write_bytes(b'b'*10); reader.poll()
        reader.close()
        self.assertEqual((self.root/'copy.jsonl').read_bytes(), b'a'*31+b'b'*10)

    def test_truncation_symlink_and_byte_limit_are_failures(self):
        source, reader = self.copier(maximum=32)
        source.write_bytes(b'a'*20); reader.poll()
        source.write_bytes(b'b')
        with self.assertRaisesRegex(e.EvidenceAbort, 'truncated'):
            reader.poll()
        second = self.root/'second.jsonl'
        other = e.StreamCopy(second, self.root/'second-copy.jsonl', rotation=31)
        self.addCleanup(self.close_copy, other)
        second.write_bytes(b'c'*20);other.poll()
        second.unlink(); second.symlink_to(self.root/'copy.jsonl')
        with self.assertRaisesRegex(e.EvidenceAbort, 'non-file'):
            other.poll()

    def test_archive_bound_closes_its_descriptors_on_failure(self):
        source, reader = self.copier(maximum=5)
        source.write_bytes(b'a'*6)
        with self.assertRaisesRegex(e.EvidenceAbort, 'byte limit'):
            reader.close()
        self.assertTrue(reader.closed)
        self.assertTrue(reader.output.closed)
        with self.assertRaises(OSError):
            os.fstat(reader.fd)

    def test_collector_surfaces_sampler_failure_and_closes_copies(self):
        source = self.root/'lease';source.mkdir()
        for name in ('events.jsonl', 'nvml-queries.jsonl'):
            (source/name).write_text('{}\n')
        failed = threading.Event()
        def sampler():
            raise OSError('CPU fixture observation loss')
        capture = e.Capture(source, self.root/'capture', failed.set, sampler=sampler)
        self.assertTrue(failed.wait(3))
        with self.assertRaises(e.EvidenceAbort):
            capture.check()
        with self.assertRaises(e.EvidenceAbort):
            capture.close()
        result = pkg.read(self.root/'capture/capture.json')
        self.assertFalse(result['passed'])
        self.assertEqual(result['resource_samples'], 0)
        self.assertTrue(all(log.closed for log in capture.logs.values()))

    def test_resource_envelope_and_first_hour_need_complete_samples(self):
        rows = [dict(rss=100, anon=200, fds=10, threads=8) for _ in range(240)]
        checkpoint = stage.hourly_checkpoint(240, 3600, rows)
        self.assertFalse(checkpoint['day_scale_stability_proved'])
        for cycle, elapsed, subset in ((239,3600,rows), (240,3599,rows),
                                       (240,3600,rows[:-1]), (240,4501,rows)):
            with self.assertRaises(RuntimeError):
                stage.hourly_checkpoint(cycle, elapsed, subset)
        for key, limit in e.ENVELOPE.items():
            changed = copy.deepcopy(rows)
            changed[-1][key] += limit+1
            with self.assertRaisesRegex(RuntimeError, 'envelope'):
                e.envelope(changed)

    def test_guard_archive_requires_full_lease_and_unbroken_evidence(self):
        path = self.root/'events.jsonl'
        rows = [dict(event='lease_acquired',monotonic=1,pid=42),
                dict(event='preflight',monotonic=2,pid=42,conflicts=[]),
                dict(event='monitor',monotonic=3,pid=42,conflicts=[]),
                dict(event='lease_released',monotonic=4,pid=42,status='COMPLETED')]
        def write(values):
            path.write_text(''.join(json.dumps(r)+'\n' for r in values))
        write(rows)
        self.assertEqual(e.guard_archive(path, {'status':'COMPLETED'})['observations'], 2)
        for changed in (rows[1:], rows[:-1], [*rows[:2], dict(rows[2], conflicts=['foreign']), rows[3]],
                        [*rows[:3], dict(rows[3], monotonic=20)]):
            write(changed)
            with self.assertRaises(e.EvidenceAbort):
                e.guard_archive(path, {'status':'COMPLETED'})
        write(rows)
        with self.assertRaises(e.EvidenceAbort):
            e.guard_archive(path, {'status':'ENV_ABORT'})

    def test_short_and_day_profiles_cannot_share_a_result_or_silently_expand(self):
        saved = {name:getattr(stage.a,name) for name in
                 ('BASE','PLAN','OUT','LIMITS','CONTROL_LIMIT','GPU_WINDOW')}
        try:
            stage.configure('short')
            short = (stage.OUT, stage.CYCLES, stage.SECONDS, stage.LIMITS.copy())
            stage.configure('day')
            self.assertNotEqual(stage.OUT, short[0])
            self.assertEqual(short[1:3], (8,120))
            self.assertEqual((stage.CYCLES,stage.SECONDS), (5760,86400))
            self.assertEqual(short[3]['generations'], 40)
            self.assertEqual(stage.LIMITS['generations'], 28800)
            with self.assertRaises(RuntimeError):
                stage.configure('unbounded')
        finally:
            stage.configure('short', apply=False)
            for name, value in saved.items():
                setattr(stage.a, name, value)

    def test_validation_unit_lifetime_is_bounded_without_installing_it(self):
        with mock.patch.object(pkg, 'verify', return_value=dict(python=dict(path='/usr/bin/python3'))):
            text = service.unit_text(self.root/'package', 'a'*64, self.root/'run')
        text = text.replace('Restart=no\n', 'Restart=no\nRuntimeMaxSec=600s\n')
        (self.root/'package/source/tools/serve_service').mkdir(parents=True)
        (self.root/'package/source/tools/serve_service/service.py').write_text('')
        unit = self.root/'soak-test.service';unit.write_text(text)
        result = subprocess.run(['systemd-analyze','--user','verify',str(unit)],
                                capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('[Install]',text)
        self.assertIn('Restart=no\n',text)


if __name__ == '__main__':
    unittest.main()
