"""GPU metadata contracts and finite, independent capacity-bound proofs."""

from copy import deepcopy
from itertools import combinations, product
import unittest

from gpu_cache_replay import (
    COUNTER_KEYS, GpuCacheState, MAX_NEEDED, UINT64_MAX,
    phase_lower_bound, transition_load_lower_bound,
)


def state(slots, ticks=None, clock=10, protected=None, **kwargs):
    return GpuCacheState({
        "slot_experts": slots,
        "slot_ticks": [1] * len(slots) if ticks is None else ticks,
        "slot_protected": ([0] * len(slots) if protected is None
                           else protected),
        "slot_clock": clock,
    }, experts=16, **kwargs)


def subsets(universe, capacity):
    return [frozenset(items) for size in range(capacity + 1)
            for items in combinations(universe, size)]


def optimal_loads(entry, demands, all_caches):
    """Exhaust all legal retained sets; no LRU/victim logic is used here.

    Demand-only loading can retain any subset of old cache plus demand,
    provided all current demand fits. Eviction is free. This independently
    computes the minimum load count for a tiny finite problem.
    """
    costs = {frozenset(entry): 0}
    for demand in demands:
        after = {}
        for before, cost in costs.items():
            available = before | demand
            for cache in all_caches:
                if demand <= cache <= available:
                    candidate = cost + len(demand - before)
                    after[cache] = min(after.get(cache, candidate), candidate)
        costs = after
    return min(costs.values())


