"""Targeted checks for failures observed in the bounded HTTP capture."""
import io
import json
from pathlib import Path
import tempfile
import unittest

from finalize import finalize, flush_pending
from lifecycle import Audit, ContractError, Request, records

ROOT = Path(__file__).resolve().parents[2]


class FailurePathTest(unittest.TestCase):
    def test_failed_capture_keeps_tail_maps_and_failure(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.q4t-work') as name:
            out = Path(name)
            (out/'events.bin').write_bytes(b'A'*128)
            pending = bytearray(b'B'*128)
            actions = []
            result = dict(status='FAILED', error='injected stack failure')
            def detach(): actions.append('detach')
            def drain():
                actions.append('drain')
                pending.extend(b'C'*128)
            def snapshot():
                actions.append('snapshot')
                (out/'stacks.json').write_text('{"7": [123]}')
                return dict(events=3, counters=[1, 3, 0, 0, 1, 0, 0])
            finalize(out, pending, result, detach, drain, snapshot, lambda _: None)
            self.assertEqual(actions, ['detach', 'drain', 'snapshot'])
            self.assertEqual((out/'events.bin').read_bytes(),
                             b'A'*128+b'B'*128+b'C'*128)
            self.assertEqual(result['status'], 'FAILED')
            self.assertEqual(result['persisted_events'], 3)
            self.assertEqual(result['pending_bytes'], 0)
            self.assertEqual(json.loads((out/'stacks.json').read_text()), {'7': [123]})

    def test_sync_failure_does_not_duplicate_written_bytes(self):
        stream = io.BytesIO()
        pending = bytearray(b'A'*128)
        def bad_sync(_): raise OSError('injected sync failure')
        with self.assertRaises(OSError): flush_pending(stream, pending, bad_sync)
        self.assertEqual(pending, b'')
        flush_pending(stream, pending, lambda _: None)
        self.assertEqual(stream.getvalue(), b'A'*128)

    def test_drain_failure_still_preserves_existing_tail(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.q4t-work') as name:
            out = Path(name)
            pending = bytearray(b'X'*128)
            result = dict(status='COLLECTED_REQUIRES_AUDIT')
            def fail(): raise OSError('injected drain failure')
            finalize(out, pending, result, lambda: None, fail,
                     lambda: dict(events=1), lambda _: None)
            self.assertEqual(result['status'], 'FAILED')
            self.assertEqual(result['persisted_events'], 1)
            self.assertIn('drain', result['finalization_errors'][0])

    def test_zero_byte_flush_is_valid_but_zero_write_is_not(self):
        flush = Request(1, 0, 0, 2, 0)
        flush.issue(10, 0)
        flush.complete(20, 0, 0)
        self.assertTrue(flush.done)
        write = Request(2, 0, 0, 1, 4096)
        write.issue(10, 4096)
        with self.assertRaises(ContractError): write.complete(20, 0, 0)

    def test_failed_http_prefix_keeps_unknown_wakeup_and_closed_io(self):
        path = ROOT/'.q4t-work/session-pwrite-repair-20261029/http-stage/trace/events.bin'
        audit = Audit()
        for event in records(path): audit.consume(event)
        result = audit.finish()
        self.assertEqual(len(result['calls']), 4320)
        self.assertEqual(result['completed_requests'], 209818)
        unknown = [c for c in result['calls'] if c['missing_wake_intervals']]
        self.assertTrue(unknown)
        self.assertTrue(all(c['wake_union_ns'] is None for c in unknown))
        self.assertEqual(result['background_unknown_setups'], 1)
        self.assertEqual(sum(result['event_counts'].values()), 1794997)


if __name__ == '__main__':
    unittest.main()
