"""Direct contracts for pinned-hotset plus atomic-group LRU."""
import unittest
from replay_hybrid import Hybrid


class Contracts(unittest.TestCase):
    def test_pinned_never_evicted(self):
        cache = Hybrid(4, 2, list(range(8)))
        cache.consume({2: 1, 3: 1})
        cache.consume({4: 1, 5: 1})
        self.assertEqual(cache.fixed, {0, 1})
        self.assertEqual(set(cache.dynamic), {4, 5})

    def test_hits_precede_group_admission(self):
        cache = Hybrid(4, 2, list(range(8)))
        row = cache.consume({0: 9, 2: 7, 3: 2})
        self.assertEqual((row['hits'], row['route_hits'], row['misses']), (1, 9, 2))
        self.assertEqual(cache.consume({0: 1, 2: 1, 3: 1})['full_hits'], 1)

    def test_oversized_bypass_preserves_order(self):
        cache = Hybrid(4, 2, list(range(8)))
        cache.consume({2: 1})
        cache.consume({3: 1})
        before = dict(cache.dynamic)
        cache.consume({0: 1, 2: 1, 4: 1, 5: 1})
        self.assertEqual(cache.dynamic, before)
        cache.consume({4: 1})
        self.assertEqual(set(cache.dynamic), {3, 4})

    def test_pinned_demand_does_not_take_dynamic_slots(self):
        cache = Hybrid(4, 2, list(range(8)))
        cache.consume({0: 1, 1: 1, 6: 1, 7: 1})
        self.assertEqual(set(cache.dynamic), {6, 7})

    def test_static_endpoint_no_admission(self):
        cache = Hybrid(4, 4, list(range(8)))
        row = cache.consume({0: 1, 7: 1})
        self.assertEqual(row['misses'], 1)
        self.assertEqual(cache.dynamic, {})

    def test_group_order_and_ties(self):
        a, b = [Hybrid(2, 0, list(range(8))) for _ in range(2)]
        a.consume({3: 1, 2: 1})
        b.consume({2: 1, 3: 1})
        self.assertEqual(a.consume({4: 1}), b.consume({4: 1}))
        self.assertEqual(set(a.dynamic), {3, 4})


if __name__ == '__main__':
    unittest.main()