class GpuCacheContracts(unittest.TestCase):
    def test_snapshot_restores_physical_slots_raw_clock_and_protection(self):
        original = {
            "slot_experts": [7, -1, 2], "slot_ticks": [19, 0, 13],
            "slot_protected": [1, 0, 0], "slot_clock": 23,
            "layer": 9, "l2_experts": [3],
        }
        cache = GpuCacheState(original, experts=16)
        expected = {key: original[key] for key in (
            "slot_experts", "slot_ticks", "slot_protected", "slot_clock")}
        self.assertEqual(cache.snapshot(), expected)
        original["slot_experts"][0] = 1
        exported = cache.snapshot()
        exported["slot_ticks"][0] = 0
        self.assertEqual(cache.snapshot()["slot_experts"], [7, -1, 2])
        self.assertEqual(cache.snapshot()["slot_ticks"], [19, 0, 13])

    def test_snapshot_rejects_missing_and_invalid_metadata(self):
        original = state([2, 1]).snapshot()
        corruptions = [
            ("slot_experts", [2, 2]), ("slot_experts", [2, 16]),
            ("slot_experts", [-2, 1]), ("slot_experts", [True, 1]),
            ("slot_experts", []), ("slot_ticks", [1]),
            ("slot_ticks", [11, 1]), ("slot_ticks", [-1, 1]),
            ("slot_ticks", [True, 1]), ("slot_protected", [0]),
            ("slot_protected", [2, 0]), ("slot_clock", UINT64_MAX + 1),
            ("slot_clock", -1), ("slot_clock", True),
        ]
        for key, value in corruptions:
            with self.subTest(key=key, value=value):
                changed = deepcopy(original)
                changed[key] = value
                with self.assertRaises(ValueError):
                    GpuCacheState(changed, experts=16)
        for key in original:
            with self.subTest(missing=key):
                changed = deepcopy(original)
                del changed[key]
                with self.assertRaises(ValueError):
                    GpuCacheState(changed, experts=16)

    def test_rejects_unknown_policy_and_capacity_above_experts(self):
        with self.assertRaises(ValueError):
            state([0], policy="reverse_expert_id")
        with self.assertRaises(ValueError):
            GpuCacheState(state([0, 1]).snapshot(), experts=1)

    def test_empty_call_is_noop_even_at_max_clock(self):
        cache = state([0], clock=UINT64_MAX)
        before = cache.snapshot()
        self.assertEqual(cache.resolve([]), dict.fromkeys(COUNTER_KEYS, 0))
        self.assertEqual(cache.snapshot(), before)

    def test_needed_requires_order_and_phase_requires_boolean(self):
        for needed in ({1, 2}, frozenset({1}), {1: 2}, "1", b"1", None):
            with self.subTest(needed=needed):
                with self.assertRaises(ValueError):
                    state([0, 1]).resolve(needed)
        with self.assertRaises(ValueError):
            state([0, 1]).resolve([1], decode_phase=1)

    def test_empty_slots_have_priority_and_lowest_index_for_both_policies(self):
        for policy in ("physical_slot", "expert_id"):
            with self.subTest(policy=policy):
                cache = state([8, -1, -1], [0, 0, 0], policy=policy)
                counts = cache.resolve([5, 3])
                self.assertEqual(cache.snapshot()["slot_experts"], [8, 5, 3])
                self.assertEqual(cache.snapshot()["slot_ticks"], [0, 11, 11])
                self.assertEqual((counts["loads"], counts["evictions"],
                                  counts["resident_delta"]), (2, 0, 2))

    def test_physical_slot_and_id_tie_are_the_only_policy_difference(self):
        baseline = state([8, 2, 5], [1, 1, 7])
        candidate = state([8, 2, 5], [1, 1, 7], policy="expert_id")
        self.assertEqual(baseline.resolve([9]), candidate.resolve([9]))
        self.assertEqual(baseline.snapshot()["slot_experts"], [9, 2, 5])
        self.assertEqual(candidate.snapshot()["slot_experts"], [8, 9, 5])

    def test_id_never_overrides_strictly_older_tick(self):
        cache = state([8, 2, 5], [1, 2, 3], policy="expert_id")
        cache.resolve([9])
        self.assertEqual(cache.snapshot()["slot_experts"], [9, 2, 5])

    def test_complete_needed_set_protects_a_later_lookup(self):
        cache = state([0, 1, 2], [0, 1, 2])
        counts = cache.resolve([3, 0])
        self.assertEqual(cache.snapshot()["slot_experts"], [0, 3, 2])
        self.assertEqual(cache.snapshot()["slot_ticks"], [11, 11, 2])
        self.assertEqual((counts["hits"], counts["loads"]), (1, 1))

    def test_planned_repeats_hit_once_reserved_slot(self):
        cache = state([0, 1, 2], [1, 2, 3])
        counts = cache.resolve([3, 3, 4, 3, 4, 2])
        self.assertEqual(cache.snapshot()["slot_experts"], [3, 4, 2])
        self.assertEqual(cache.snapshot()["slot_ticks"], [11, 11, 11])
        expected = {
            "resolve_calls": 1, "expert_lookups": 6, "hits": 4,
            "misses": 2, "loads": 2, "prefill_lookups": 6,
            "decode_lookups": 0, "prefill_misses": 2, "decode_misses": 0,
            "evictions": 2, "resident_delta": 0, "resident_hits": 1,
            "planned_hits": 3,
        }
        self.assertEqual(counts, expected)

    def test_first_occurrence_order_controls_incoming_physical_mapping(self):
        first, second = state([0, 1]), state([0, 1])
        self.assertEqual(first.resolve([2, 3, 2]),
                         second.resolve([3, 2, 3]))
        self.assertEqual(first.snapshot()["slot_experts"], [2, 3])
        self.assertEqual(second.snapshot()["slot_experts"], [3, 2])

    def test_all_hit_call_stamps_one_tick_not_per_entry(self):
        cache = state([0, 1, 2], [1, 2, 3])
        counts = cache.resolve([1, 1, 0, 1])
        self.assertEqual(cache.snapshot()["slot_clock"], 11)
        self.assertEqual(cache.snapshot()["slot_ticks"], [11, 11, 3])
        self.assertEqual((counts["hits"], counts["misses"]), (4, 0))

    def test_protected_resident_hits_touch_but_cannot_be_victims(self):
        cache = state([0, 1, 2], [0, 1, 2], protected=[1, 0, 0])
        cache.resolve([3])
        self.assertEqual(cache.snapshot()["slot_experts"], [0, 3, 2])
        counts = cache.resolve([0])
        self.assertEqual(counts["hits"], 1)
        self.assertEqual(cache.snapshot()["slot_ticks"], [12, 11, 2])
        self.assertEqual(cache.snapshot()["slot_protected"], [1, 0, 0])

    def test_protected_empty_slot_is_not_allocated(self):
        cache = state([-1, 1], [0, 1], protected=[1, 0])
        cache.resolve([2])
        self.assertEqual(cache.snapshot()["slot_experts"], [-1, 2])

    def test_protected_capacity_failure_never_evicts_protected(self):
        cache = state([0, 1], protected=[1, 1])
        with self.assertRaisesRegex(ValueError, "no residency slot"):
            cache.resolve([2])
        self.assertEqual(cache.snapshot()["slot_experts"], [0, 1])
        self.assertEqual(cache.snapshot()["slot_clock"], 11)

    def test_capacity_failure_keeps_partial_clock_and_planned_touch(self):
        cache = state([0, 1], [1, 2])
        with self.assertRaisesRegex(ValueError, "no residency slot"):
            cache.resolve([2, 2, 3, 4])
        self.assertEqual(cache.snapshot()["slot_experts"], [0, 1])
        self.assertEqual(cache.snapshot()["slot_ticks"], [11, 2])
        self.assertEqual(cache.snapshot()["slot_clock"], 11)

    def test_invalid_needed_ids_fail_after_clock_before_any_touches(self):
        for needed in ([1, -1], [1, 16], [1, True], [1, 2.0]):
            with self.subTest(needed=needed):
                cache = state([0, 1], [1, 2])
                with self.assertRaises(ValueError):
                    cache.resolve(needed)
                self.assertEqual(cache.snapshot()["slot_ticks"], [1, 2])
                self.assertEqual(cache.snapshot()["slot_clock"], 11)

    def test_oversized_input_fails_before_clock_mutation(self):
        cache = state([0])
        before = cache.snapshot()
        with self.assertRaisesRegex(ValueError, "1<<20"):
            cache.resolve([0] * (MAX_NEEDED + 1))
        self.assertEqual(cache.snapshot(), before)

    def test_overflow_is_rejected_without_tick_normalization(self):
        cache = state([0], [UINT64_MAX - 1], clock=UINT64_MAX - 1)
        cache.resolve([0])
        self.assertEqual(cache.snapshot()["slot_ticks"], [UINT64_MAX])
        before = cache.snapshot()
        with self.assertRaisesRegex(ValueError, "overflow"):
            cache.resolve([1])
        self.assertEqual(cache.snapshot(), before)

    def test_decode_counter_flag_tracks_subchunk_not_request_label(self):
        cache = state([0, 1])
        counts = cache.resolve([2, 2], decode_phase=True)
        self.assertEqual((counts["decode_lookups"], counts["decode_misses"],
                          counts["prefill_lookups"], counts["prefill_misses"]),
                         (2, 1, 0, 0))

    def test_state_carry_and_serialized_restart_produce_same_suffix(self):
        cache = state([0, 1, 2], [1, 2, 3])
        cache.resolve([3, 1])
        restored = GpuCacheState(cache.snapshot(), experts=16)
        for needed in ([3, 3], [4, 1], [3, 1, 4]):
            self.assertEqual(cache.resolve(needed), restored.resolve(needed))
            self.assertEqual(cache.snapshot(), restored.snapshot())
        self.assertEqual(cache.snapshot()["slot_clock"], 14)

    def test_id_policy_member_recency_is_invariant_to_slot_permutation(self):
        left = state([8, 2, 5], [1, 1, 3], policy="expert_id")
        right = state([5, 8, 2], [3, 1, 1], policy="expert_id")
        for needed in ([9], [7, 5], [8, 8], [2, 7]):
            self.assertEqual(left.resolve(needed), right.resolve(needed))
            a, b = left.snapshot(), right.snapshot()
            self.assertEqual(dict(zip(a["slot_experts"], a["slot_ticks"])),
                             dict(zip(b["slot_experts"], b["slot_ticks"])))


