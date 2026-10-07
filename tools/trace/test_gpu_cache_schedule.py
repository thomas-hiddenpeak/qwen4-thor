"""Bounded host contracts for gpu_cache_schedule; build is external."""

import argparse
import json
from pathlib import Path
import random
import struct
import subprocess
import unittest


HELPER = None
SORT_HELPER = None
COUNTERS = {
    'csr_visits', 'reset_rows', 'bucket_adds', 'bucket_removes',
    'bucket_summary_checks', 'bucket_word_clears', 'selections',
    'work_budget', 'work_used', 'metadata_payload_bytes',
}


def packet(rows, *, experts=512, capacity=256, policy=0):
    topk = len(rows[0])
    flat = [expert for row in rows for expert in row]
    return (struct.pack('<5I', experts, capacity, topk, len(rows), policy)
            + struct.pack('<' + str(len(flat)) + 'H', *flat))


def literal_runtime_legacy(rows, order, capacity):
    """Independent transcription of moe.cu's mark/rollback/reprobe loop."""
    in_set = set()
    current, chunks = [], []
    distinct = 0
    for token in order:
        fresh = []
        for expert in rows[token]:
            if expert not in in_set:
                in_set.add(expert)
                fresh.append(expert)
        if distinct + len(fresh) > capacity:
            for expert in fresh:
                in_set.remove(expert)
            if current:
                chunks.append(current)
                current, in_set, distinct = [], set(), 0
            fresh = []
            for expert in rows[token]:
                if expert not in in_set:
                    in_set.add(expert)
                    fresh.append(expert)
            distinct += len(fresh)
        else:
            distinct += len(fresh)
        current.append(token)
    if current:
        chunks.append(current)
    return chunks


def independent_min_new(rows, order, capacity):
    """Small set reference: no CSR, buckets or incremental score updates."""
    remaining = list(order)
    chunks = []
    while remaining:
        needed, chunk = set(), []
        while remaining:
            token = min(remaining, key=lambda row: len(set(rows[row]) - needed))
            if len(needed | set(rows[token])) > capacity:
                break
            chunk.append(token)
            needed.update(rows[token])
            remaining.remove(token)
        if not chunk:
            raise AssertionError('reference made no progress')
        chunks.append(chunk)
    return chunks


