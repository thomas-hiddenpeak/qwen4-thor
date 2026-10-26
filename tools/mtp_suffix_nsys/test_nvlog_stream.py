"""Sixteen CPU methods. The owner supplies the already-built native binary.

No profiler, CUDA, HTTP, sensor queries, or real-log replay is run by this suite.
The separately frozen 746MB roundtrip is not silently included or repeated.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import nvlog_stream as m


class StreamContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ['Q4T_NVLOG_STREAM_BINARY']).resolve()
        if not cls.binary.is_file():
            raise ValueError('CPU owner must supply the frozen native binary')

    def setUp(self):
        root = Path(__file__).resolve().parents[2]/'.q4t-work'
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='nvlog-stream-cpu-', dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.counter = 0

    def collector(self, **options):
        self.counter += 1
        collector = m.Collector(self.binary, self.root/str(self.counter),
                                timeout_seconds=options.pop('timeout_seconds', 10), **options)
        self.addCleanup(collector.abort)
        return collector.start()

    def encode(self, data, **options):
        collector = self.collector(**options)
        collector.write_chunks(data[i:i+m.BLOCK] for i in range(0, len(data), m.BLOCK))
        receipt = collector.finish()
        return collector, receipt

    def external(self, collector, program, *args):
        child = subprocess.Popen([sys.executable, '-B', '-c', program,
                                  str(collector.fifo_path), *map(str, args)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def cleanup():
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
            child.stderr.close()
        self.addCleanup(cleanup)
        return child

    def copied(self, collector, suffix, data):
        path = collector.directory/suffix
        path.write_bytes(data)
        return path

    def test_01_empty_and_multiblock_exact_sha_caps_and_receipt(self):
        for data in (b'', bytes(range(256))*1400):
            with self.subTest(size=len(data)):
                collector, receipt = self.encode(data)
                result = m.verify(collector.compressed_path, receipt)
                self.assertEqual(result['logical_sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual(result['logical_bytes'], len(data))
                self.assertLessEqual(receipt['controlled_workspace_bytes'], 16 << 20)
                self.assertEqual(receipt['address_space_limit_bytes'], 64 << 20)
                self.assertGreater(receipt['maxrss_kib'], 0)
                self.assertEqual(receipt['workers'], 0)
                self.assertFalse(collector.compressed_path.stat().st_mode & 0o222)

    def test_02_external_writers_close_and_reopen_without_early_eof(self):
        collector = self.collector()
        program = 'import os,sys; f=os.open(sys.argv[1],os.O_WRONLY); os.write(f,sys.argv[2].encode()); os.close(f)'
        for text in ('first\n', 'second\n', 'third\n'):
            child = self.external(collector, program, text)
            self.assertEqual(child.wait(timeout=2), 0)
            time.sleep(.02)
            collector.check()
        receipt = collector.finish()
        self.assertEqual(b''.join(m.iter_chunks(collector.compressed_path, receipt=receipt)),
                         b'first\nsecond\nthird\n')

    def test_03_concurrent_external_writers_complete_atomic_records(self):
        collector = self.collector()
        program = ('import os,sys; f=os.open(sys.argv[1],os.O_WRONLY); '
                   '[(os.write(f,(sys.argv[2]+":"+str(i)+"\\n").encode())) for i in range(100)]; os.close(f)')
        children = [self.external(collector, program, label) for label in ('A', 'B', 'C')]
        for child in children:
            self.assertEqual(child.wait(timeout=2), 0)
        receipt = collector.finish()
        actual = list(m.iter_lines(collector.compressed_path, receipt=receipt))
        expected = [f'{label}:{i}\n'.encode() for label in ('A', 'B', 'C') for i in range(100)]
        self.assertCountEqual(actual, expected)

    def test_04_byte_boundaries_utf8_and_all_error_lines_preserved(self):
        data = b'a'*(m.BLOCK-1)+b'\n'+('W 中文\nE failure\nF terminal\n'*300).encode()
        collector, receipt = self.encode(data)
        self.assertEqual(b''.join(m.iter_lines(collector.compressed_path, receipt=receipt)), data)
        self.assertEqual(m.verify(collector.compressed_path, receipt)['logical_bytes'], len(data))

    def test_05_logical_cap_exact_and_excess_refuse_completion(self):
        collector, receipt = self.encode(b'x'*100, logical_limit=100)
        self.assertEqual(m.verify(collector.compressed_path, receipt)['logical_bytes'], 100)
        collector = self.collector(logical_limit=100)
        with self.assertRaises((ValueError, BrokenPipeError)):
            collector.write_chunks([b'x'*101])
            collector.finish()
        self.assertNotEqual(collector.child.returncode, 0)
        if collector.receipt_path.exists():
            self.assertEqual(json.loads(collector.receipt_path.read_text())['status'], 'INVALID')

    def test_06_compressed_cap_is_not_truncated_valid_frame(self):
        collector = self.collector(compressed_limit=64)
        with self.assertRaises((ValueError, BrokenPipeError)):
            collector.write_chunks([os.urandom(4096)])
            collector.finish()
        self.assertNotEqual(collector.child.returncode, 0)
        self.assertLessEqual(collector.compressed_path.stat().st_size, 64)
        with self.assertRaises(ValueError):
            m.verify(collector.compressed_path)

    def test_07_finish_requires_eof_from_still_open_external_writer(self):
        collector = self.collector()
        fd = os.open(collector.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, b'held\n')
            with self.assertRaises(subprocess.TimeoutExpired):
                collector.finish(timeout_seconds=.05)
            self.assertIsNotNone(collector.child.returncode)
            self.assertFalse(collector.complete)
        finally:
            os.close(fd)

    def test_08_control_eof_without_finish_is_failure(self):
        collector = self.collector()
        collector.write_chunks([b'not authorized to finish\n'])
        os.close(collector.control)
        collector.control = None
        self.assertNotEqual(collector.child.wait(timeout=2), 0)
        receipt = json.loads(collector.receipt_path.read_text())
        self.assertEqual(receipt['status'], 'INVALID')
        self.assertFalse(receipt['frame_completed'])

    def test_09_fifo_inode_replacement_rejected(self):
        collector = self.collector()
        collector.fifo_path.rename(collector.directory/'old.fifo')
        os.mkfifo(collector.fifo_path)
        with self.assertRaises(ValueError):
            collector.check()
        self.assertNotEqual(collector.child.wait(timeout=2), 0)

    def test_10_native_exclusive_output_refuses_symlink(self):
        directory = self.root/'raw'
        directory.mkdir()
        fifo = directory/'input'
        os.mkfifo(fifo)
        identity = fifo.stat()
        victim = directory/'victim'
        victim.write_text('retain')
        output = directory/'compressed'
        output.symlink_to(victim)
        reader, writer = os.pipe()
        try:
            command = [str(self.binary), '--fifo', str(fifo), '--output', str(output),
                '--ready', str(directory/'ready'), '--receipt', str(directory/'receipt'),
                '--fifo-device', str(identity.st_dev), '--fifo-inode', str(identity.st_ino),
                '--control-fd', str(reader), '--logical-limit', '100',
                '--compressed-limit', '100', '--timeout-seconds', '2']
            result = subprocess.run(command, pass_fds=(reader,), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=3)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(victim.read_text(), 'retain')
        finally:
            os.close(reader)
            os.close(writer)

    def test_11_truncated_and_checksum_corruption_rejected(self):
        collector, _ = self.encode(b'known work\n'*1000)
        packed = collector.compressed_path.read_bytes()
        for index, data in enumerate((packed[:-1], packed[:-4], packed[:-1]+bytes([packed[-1]^1]))):
            with self.subTest(index=index):
                path = self.copied(collector, 'bad'+str(index), data)
                with self.assertRaises(ValueError):
                    m.verify(path)

    def test_12_extra_frame_and_trailing_garbage_rejected(self):
        collector, _ = self.encode(b'one frame\n')
        packed = collector.compressed_path.read_bytes()
        for index, data in enumerate((packed+packed, packed+b'garbage')):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    m.verify(self.copied(collector, 'extra'+str(index), data))

    def test_13_receipt_hash_bytes_identity_and_status_are_required(self):
        collector, receipt = self.encode(b'bind this exact file\n')
        changes = ({'logical_sha256': '0'*64}, {'logical_bytes': 99},
                   {'output_inode': receipt['output_inode']+1}, {'status': 'INVALID'})
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    m.verify(collector.compressed_path, receipt | change)
        path = self.copied(collector, 'identical-copy', collector.compressed_path.read_bytes())
        with self.assertRaises(ValueError):
            m.verify(path, receipt)

    def test_14_line_length_and_incomplete_line_rejected_without_truncation(self):
        for data in (b'x'*100+b'\n', b'unterminated'):
            with self.subTest(data=data):
                collector, receipt = self.encode(data)
                self.assertEqual(m.verify(collector.compressed_path, receipt)['logical_bytes'], len(data))
                with self.assertRaises(ValueError):
                    list(m.iter_lines(collector.compressed_path, receipt=receipt, max_line=100))

    def test_15_owned_worker_failure_and_once_only_operations(self):
        collector = self.collector()
        collector.child.terminate()
        collector.child.wait(timeout=2)
        with self.assertRaises(ValueError):
            collector.write_chunks([b'cannot silently continue'])
        collector.abort()
        self.assertFalse(collector.complete)
        collector, _ = self.encode(b'normal\n')
        with self.assertRaises(ValueError):
            collector.finish()
        with self.assertRaises(ValueError):
            collector.start()
        self.assertEqual(collector.child.returncode, 0)

    def test_16_fixed_deadline_decoder_caps_and_no_limit_increases(self):
        with self.assertRaises(ValueError):
            m.Collector(self.binary, self.root/'bad', logical_limit=m.LOGICAL_LIMIT+1)
        collector, receipt = self.encode(b'x'*1000)
        with self.assertRaises(ValueError):
            m.verify(collector.compressed_path, receipt, logical_limit=999)
        with self.assertRaises(ValueError):
            m.verify(collector.compressed_path, receipt, compressed_limit=1)
        collector = self.collector(timeout_seconds=1)
        self.assertNotEqual(collector.child.wait(timeout=2), 0)
        receipt = json.loads(collector.receipt_path.read_text())
        self.assertEqual(receipt['status'], 'INVALID')
        self.assertIn('deadline', receipt['error'])


if __name__ == '__main__':
    unittest.main()
