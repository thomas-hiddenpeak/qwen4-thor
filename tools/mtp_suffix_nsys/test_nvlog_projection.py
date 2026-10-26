"""Ten bounded CPU contracts for the optional, explicitly lossy ACK view."""
import copy
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import nvlog_stream as m
import recorded_gpu
import test_nvlog_stream as raw_tests
from test_recorded_gpu import terminal_log, PID

ACK = (b'I17:23:41.664| quadd_verbose_daemon_evtsrc_trace_events |'
       b'                             nsys pid=3531384 tid= 3531390 |'
       b' Trace.cpp:2070 [SerializeEventImpl]: Trace[0xffff6810feb0]:'
       b' Serialized event with timestamp {1196465251945171, 1196465251945171}\n')


def semantic(value):
    if isinstance(value, dict):
        return {k: semantic(v) for k, v in value.items()
                if k not in ('line', 'byte_offset', 'raw_source')}
    if isinstance(value, list):
        return [semantic(v) for v in value]
    return value


class ProjectionContracts(unittest.TestCase):
    setUp = raw_tests.StreamContracts.setUp
    encode = raw_tests.StreamContracts.encode

    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ['Q4T_NVLOG_STREAM_BINARY']).resolve()

    def collector(self, **options):
        return raw_tests.StreamContracts.collector(
            self, projection=options.pop('projection', m.ACK_PROJECTION), **options)

    def decoded(self, collector, receipt):
        return b''.join(m.iter_chunks(collector.compressed_path, receipt=receipt))

    def test_01_exact_ack_byte_boundaries_and_attested_hashes(self):
        variants = [ACK, ACK.replace(b'1196465251945171', b'18446744073709551615'),
                    ACK.replace(b'17:23:41.664', b'00:00:00.000')]
        kept = b'unknown \xff record\n'
        data = variants[0] + kept + b''.join(variants[1:])
        collector = self.collector()
        collector.write_chunks(bytes([c]) for c in data)
        receipt = collector.finish()
        self.assertEqual(self.decoded(collector, receipt), kept)
        self.assertEqual(receipt['input_sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(receipt['discarded_sha256'], hashlib.sha256(b''.join(variants)).hexdigest())
        self.assertEqual(receipt['input_lines'], 4)
        self.assertEqual(receipt['discarded_lines'], 3)
        result = m.verify(collector.compressed_path, receipt)
        self.assertEqual(result['source_kind'], 'PROJECTED_NVLOG_VIEW')
        self.assertFalse(result['projection_evidence']['original_bytes_recoverable'])

    def test_02_changed_and_malformed_records_preserved_byte_exact(self):
        variants = [b'E'+ACK[1:], b'W'+ACK[1:], b'F'+ACK[1:],
            ACK.replace(b'17:23:41.664', b'25:23:41.664'),
            ACK.replace(b'17:23:41.664', b'17:60:41.664'),
            ACK.replace(b'17:23:41.664', b'17:23:60.664'),
            ACK.replace(b'17:23:41.664', b'17:23:41.66x'),
            ACK.replace(b'1196465251945171', b'18446744073709551616'),
            ACK.replace(b'1196465251945171', b'-1'),
            ACK.replace(b'0xffff6810feb0', b'0x10000000000000000'),
            ACK.replace(b'tid= ', b'tid='),
            ACK.replace(b'trace_events |', b'trace_event_details |'),
            ACK.replace(b'2070', b'2071'), ACK.replace(b'SerializeEventImpl', b'ReadStorageImpl'),
            ACK[:-1]+b' lost events\n', ACK[:-1]+b'\r\n', b'prefix '+ACK,
            ACK.replace(b'nsys pid=', b'target pid='), ACK.replace(b'{119', b'{+119')]
        data = b''.join(variants)
        collector, receipt = self.encode(data)
        self.assertEqual(self.decoded(collector, receipt), data)
        self.assertEqual(receipt['discarded_lines'], 0)

    def test_03_terminal_filter_error_and_loss_semantics_unchanged(self):
        kept = (terminal_log() +
                f'E01:00:00.004| unknown | target pid={PID} tid= {PID} | X:1 [X]: error\n' +
                'W01:00:00.005| other | lost events\n').encode()
        data = ACK.join(kept.splitlines(keepends=True)) + ACK
        collector, receipt = self.encode(data)
        self.assertEqual(self.decoded(collector, receipt), kept)
        before = recorded_gpu.terminal_evidence(data, PID)
        after = recorded_gpu.terminal_evidence_lines(
            m.iter_lines(collector.compressed_path, receipt=receipt), PID)
        self.assertEqual(semantic(before), semantic(after))
        self.assertTrue(after['late_filter_pair_observed'])
        self.assertTrue(after['qualification_errors'])
        self.assertEqual(len(after['target_errors']), 1)

    def test_04_original_input_cap_applies_even_to_discarded_bytes(self):
        collector, receipt = self.encode(ACK, logical_limit=len(ACK))
        self.assertEqual(receipt['logical_bytes'], 0)
        collector = self.collector(logical_limit=len(ACK)-1)
        with self.assertRaises((ValueError, BrokenPipeError)):
            collector.write_chunks([ACK])
            collector.finish()
        self.assertFalse(collector.complete)

    def test_05_physical_line_cap_and_unterminated_input_rejected(self):
        collector, receipt = self.encode(b'x'*(m.LINE_LIMIT-1)+b'\n')
        self.assertEqual(receipt['logical_bytes'], m.LINE_LIMIT)
        for data in (b'x'*m.LINE_LIMIT+b'\n', ACK[:-1]):
            with self.subTest(size=len(data)):
                collector = self.collector()
                with self.assertRaises((ValueError, BrokenPipeError)):
                    collector.write_chunks(data[i:i+m.BLOCK] for i in range(0, len(data), m.BLOCK))
                    collector.finish()
                self.assertFalse(collector.complete)

    def test_06_empty_and_all_discarded_do_not_certify_terminal_events(self):
        for data in (b'', ACK*1000):
            with self.subTest(size=len(data)):
                collector, receipt = self.encode(data)
                self.assertEqual(self.decoded(collector, receipt), b'')
                terminal = recorded_gpu.terminal_evidence_lines(
                    m.iter_lines(collector.compressed_path, receipt=receipt), PID)
                self.assertFalse(terminal['late_filter_pair_observed'])
                self.assertEqual(len(terminal['qualification_errors']), 3)

    def test_07_projection_receipt_counter_hash_mode_and_bounds_tampering(self):
        collector, receipt = self.encode(ACK+b'kept\n')
        changes = [{'projection': 'unknown'}, {'input_lines': True}, {'input_bytes': 0},
                   {'discarded_lines': 3}, {'input_sha256': ''}, {'logical_limit': m.LOGICAL_LIMIT+1},
                   {'controlled_workspace_bytes': (16<<20)+1}, {'status': 'INVALID'},
                   {'input_eof': False}, {'checksum': False}, {'level': 3},
                   {'discarded_bytes': -1}, {'logical_sha256': '0'*64}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                m.verify(collector.compressed_path, receipt | change)
        self.assertIsNone(m.projection_evidence({'projection': 'none'}))
        self.assertIsNone(m.projection_evidence({}))

    def test_08_finish_rejects_mode_substitution(self):
        collector = self.collector()
        collector.write_chunks([ACK])
        original = m._json
        def changed(path):
            value = original(path)
            return value | {'projection': 'none'} if path == collector.receipt_path else value
        with patch.object(m, '_json', side_effect=changed), self.assertRaisesRegex(ValueError, 'requested mode'):
            collector.finish()
        self.assertFalse(collector.complete)
        with self.assertRaises(ValueError):
            self.collector(projection='alternative_filter')

    def test_09_raw_default_remains_lossless_and_legacy_receipts_work(self):
        collector, receipt = self.encode(ACK+b'unterminated', projection='none')
        self.assertEqual(self.decoded(collector, receipt), ACK+b'unterminated')
        legacy = copy.deepcopy(receipt)
        for key in ('projection', 'input_bytes', 'input_sha256', 'input_lines',
                    'retained_lines', 'discarded_bytes', 'discarded_lines', 'discarded_sha256'):
            legacy.pop(key)
        self.assertEqual(m.verify(collector.compressed_path, legacy)['source_kind'], 'LOSSLESS_NVLOG')

    def test_10_retained_output_capacity_failure_remains_failure(self):
        collector = self.collector(compressed_limit=64)
        with self.assertRaises((ValueError, BrokenPipeError)):
            collector.write_chunks([os.urandom(4096).hex().encode()+b'\n'])
            collector.finish()
        self.assertFalse(collector.complete)
        self.assertLessEqual(collector.compressed_path.stat().st_size, 64)
        with self.assertRaises(ValueError):
            m.verify(collector.compressed_path)