class GPUCacheScheduleContracts(unittest.TestCase):
    def schedule(self, rows, *, experts=512, capacity=256, policy=0):
        result = subprocess.run(
            [str(HELPER)], input=packet(rows, experts=experts,
                                       capacity=capacity, policy=policy),
            capture_output=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stderr, b'')
        value = json.loads(result.stdout)
        self.assertEqual(set(value), {
            'schema', 'token_order', 'chunks', 'partition_applied',
            'fallback', 'counters',
        })
        self.assertEqual(value['schema'], 1)
        self.assertEqual(type(value['partition_applied']), bool)
        self.assertEqual(type(value['fallback']), bool)
        order = value['token_order']
        self.assertEqual(sorted(order), list(range(len(rows))))
        keys = [tuple(sorted(rows[token])) for token in order]
        self.assertEqual(keys, sorted(keys))
        chunks = value['chunks']
        self.assertTrue(chunks)
        self.assertEqual(sorted(t for chunk in chunks for t in chunk),
                         list(range(len(rows))))
        for chunk in chunks:
            self.assertTrue(chunk)
            self.assertLessEqual(len({e for t in chunk for e in rows[t]}),
                                 capacity)
        counters = value['counters']
        self.assertEqual(set(counters), COUNTERS)
        for count in counters.values():
            self.assertIs(type(count), int)
            self.assertGreaterEqual(count, 0)
        self.assertEqual(counters['work_used'],
                         counters['csr_visits'] + counters['reset_rows'])
        self.assertLessEqual(counters['work_used'], counters['work_budget'])
        return value

    def test_legacy_reprobes_shared_experts_after_full_flush(self):
        rows = [[0, 1], [0, 2], [1, 3], [2, 3], [3, 4]]
        value = self.schedule(rows, experts=5, capacity=3)
        self.assertEqual(value['chunks'], [[0, 1], [2, 3], [4]])
        self.assertEqual(value['chunks'], literal_runtime_legacy(
            rows, value['token_order'], 3))
        self.assertFalse(value['partition_applied'])
        self.assertFalse(value['fallback'])
        self.assertEqual(set(value['counters'].values()), {0})

    def test_legacy_fixed_bounded_random_reference(self):
        rng = random.Random(219)
        for sample in range(16):
            rows = [rng.sample(range(19), 3) for _ in range(23)]
            with self.subTest(sample=sample):
                value = self.schedule(rows, experts=19, capacity=7)
                self.assertEqual(value['chunks'], literal_runtime_legacy(
                    rows, value['token_order'], 7))

    def test_equal_keys_use_existing_cpp_sort_not_stable_row_order(self):
        rows = [list(range(t % 10, 10)) + list(range(t % 10))
                for t in range(33)]
        value = self.schedule(rows)
        payload = packet(rows)[20:]
        oracle = subprocess.run(
            [str(SORT_HELPER)], input=struct.pack('<2I', len(rows), 10)
            + payload, capture_output=True, timeout=30, check=False)
        self.assertEqual(oracle.returncode, 0, oracle.stderr.decode())
        self.assertEqual(oracle.stderr, b'')
        self.assertEqual(len(oracle.stdout), 4 * len(rows))
        expected = list(struct.unpack(
            '<' + str(len(rows)) + 'I', oracle.stdout))
        self.assertNotEqual(expected, list(range(len(rows))))
        self.assertEqual(value['token_order'], expected)
        self.assertEqual(value['chunks'], [expected])
        candidate = self.schedule(rows, policy=1)
        self.assertEqual(candidate['token_order'], expected)
        self.assertEqual(candidate['chunks'], [expected])

    def test_min_new_golden_differs_from_legacy(self):
        rows = [[0, 1], [0, 2], [0, 3], [1, 2]]
        legacy = self.schedule(rows, experts=4, capacity=3)
        candidate = self.schedule(rows, experts=4, capacity=3, policy=1)
        self.assertEqual(legacy['chunks'], [[0, 1], [2], [3]])
        self.assertEqual(candidate['chunks'], [[0, 1, 3], [2]])
        self.assertTrue(candidate['partition_applied'])
        self.assertFalse(candidate['fallback'])
        self.assertEqual(candidate['counters']['work_budget'], 32 * 8)
        self.assertEqual(candidate['counters']['selections'], 4)

    def test_min_new_independent_small_reference(self):
        rng = random.Random(41)
        for sample in range(12):
            rows = [rng.sample(range(11), 3) for _ in range(15)]
            with self.subTest(sample=sample):
                value = self.schedule(rows, experts=11, capacity=5, policy=1)
                self.assertTrue(value['partition_applied'])
                self.assertFalse(value['fallback'])
                self.assertEqual(value['chunks'], independent_min_new(
                    rows, value['token_order'], 5))

    def test_fixed_max_prefill_shape_uses_production_work_budget(self):
        rows = [list(range(10)) for _ in range(8192)]
        value = self.schedule(rows, policy=1)
        self.assertTrue(value['partition_applied'])
        self.assertFalse(value['fallback'])
        self.assertEqual(value['chunks'], [value['token_order']])
        self.assertEqual(value['counters']['work_budget'], 2621440)
        self.assertEqual(value['counters']['work_used'], 90112)
        self.assertEqual(value['counters']['selections'], 8192)

    def test_production_budget_fallback_discards_all_partial_chunks(self):
        rows = [[expert] for expert in range(4096)]
        value = self.schedule(rows, experts=4096, capacity=1, policy=1)
        self.assertFalse(value['partition_applied'])
        self.assertTrue(value['fallback'])
        self.assertEqual(value['chunks'], [[row] for row in range(4096)])
        self.assertEqual(value['chunks'], literal_runtime_legacy(
            rows, value['token_order'], 1))
        self.assertEqual(value['counters']['work_used'], 32 * 4096)
        self.assertLess(value['counters']['selections'], 4096)

    def test_singleton_keeps_legacy_for_either_selected_policy(self):
        rows = [list(reversed(range(10)))]
        legacy = self.schedule(rows, policy=0)
        candidate = self.schedule(rows, policy=1)
        self.assertEqual(legacy, candidate)
        self.assertEqual(legacy['token_order'], [0])
        self.assertEqual(legacy['chunks'], [[0]])
        self.assertFalse(candidate['partition_applied'])
        self.assertFalse(candidate['fallback'])
        self.assertEqual(set(candidate['counters'].values()), {0})

    def reject(self, data):
        result = subprocess.run([str(HELPER)], input=data, capture_output=True,
                                timeout=30, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b'')

    def test_invalid_headers_rejected_before_payload_allocation(self):
        valid = [512, 256, 10, 2, 0]
        for index, bad in [(0, 0), (0, 65537), (1, 0), (1, 513),
                           (1, 9), (2, 0), (2, 17), (3, 0),
                           (3, 8193), (4, 2), (3, 0xffffffff)]:
            header = valid.copy()
            header[index] = bad
            with self.subTest(index=index, value=bad):
                self.reject(struct.pack('<5I', *header))

    def test_duplicate_or_out_of_range_router_ids_rejected(self):
        for policy in (0, 1):
            for rows in ([[0, 0], [1, 2]], [[0, 4], [1, 2]]):
                with self.subTest(policy=policy, rows=rows):
                    self.reject(packet(rows, experts=4, capacity=3,
                                       policy=policy))

    def test_truncated_or_trailing_payload_rejected(self):
        data = packet([list(range(10)), list(range(1, 11))])
        for bad in (b'', data[:19], data[:-1], data + b'\x00', data + data):
            with self.subTest(length=len(bad)):
                self.reject(bad)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helper', required=True, type=Path)
    parser.add_argument('--sort-helper', required=True, type=Path)
    args, rest = parser.parse_known_args()
    HELPER = args.helper.resolve(strict=True)
    SORT_HELPER = args.sort_helper.resolve(strict=True)
    unittest.main(argv=['test_gpu_cache_schedule.py'] + rest)
