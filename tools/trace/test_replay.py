"""Direct cache contracts, independent of GPU/runtime."""
from collections import Counter
import unittest
from copy import deepcopy
import json
from pathlib import Path
from replay import Cache, EXPERT_PAYLOAD, SLOT_BYTES, run, validate_plan

RANK = list(range(512))


def demand(start):
    return Counter(range(start, start + 10))


class Contracts(unittest.TestCase):
    def test_cold_group_and_repeat(self):
        c = Cache(10, 'lru', RANK)
        self.assertEqual(c.consume(demand(0), 2)['misses'], 10)
        self.assertEqual(c.consume(demand(0), 2)['full_hits'], 1)

    def test_protected_group(self):
        c = Cache(10, 'lru', RANK)
        c.consume(demand(0), 2)
        r = c.consume(demand(5), 2)
        self.assertEqual(r['misses'], 5)
        self.assertEqual(set(c.resident), set(range(5, 15)))

    def test_permutation_invariant(self):
        a, b = Cache(12, 'lru', RANK), Cache(12, 'lru', RANK)
        for start in [0, 5, 1, 20, 4]:
            self.assertEqual(a.consume(demand(start), 2), b.consume(
                Counter(reversed(range(start, start + 10))), 2))
            self.assertEqual(a.resident, b.resident)

    def test_lru_tie(self):
        c = Cache(12, 'lru', RANK)
        c.consume(demand(0), 2)
        c.consume(demand(5), 2)
        self.assertEqual(set(c.resident), set(range(3, 15)))

    def test_prefill_union_bypass(self):
        c = Cache(10, 'lru', RANK)
        c.consume(demand(0), 2)
        before = c.resident.copy()
        r = c.consume(Counter(list(range(20)) * 7), 1)
        self.assertEqual((r['demands'], r['routes'], r['route_hits'], r['misses']),
                         (20, 140, 70, 10))
        self.assertEqual(c.resident, before)
        self.assertEqual(r['logical_bytes'], 10 * SLOT_BYTES)

    def test_prefill_fits(self):
        c = Cache(10, 'lru', RANK)
        c.consume(Counter(list(range(10)) * 5), 1)
        self.assertEqual(c.consume(demand(0), 2)['full_hits'], 1)

    def test_static_fill_and_no_admission(self):
        c = Cache(32, 'static', RANK)
        self.assertEqual(c.initial, 32)
        self.assertEqual(c.consume(demand(40), 2)['misses'], 10)
        self.assertEqual(c.consume(demand(40), 2)['misses'], 10)

    def test_cross_layer_identity(self):
        a, b = Cache(10, 'lru', RANK), Cache(10, 'lru', RANK)
        a.consume(demand(0), 2)
        self.assertEqual(b.consume(demand(0), 2)['misses'], 10)

    def test_reset_retention_and_charge(self):
        requests = [dict(name=str(i), groups=[(2, 0, 0, demand(0))])
                    for i in range(2)]
        ranks = [RANK] * 48
        for mode, expected in [('cold_decode', 10), ('prefill_reset', 10),
                               ('continuous', 0)]:
            r = run(requests, ranks, 32, 'lru', mode, 64)
            self.assertEqual(r['requests'][1]['layers'][0]['misses'], expected)
            self.assertEqual(r['total_budget_bytes'], (48 * 32 + 10) * SLOT_BYTES)
        r = run(requests, ranks, 32, 'static', 'continuous', 64)
        self.assertEqual([x['initial_bytes'] for x in r['requests']],
                         [48 * 32 * SLOT_BYTES, 0])

    def test_no_future(self):
        a, b = Cache(32, 'lru', RANK), Cache(32, 'lru', RANK)
        prefix = [demand(0), demand(8), demand(20)]
        self.assertEqual([a.consume(x, 2) for x in prefix],
                         [b.consume(x, 2) for x in prefix])
        b.consume(demand(400), 2)
        self.assertNotEqual(a.resident, b.resident)

    def test_bytes(self):
        self.assertEqual(EXPERT_PAYLOAD, 2764816)
        self.assertEqual(SLOT_BYTES, 2765056)
        self.assertEqual(SLOT_BYTES % 256, 0)

    def test_plan_rejections(self):
        good = json.loads(Path(__file__).with_name(
            'replay-plan-20260928.json').read_text())
        validate_plan(good)
        mutations = [
            lambda p: p.update(calibration=[]),
            lambda p: p.update(evaluation={}),
            lambda p: p.update(sources={}),
            lambda p: p.update(capacities=[0]),
            lambda p: p.update(window=0),
            lambda p: p['evaluation'].update(heldout=[]),
            lambda p: p['evaluation']['heldout'].append(p['calibration'][0]),
            lambda p: p['calibration'][0].update(source='missing'),
            lambda p: p['calibration'][0].update(sha256=''),
        ]
        for mutate in mutations:
            bad = deepcopy(good)
            mutate(bad)
            with self.assertRaises(ValueError):
                validate_plan(bad)

    def test_invalid(self):
        for n, p in [(0, 'lru'), (9, 'lru'), (513, 'static'), (32, 'lfu')]:
            with self.assertRaises(ValueError):
                Cache(n, p, RANK)
        c = Cache(32, 'lru', RANK)
        for counts, stage in [(Counter(), 1), (Counter([512]), 1),
                              (Counter([1]), 2), (demand(0), 3)]:
            with self.assertRaises(ValueError):
                c.consume(counts, stage)
        with self.assertRaises(ValueError):
            run([], [RANK] * 48, 32, 'lru', 'continuous', 64)


if __name__ == '__main__':
    unittest.main()