class CapacityBoundContracts(unittest.TestCase):
    def test_spare_capacity_can_make_transition_bound_zero(self):
        self.assertEqual(transition_load_lower_bound({0}, {1, 2}, 3), 0)
        self.assertEqual(transition_load_lower_bound({0, 1}, {2, 3}, 3), 1)
        self.assertEqual(transition_load_lower_bound({0, 1}, {2, 3}, 2), 2)

    def test_first_demand_uses_actual_entry_and_empty_slots_are_ignored(self):
        self.assertEqual(phase_lower_bound([0, -1], [[0, 1]], 2), 1)
        self.assertEqual(phase_lower_bound([0, 1], [], 2), 0)
        self.assertEqual(phase_lower_bound([0, 1], [[0, 2], [1, 3],
                                                       [0, 2]], 2), 5)

    def test_bound_rejects_invalid_or_oversized_sets(self):
        for previous, following, capacity in [
            ([0, 1], [2], 1), ([0], [1, 2], 1),
            ([-1], [0], 1), ([0], [True], 1), ([0], [1], 0),
        ]:
            with self.subTest(previous=previous, following=following):
                with self.assertRaises(ValueError):
                    transition_load_lower_bound(previous, following,
                                                capacity)
        with self.assertRaises(ValueError):
            phase_lower_bound([0, 1], [[0]], 1)
        with self.assertRaises(ValueError):
            phase_lower_bound([-2], [[0]], 1)

    def test_transition_bound_equals_exhaustive_best_admissible_cache(self):
        universe = range(4)
        for capacity in range(1, 5):
            caches = subsets(universe, capacity)
            for previous, following in product(caches, repeat=2):
                minimum = min(len(following - cache) for cache in caches
                              if previous <= cache)
                self.assertEqual(transition_load_lower_bound(
                    previous, following, capacity), minimum)

    def test_phase_bound_never_exceeds_exhaustive_optimum(self):
        universe = range(3)
        for capacity in (1, 2, 3):
            caches = subsets(universe, capacity)
            for entry in caches:
                for demands in product(caches, repeat=3):
                    optimum = optimal_loads(entry, demands, caches)
                    lower = phase_lower_bound(entry, demands, capacity)
                    self.assertLessEqual(lower, optimum)


if __name__ == "__main__":
    unittest.main()
