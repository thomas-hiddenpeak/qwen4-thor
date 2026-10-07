"""Finite offline contracts; no model, trace, filesystem or CUDA execution."""

from copy import deepcopy
from itertools import combinations, product
import unittest

from mirror_retention import analyze_layer, classify_snapshot, compare_layers


def snapshot(gpu, mirror, l2=(), cursor=0):
    return dict(slot_experts=list(gpu), l2_experts=list(l2),
                mirror_experts=list(mirror), mirror_cursor=cursor)


def plan(needed, missing=(), victims=()):
    return dict(needed=list(needed), missing=list(missing), victims=list(victims))


def dimensions(entry, rows):
    return dict(experts=64, gpu_slots=len(entry["slot_experts"]),
        l2_slots=len(entry["l2_experts"]), mirror_k=len(entry["mirror_experts"]),
        top_k=len(rows[0]["needed"]) if rows else 1, expected_plans=len(rows))


def analyze(entry, rows, observed=0, **extra):
    return analyze_layer(entry, rows, observed, **dimensions(entry, rows), **extra)


def compare(short, short_rows, short_count, long, long_rows, long_count):
    return compare_layers(short, short_rows, short_count, long, long_rows,
                          long_count, **dimensions(short, short_rows))


def candidate_assignments(eligible_by_plan, common_by_plan, capacity):
    """Independent oracle: enumerate actual candidate subsets, then count.

    The caller supplies explicit eligibility examples. No production union,
    cap sum or interval formula is reproduced in this finite oracle.
    """
    choices = []
    for eligible in eligible_by_plan:
        values = sorted(eligible)
        choices.append([set(row) for size in range(min(capacity, len(values)) + 1)
                        for row in combinations(values, size)])
    outcomes = {}
    for assignment in product(*choices):
        total = sum(len(row) for row in assignment)
        common = sum(len(row & available) for row, available
                     in zip(assignment, common_by_plan))
        outcomes.setdefault(total, set()).add(common)
    return outcomes


class SnapshotContracts(unittest.TestCase):
    def test_disjoint_categories_and_physical_empty_slots(self):
        entry = snapshot([0, 2, 5, -1], [0, 1, 2, 3, -1], [1, 2, -1], 3)
        got = classify_snapshot(entry, experts=8, gpu_slots=4,
                                l2_slots=3, mirror_k=5)
        self.assertEqual(got, dict(gpu_only=[0], l2_only=[1], both=[2], sole=[3],
            empty_slots=[4], mirror_cursor=3,
            counts=dict(gpu_only=1, l2_only=1, both=1, sole=1, empty_slots=1)))

    def test_default_dimensions_require_actual_capture_shape(self):
        entry = snapshot(list(range(256)), list(range(8)), list(range(16)))
        self.assertEqual(classify_snapshot(entry)["both"], list(range(8)))
        for key in ("slot_experts", "l2_experts", "mirror_experts"):
            changed = deepcopy(entry)
            changed[key].pop()
            with self.subTest(key=key), self.assertRaises(ValueError):
                classify_snapshot(changed)

    def test_invalid_occupied_ids_and_duplicates_reject(self):
        original = snapshot([0, 1], [0, -1], [2, -1])
        for key in ("slot_experts", "l2_experts", "mirror_experts"):
            for values in ([0, 0], [-2, 0], [64, 0], [True, 0], [1.0, 0]):
                changed = deepcopy(original)
                changed[key] = values
                with self.subTest(key=key, values=values), self.assertRaises(ValueError):
                    classify_snapshot(changed, experts=64, gpu_slots=2,
                                      l2_slots=2, mirror_k=2)

    def test_empty_slots_may_repeat_and_disabled_ring_cursor_is_zero(self):
        entry = snapshot([-1, -1], [-1, -1], [-1, -1])
        got = classify_snapshot(entry, experts=4, gpu_slots=2, l2_slots=2, mirror_k=2)
        self.assertEqual(got["empty_slots"], [0, 1])
        self.assertEqual(sum(got["counts"].values()), 2)
        entry = snapshot([0], [], [], 0)
        self.assertEqual(classify_snapshot(entry, experts=4, gpu_slots=1,
                         l2_slots=0, mirror_k=0)["empty_slots"], [])
        entry["mirror_cursor"] = 1
        with self.assertRaises(ValueError):
            classify_snapshot(entry, experts=4, gpu_slots=1, l2_slots=0, mirror_k=0)

    def test_snapshot_cursor_missing_field_and_unordered_array_reject(self):
        entry = snapshot([0, 1], [2, -1])
        for value in (-1, 2, True):
            changed = {**entry, "mirror_cursor": value}
            with self.subTest(cursor=value), self.assertRaises(ValueError):
                classify_snapshot(changed, experts=4, gpu_slots=2, l2_slots=0, mirror_k=2)
        for changed in ({k: v for k, v in entry.items() if k != "l2_experts"},
                        {**entry, "mirror_experts": {2, -1}}):
            with self.assertRaises(ValueError):
                classify_snapshot(changed, experts=4, gpu_slots=2, l2_slots=0, mirror_k=2)

    def test_snapshot_results_do_not_alias_input(self):
        entry = snapshot([0], [0, 1])
        original = deepcopy(entry)
        got = classify_snapshot(entry, experts=4, gpu_slots=1, l2_slots=0, mirror_k=2)
        got["gpu_only"].append(3)
        got["sole"].clear()
        got["counts"]["sole"] = 100
        self.assertEqual(entry, original)


