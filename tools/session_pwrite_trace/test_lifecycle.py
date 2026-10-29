"""Fixed bounded CPU contracts; no new storage or model workload."""
import json
import os
from pathlib import Path
import random
import struct
import tempfile
import unittest

from lifecycle import Audit, ContractError, Files, Request, audit, records, union_ns

ROOT = Path(__file__).resolve().parents[2]


class LifecycleTest(unittest.TestCase):
    def test_fd_and_inode_reuse_get_distinct_generation(self):
        f = Files()
        f.open(1, 4, 10)
        self.assertEqual(f.get(1, 4), 10)
        f.close(1, 4)
        f.open(1, 4, 20)
        self.assertEqual(f.get(1, 4), 20)

    def test_process_isolation(self):
        f = Files()
        f.open(1, 4, 10)
        f.open(2, 4, 20)
        self.assertEqual((f.get(1, 4), f.get(2, 4)), (10, 20))

    def test_dup_survives_original_close(self):
        f = Files()
        f.open(1, 4, 10)
        f.dup(1, 4, 5)
        f.close(1, 4)
        self.assertEqual(f.get(1, 5), 10)

    def test_dup_replaces_destination(self):
        f = Files()
        f.open(1, 4, 10)
        f.open(1, 5, 20)
        f.dup(1, 4, 5)
        self.assertEqual(f.get(1, 5), 10)

    def test_failed_close_does_not_guess(self):
        f = Files()
        f.open(1, 4, 10)
        f.syscall(1, 11, 57, -9, [4, 0, 0])
        self.assertEqual(f.get(1, 4), 10)

    def test_unknown_fd_rejected(self):
        with self.assertRaisesRegex(ContractError, 'unknown'):
            Files().get(1, 4)

    def test_missed_close_rejected(self):
        f = Files()
        f.open(1, 4, 10)
        with self.assertRaises(ContractError): f.open(1, 4, 20)

    def test_close_range(self):
        f = Files()
        for fd in (3, 4, 5): f.open(1, fd, fd)
        f.syscall(1, 6, 436, 0, [3, 4, 0])
        self.assertEqual(f.fds, {(1, 5): 5})

    def test_cloexec_is_not_close(self):
        f = Files()
        f.open(1, 4, 10)
        f.syscall(1, 6, 436, 0, [3, 4, 4])
        self.assertEqual(f.get(1, 4), 10)

    def request(self):
        return Request(1, 1, 1, 1, 8192)

    def test_partial_completion(self):
        r = self.request()
        r.issue(10, 8192)
        r.complete(15, 4096, 0)
        self.assertFalse(r.done)
        self.assertEqual(r.remaining, 4096)
        r.complete(20, 4096, 0)
        self.assertTrue(r.done)
        self.assertEqual(r.intervals, [(10, 20)])

    def test_requeue_then_complete(self):
        r = self.request()
        r.insert_time = 5
        r.issue(10, 8192)
        r.requeue(12)
        r.insert_time = 14
        r.issue(18, 8192)
        r.complete(22, 8192, 0)
        self.assertEqual(r.queues, [(5, 10), (14, 18)])
        self.assertEqual(r.intervals, [(10, 12), (18, 22)])

    def test_partial_then_requeue(self):
        r = self.request()
        r.issue(10, 8192)
        r.complete(12, 4096, 0)
        r.requeue(13)
        r.issue(16, 4096)
        r.complete(20, 4096, 0)
        self.assertTrue(r.done)

    def test_bad_completion_sizes(self):
        for size in (0, -1, 8193):
            with self.subTest(size=size):
                r = self.request()
                r.issue(10, 8192)
                with self.assertRaises(ContractError): r.complete(20, size, 0)

    def test_completion_error(self):
        r = self.request()
        r.issue(10, 8192)
        with self.assertRaises(ContractError): r.complete(20, 8192, 1)

    def test_duplicate_issue_and_completion(self):
        r = self.request()
        r.issue(10, 8192)
        with self.assertRaises(ContractError): r.issue(11, 8192)
        r.complete(20, 8192, 0)
        with self.assertRaises(ContractError): r.complete(21, 8192, 0)

    def test_completion_before_issue(self):
        r = self.request()
        with self.assertRaises(ContractError): r.complete(20, 8192, 0)

    def test_reversed_time(self):
        r = self.request()
        r.issue(10, 8192)
        with self.assertRaises(ContractError): r.complete(9, 8192, 0)

    def test_merge_refuses_guessed_survivor(self):
        with self.assertRaisesRegex(ContractError, 'survivor unknown'):
            self.request().merge()

    def test_union_against_independent_discrete_reference(self):
        randomizer = random.Random(20261029)
        for _ in range(100):
            intervals = [sorted(randomizer.sample(range(30), 2)) for _ in range(12)]
            expected = len({t for a, b in intervals for t in range(a, b)
                            if 4 <= t < 24})
            self.assertEqual(union_ns(intervals, 4, 24), expected)
            self.assertEqual(union_ns(list(reversed(intervals)), 4, 24), expected)

    def test_truncated_record(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.q4t-work') as temp:
            p = Path(temp)/'record'
            p.write_bytes(bytes(127))
            with self.assertRaises(ContractError): list(records(p))

    def test_new_raw_fixture(self):
        path = Path(os.environ['Q4T_TRACE_FIXTURE'])
        result = audit(path)
        fixture = json.loads((path/'fixture.json').read_text())
        self.assertTrue(result['passed'])
        self.assertEqual(len(result['calls']), len(fixture['calls']))
        self.assertEqual(sum(c['written'] for c in result['calls']), fixture['writes'])
        self.assertEqual(len({c['file_generation'] for c in result['calls']}), 2)
        for actual, expected in zip(result['calls'], fixture['calls']):
            self.assertEqual(actual['offset'], expected['offset'])
            self.assertEqual(actual['written'], expected['returned'])
            self.assertLessEqual(expected['start'], actual['start_ns'])
            self.assertGreaterEqual(expected['end'], actual['end_ns'])

    def test_deleted_completion_fails(self):
        path = Path(os.environ['Q4T_TRACE_FIXTURE'])
        a = Audit()
        deleted = False
        with self.assertRaises(ContractError):
            for e in records(path/'events.bin'):
                if e['kind'] == 12 and not deleted:
                    deleted = True
                    continue
                a.consume(e)
            a.finish()

    def test_corrupt_cid_fails(self):
        path = Path(os.environ['Q4T_TRACE_FIXTURE'])
        a = Audit()
        changed = False
        with self.assertRaisesRegex(ContractError, 'CID mismatch'):
            for e in records(path/'events.bin'):
                if e['kind'] == 30 and not changed:
                    payload = bytearray(struct.pack('<12Q', *e['d']))
                    payload[44] ^= 1
                    e['d'] = list(struct.unpack('<12Q', payload))
                    changed = True
                a.consume(e)

    def test_same_raw_request_address_reused(self):
        path = Path(os.environ['Q4T_TRACE_FIXTURE'])
        seen = {}
        for e in records(path/'events.bin'):
            if e['kind'] == 20:
                seen.setdefault(e['key'], set()).add(e['d'][0])
        self.assertTrue(any(len(generations) > 1 for generations in seen.values()))
        self.assertTrue(audit(path)['passed'])

    def request_stream(self, generation, start, cid=7):
        def event(kind, ts):
            return dict(ts=ts, task=1, key=99, kind=kind, cpu=0,
                        d=[generation, 0, 0, 4096, 0, 1, 0, 0, 0, 0, 0, 0])
        setup = event(20, start)
        setup['d'][7] = cid << 16
        normal = event(30, start+1)
        payload = bytearray(96)
        struct.pack_into('<ii', payload, 32, 0, 5)
        struct.pack_into('<H', payload, 44, cid)
        normal['d'] = list(struct.unpack('<12Q', payload))
        complete = event(12, start+5)
        complete['d'][7] = 4096
        return [setup, normal, event(11, start+2), event(22, start+3),
                event(21, start+4), complete]

    def test_cid_reuse_after_completion(self):
        a = Audit()
        for e in self.request_stream(1, 10)+self.request_stream(2, 20):
            a.consume(e)
        self.assertEqual(a.finish()['completed_requests'], 2)

    def test_cid_reuse_while_live_rejected(self):
        a = Audit()
        for e in self.request_stream(1, 10)[:2]: a.consume(e)
        with self.assertRaisesRegex(ContractError, 'CID reused'):
            for e in self.request_stream(2, 20)[:2]:
                e['key'] = 100
                a.consume(e)

    def test_interleaved_ring_order_uses_request_generation(self):
        a = Audit()
        first = self.request_stream(1, 10, 7)
        second = self.request_stream(2, 11, 8)
        for e in second: e['key'] = 100
        for e in first[:3]+second[:3]+second[3:]+first[3:]: a.consume(e)
        result = a.finish()
        self.assertEqual(result['completed_requests'], 2)
        self.assertGreater(result['timestamp_inversions'], 0)

    def test_original_capture_salvage_is_unchanged(self):
        from review_capture import review
        old = ROOT/'.q4t-work/session-pwrite-trace-20261029/fixture-02'
        result = review(old)
        self.assertEqual(json.loads(json.dumps(result)),
                         json.loads((old/'offline-review.json').read_text()))


if __name__ == '__main__':
    unittest.main()