class RetentionContracts(unittest.TestCase):
    def test_entry_support_has_no_invented_eviction_time(self):
        got = analyze(snapshot([5], [1]), [plan([1], [1], [5])], 1,
                      include_plan_support=True)
        self.assertEqual(got["support_counts"], dict(entry_only=1,
            prior_victim_only=0, entry_and_prior_victim=0, unsupported=0))
        self.assertEqual(got["latest_prior_victim_plan_gap_histogram"], {})
        self.assertEqual(got["strictly_intervening_victims_histogram"], {})
        event = got["entry_mirror_first_events"][0]
        self.assertIsNone(event["first_gpu_eviction_plan"])
        self.assertEqual(event["first_gpu_miss_plan"], 1)
        self.assertTrue(event["eviction_censored"])

    def test_unknown_nonmirror_entry_has_no_support(self):
        got = analyze(snapshot([5], [-1]), [plan([1], [1], [5])], 0)
        self.assertEqual(got["support_counts"]["unsupported"], 1)
        self.assertEqual(got["potential_candidate_capacity"], 0)
        with self.assertRaises(ValueError):
            analyze(snapshot([5], [-1]), [plan([1], [1], [5])], 1)

    def test_current_victims_enter_possible_set_only_next_plan(self):
        rows = [plan([0], [0], [1]), plan([1], [1], [0])]
        got = analyze(snapshot([1], [-1]), rows, 1, include_plan_support=True)
        first, second = got["plan_support"]
        self.assertEqual(first["possible_mirror_ids"], [])
        self.assertEqual(first["possible_supported_missing"], [])
        self.assertEqual(second["possible_mirror_ids"], [1])
        self.assertEqual(second["possible_supported_missing"], [1])
        self.assertEqual(second["latest_prior_victim"], [dict(expert=1,
            victim_plan=1, plan_gap=1, strictly_intervening_victims=0)])

    def test_old_victims_are_not_trimmed_to_last_k(self):
        rows = [plan([20 + i], [20 + i], [1 + i]) for i in range(10)]
        rows.append(plan([1], [1], [11]))
        got = analyze(snapshot(range(1, 17), [-1]), rows, 1, include_plan_support=True)
        self.assertEqual(got["plan_support"][-1]["possible_mirror_ids"], list(range(1, 11)))
        self.assertEqual(got["plan_support"][-1]["possible_supported_missing"], [1])
        self.assertEqual(got["latest_prior_victim_plan_gap_histogram"], {"10": 1})
        self.assertEqual(got["strictly_intervening_victims_histogram"], {"9": 1})

    def test_latest_prior_eviction_replaces_old_distance_only(self):
        rows = [plan([2], [2], [1]), plan([1], [1], [2]),
                plan([2], [2], [1]), plan([1], [1], [2])]
        got = analyze(snapshot([1], [-1]), rows, 3, include_plan_support=True)
        self.assertEqual(got["plan_support"][-1]["latest_prior_victim"],
            [dict(expert=1, victim_plan=3, plan_gap=1, strictly_intervening_victims=0)])
        self.assertEqual(got["latest_prior_victim_plan_gap_histogram"], {"1": 3})

    def test_intervening_count_excludes_both_endpoint_plans(self):
        rows = [plan([10, 11], [10, 11], [1, 2]),
                plan([12, 13], [12, 13], [3, 4]), plan([1, 12], [1], [5])]
        got = analyze(snapshot(range(1, 7), [-1]), rows, 1)
        self.assertEqual(got["latest_prior_victim_plan_gap_histogram"], {"2": 1})
        self.assertEqual(got["strictly_intervening_victims_histogram"], {"2": 1})

    def test_entry_gpu_duplicate_first_eviction_precedes_first_miss(self):
        rows = [plan([2], [2], [1]), plan([1], [1], [2])]
        got = analyze(snapshot([1], [1]), rows, 1)
        self.assertEqual(got["entry_mirror_first_events"], [dict(expert=1,
            mirror_slot=0, entry_category="gpu_only", first_gpu_eviction_plan=1,
            first_gpu_miss_plan=2, eviction_censored=False, miss_censored=False,
            censor_after_plan=2)])
        self.assertEqual(got["support_counts"]["entry_and_prior_victim"], 1)

    def test_first_miss_and_first_eviction_are_independent_with_right_censoring(self):
        rows = [plan([1], [1], [4]), plan([5], [5], [1])]
        got = analyze(snapshot([4, 2], [1, 2, 3]), rows, 1)
        first, second, third = got["entry_mirror_first_events"]
        self.assertEqual((first["first_gpu_miss_plan"], first["first_gpu_eviction_plan"]), (1, 2))
        for row in (second, third):
            self.assertIsNone(row["first_gpu_miss_plan"])
            self.assertIsNone(row["first_gpu_eviction_plan"])
            self.assertTrue(row["miss_censored"] and row["eviction_censored"])
            self.assertEqual(row["censor_after_plan"], 2)

    def test_support_ten_but_per_plan_candidate_cap_is_eight(self):
        entry = snapshot([8, 9, *range(20, 34)], range(8))
        rows = [plan([40, 41, *range(20, 28)], [40, 41], [8, 9]),
                plan(range(10), range(10), range(20, 30))]
        got = analyze(entry, rows, 8, include_plan_support=True)
        self.assertEqual(got["supported_missing_total"], 10)
        self.assertEqual(got["potential_candidate_capacity"], 8)
        self.assertEqual(got["plan_support"][1]["candidate_capacity"], 8)
        with self.assertRaises(ValueError):
            analyze(entry, rows, 9)

    def test_plan_sequence_is_fully_consumed_and_exact_length(self):
        entry = snapshot([1], [-1])
        rows = [plan([0], [0], [1]), plan([1], [1], [0])]
        kwargs = dimensions(entry, rows)
        with self.assertRaises(ValueError):
            analyze_layer(entry, rows[:-1], 0, **kwargs)
        with self.assertRaises(ValueError):
            analyze_layer(entry, rows + [plan([0], [0], [1])], 0, **kwargs)
        def corrupt_trailer():
            yield from rows
            raise ValueError("late malformed source")
        with self.assertRaisesRegex(ValueError, "late malformed"):
            analyze_layer(entry, corrupt_trailer(), 0, **kwargs)

    def test_late_plan_shape_and_invalid_ids_reject(self):
        entry = snapshot([2, 3], [-1])
        first = plan([0, 1], [0, 1], [2, 3])
        broken = [plan([0, 0]), plan([True, 1]), plan([0, 64]),
            plan([0, 1], [0, 0]), plan([0, 1], [2]),
            plan([0, 1], [1, 0]), plan([0, 1], [0], [2, 2]),
            plan([0, 1], [0], [1]), plan([0, 1], [0], [2, 3]),
            plan([0, 1], [-1]), {"needed": [0, 1], "missing": []}]
        for last in broken:
            with self.subTest(last=last), self.assertRaises(ValueError):
                analyze(entry, [first, last])

    def test_counts_flags_and_dimensions_reject_bool_or_negative(self):
        entry, rows = snapshot([0], [-1]), [plan([0])]
        for count in (True, -1, 1.0, 1 << 64):
            with self.subTest(count=count), self.assertRaises(ValueError):
                analyze(entry, rows, count)
        with self.assertRaises(ValueError):
            analyze(entry, rows, include_plan_support=1)
        for key, value in (("experts", True), ("gpu_slots", 0), ("mirror_k", -1),
                           ("expected_plans", True), ("top_k", 0)):
            options = {**dimensions(entry, rows), key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                analyze_layer(entry, rows, 0, **options)

    def test_results_and_iterable_inputs_are_copy_isolated(self):
        entry = snapshot([1], [0])
        rows = [plan([0], [0], [1]), plan([1], [1], [0])]
        original_entry, original_rows = deepcopy(entry), deepcopy(rows)
        expected = analyze(entry, rows, 2, include_plan_support=True)
        got = analyze_layer(entry, iter(rows), 2, **dimensions(entry, rows),
                            include_plan_support=True)
        self.assertEqual(got, expected)
        got["plan_support"][0]["possible_mirror_ids"].append(55)
        got["entry_mirror_first_events"][0]["expert"] = 55
        self.assertEqual(entry, original_entry)
        self.assertEqual(rows, original_rows)
        self.assertEqual(analyze(entry, rows, 2, include_plan_support=True), expected)

    def test_empty_phase_and_disabled_ring_have_zero_capacity(self):
        got = analyze(snapshot([0], [1]), [], 0)
        self.assertEqual(got["plan_count"], 0)
        self.assertEqual(got["potential_candidate_capacity"], 0)
        self.assertEqual(got["entry_mirror_first_events"][0]["censor_after_plan"], 0)
        rows = [plan([1], [1], [0]), plan([0], [0], [1])]
        got = analyze(snapshot([0], []), rows, 0)
        self.assertEqual(got["supported_missing_total"], 1)
        self.assertEqual(got["potential_candidate_capacity"], 0)


class CommonIntervalContracts(unittest.TestCase):
    def test_hand_calculated_positive_common_difference_interval(self):
        short = snapshot([5, 6, 7, 8, 9, 10, 11, 12], [0, 1, 2])
        long = snapshot([2, 3, 4, 7, 8, 9, 10, 11], [0, 5, 6])
        sr = [plan(range(7), range(5), range(7, 12))]
        lr = [plan(range(7), [0, 1, 5, 6], range(7, 11))]
        got = compare(short, sr, 3, long, lr, 2)
        self.assertTrue(got["route_equal"])
        self.assertEqual(got["miss_counts"], dict(common=2, S_only=3, L_only=2,
                                                   S_total=5, L_total=4))
        self.assertEqual(got["capacities"], dict(S=dict(common=2, only=1, total=3),
                                               L=dict(common=1, only=2, total=3)))
        self.assertEqual(got["common_candidate_intervals"],
                         dict(S=dict(lower=2, upper=2), L=dict(lower=0, upper=1)))
        self.assertEqual(got["common_candidate_difference_interval"], dict(lower=1, upper=2))

    def test_route_order_mismatch_is_retained_even_when_sets_match(self):
        entry = snapshot([0, 1], [-1])
        got = compare(entry, [plan([0, 1])], 0, entry, [plan([1, 0])], 0)
        self.assertFalse(got["route_equal"])
        self.assertTrue(got["needed_sets_equal"])
        self.assertEqual(got["route_mismatch_plans"], [1])
        self.assertEqual(got["needed_set_mismatch_plans"], [])
        got = compare(entry, [plan([0, 1])], 0, entry, [plan([0, 2], [2], [1])], 0)
        self.assertFalse(got["needed_sets_equal"])
        self.assertEqual(got["needed_set_mismatch_plans"], [1])

    def test_common_only_caps_are_separate_from_total_capacity(self):
        entry = snapshot([2, 3, 4, 5, 6, 7, 8, 9], [0, 1])
        sr = [plan([10, 11, 8, 9], [10, 11], [2, 3]),
              plan([0, 1, 2, 3], [0, 1, 2, 3], [4, 5, 6, 7])]
        lr = [plan([10, 11, 8, 9], [10, 11], [6, 7]),
              plan([0, 1, 2, 3], [0, 1], [4, 5])]
        got = compare(entry, sr, 2, entry, lr, 2)
        self.assertEqual(got["per_plan"][1]["capacities"]["S"],
                         dict(common=2, only=2, total=2))
        self.assertEqual(got["common_candidate_intervals"]["S"], dict(lower=0, upper=2))
        with self.assertRaises(ValueError):
            compare(entry, sr, 3, entry, lr, 2)

    def test_independent_finite_candidate_assignments_fit_every_interval(self):
        short, long = snapshot([3, 4, 5, 6], [0, 2]), snapshot([2, 4, 5, 6], [1, 3])
        sr = [plan([0, 1, 2, 3], [0, 1, 2], [4, 5, 6]),
              plan([4, 5, 6, 7], [4, 5, 6, 7], [0, 1, 2, 3])]
        lr = [plan([0, 1, 2, 3], [0, 1, 3], [4, 5, 6]),
              plan([4, 5, 6, 7], [4, 5, 6, 7], [0, 1, 2, 3])]
        common = [{0, 1}, {4, 5, 6, 7}]
        feasible_s = candidate_assignments([{0, 2}, {4, 5, 6}], common, 2)
        feasible_l = candidate_assignments([{1, 3}, {4, 5, 6}], common, 2)
        for cs, cl in product(feasible_s, feasible_l):
            got = compare(short, sr, cs, long, lr, cl)
            interval = got["common_candidate_difference_interval"]
            for actual_s, actual_l in product(feasible_s[cs], feasible_l[cl]):
                self.assertLessEqual(interval["lower"], actual_s - actual_l)
                self.assertGreaterEqual(interval["upper"], actual_s - actual_l)
            self.assertLessEqual(interval["lower"], interval["upper"])

    def test_missing_tail_on_either_comparison_side_rejects(self):
        entry, rows = snapshot([0], [1]), [plan([1], [1], [0])]
        kwargs = dimensions(entry, rows)
        for sr, lr in (([], rows), (rows, [])):
            with self.assertRaises(ValueError):
                compare_layers(entry, sr, 0, entry, lr, 0, **kwargs)

    def test_zero_observed_candidates_have_zero_common_interval(self):
        entry, rows = snapshot([2], [0, 1]), [plan([0], [0], [2])]
        got = compare(entry, rows, 0, entry, rows, 0)
        self.assertEqual(got["common_candidate_difference_interval"], dict(lower=0, upper=0))


if __name__ == "__main__":
    unittest.main()
