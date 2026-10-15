"""Independent finite source-scheduling contracts; no runtime or trace I/O."""

from functools import lru_cache
from itertools import combinations, permutations, product
import unittest

from decode_supply_bounds import plan_direct_loss_upper


class FinitePlan:
    """Tiny source-constrained schedules with completed entry events.

    This oracle follows operations, not the upper-bound formula. Each task
    claims all its experts before committing any of them. A commit reserves
    its ring target, publishes its non-needed GPU victim, then releases its
    source. Reservation and publication are separate so a pending claimant
    can observe the old mapping while its buffer is writeback-claimed.

    L2 is absent. Copy completions can occur at any subsequent step; treating
    buffer completions independently deliberately overapproximates a single
    CUDA stream's completion order. These are abstract source schedules, not
    a claim that every enumerated event trace is physically realizable.
    Validating the bound over this larger set remains conservative.
    Ready/unclaimed targets are selected from the cursor; no target means a
    skipped writeback. Entry ring identities are unique; test victims are
    fresh non-needed IDs, avoiding unrelated duplicate invalidation.
    """

    def __init__(self, missing, victims, ring, tasks):
        self.missing = tuple(missing)
        self.index = {expert: i for i, expert in enumerate(self.missing)}
        self.victims = tuple(victims)
        self.entry = frozenset(ring)
        self.tasks = tuple(tuple(task) for task in tasks)
        self.operations = tuple(
            tuple(("claim", expert) for expert in task)
            + tuple((action, expert) for expert in task
                    for action in ("reserve", "publish", "finish"))
            for task in self.tasks
        )
        count = len(self.missing)
        # pcs, ring IDs, owners, ready, cursor, source slots, target slots,
        # lost-entry-mirror bitmask. -1 denotes no owner/source/target.
        self.initial = (
            (0,) * len(tasks), tuple(ring), (-1,) * len(ring),
            (True,) * len(ring), 0, (-1,) * count, (-1,) * count, 0,
        )

    def step(self, state, task):
        pcs, ring, owners, ready, cursor, sources, targets, losses = state
        action, expert = self.operations[task][pcs[task]]
        index = self.index[expert]
        pcs, ring, owners = list(pcs), list(ring), list(owners)
        ready, sources, targets = list(ready), list(sources), list(targets)
        if action == "claim":
            slot = next((i for i, present in enumerate(ring)
                         if present == expert and owners[i] == -1), -1)
            sources[index] = slot
            if slot >= 0:
                # No same-plan victim is needed. A successful source claim
                # therefore consumes an entry mirror whose event completed
                # before the plan, even if a lazy runtime flag remains set.
                assert ready[slot], "unfinished source event in true decode"
                owners[slot] = index
            elif expert in self.entry:
                losses |= 1 << index
        elif action == "reserve":
            if self.victims[index] is not None and ring:
                for offset in range(len(ring)):
                    slot = (cursor + offset) % len(ring)
                    if ready[slot] and owners[slot] == -1:
                        targets[index] = slot
                        owners[slot] = len(self.missing) + index
                        cursor = (slot + 1) % len(ring)
                        break
        elif action == "publish":
            slot = targets[index]
            if slot >= 0:
                assert owners[slot] == len(self.missing) + index
                ring[slot] = self.victims[index]
                owners[slot], ready[slot] = -1, False
        elif action == "finish":
            slot = sources[index]
            if slot >= 0:
                assert owners[slot] == index
                assert ring[slot] == expert
                owners[slot], ready[slot] = -1, False
        else:
            raise AssertionError("unknown oracle action")
        pcs[task] += 1
        return (tuple(pcs), tuple(ring), tuple(owners), tuple(ready), cursor,
                tuple(sources), tuple(targets), losses)

    @staticmethod
    def complete(state, slot):
        changed = list(state)
        ready = list(state[3])
        assert not ready[slot]
        ready[slot] = True
        changed[3] = tuple(ready)
        return tuple(changed)

    def terminal_losses(self):
        """Enumerate all interleavings in this finite overapproximation."""
        @lru_cache(maxsize=None)
        def visit(state):
            runnable = [task for task, pc in enumerate(state[0])
                        if pc < len(self.operations[task])]
            if not runnable:
                # Remaining copies cannot change claims already classified.
                assert all(owner == -1 for owner in state[2])
                return frozenset((state[-1].bit_count(),))
            results = set()
            for task in runnable:
                results.update(visit(self.step(state, task)))
            for slot, ready in enumerate(state[3]):
                if not ready:
                    results.update(visit(self.complete(state, slot)))
            return frozenset(results)

        return visit(self.initial)


def subsets(items, maximum=None):
    maximum = len(items) if maximum is None else maximum
    return [tuple(choice) for size in range(maximum + 1)
            for choice in combinations(items, size)]


class DirectLossBoundContracts(unittest.TestCase):
    def test_empty_plan_and_disabled_mirror_have_zero_bound(self):
        self.assertEqual(plan_direct_loss_upper([], [], [7]), 0)
        self.assertEqual(plan_direct_loss_upper([0, 1], [8], [1], 0), 0)

    def test_single_miss_cannot_lose_its_entry_mirror_before_own_claim(self):
        self.assertEqual(plan_direct_loss_upper([0], [8], [0]), 0)
        oracle = FinitePlan([0], [8], [0], [(0,)])
        self.assertEqual(oracle.terminal_losses(), {0})

    def test_empty_gpu_slots_cannot_write_back(self):
        self.assertEqual(plan_direct_loss_upper([0, 1], [], [0, 1]), 0)
        oracle = FinitePlan([0, 1], [None, None], [0, 1], [(0,), (1,)])
        self.assertEqual(oracle.terminal_losses(), {0})

    def test_four_independent_limits_have_explicit_expected_values(self):
        self.assertEqual(plan_direct_loss_upper(
            range(10), range(20, 30), range(10)), 8)
        self.assertEqual(plan_direct_loss_upper(
            [0, 1, 2], [8, 9, 10], [0, 1, 2], 8), 2)
        self.assertEqual(plan_direct_loss_upper(
            [0, 1, 2, 3], [8], [0, 1, 2, 3], 8), 1)
        self.assertEqual(plan_direct_loss_upper(
            [0, 1, 2, 3], [8, 9, 10, 11], [1, 12], 8), 1)

    def test_no_possible_needed_identity_has_zero_bound(self):
        self.assertEqual(plan_direct_loss_upper([0, 1], [8, 9], [2, 3]), 0)

    def test_possible_set_is_not_trimmed_to_mirror_capacity(self):
        possible = list(range(20))
        self.assertEqual(plan_direct_loss_upper([0, 1, 2], [30, 31, 32],
                                               possible, 2), 2)

    def test_possible_duplicates_and_generators_do_not_change_bound(self):
        self.assertEqual(plan_direct_loss_upper(
            iter([0, 1, 2]), iter([8, 9]), iter([1, 1, 2, 2]), 2), 2)

    def test_arguments_remain_unchanged(self):
        missing, victims, possible = [1, 0], [8, 9], {0, 1, 3}
        self.assertEqual(plan_direct_loss_upper(missing, victims, possible), 1)
        self.assertEqual((missing, victims, possible),
                         ([1, 0], [8, 9], {0, 1, 3}))

    def test_duplicate_missing_or_victim_ids_are_rejected(self):
        for missing, victims in (([0, 0], [8]), ([0, 1], [8, 8])):
            with self.subTest(missing=missing, victims=victims):
                with self.assertRaises(ValueError):
                    plan_direct_loss_upper(missing, victims, [0, 1])

    def test_overlap_and_excess_occupied_victims_are_rejected(self):
        for missing, victims in (([0, 1], [1]), ([0], [8, 9]), ([], [8])):
            with self.subTest(missing=missing, victims=victims):
                with self.assertRaises(ValueError):
                    plan_direct_loss_upper(missing, victims, [])

    def test_invalid_ids_are_rejected_in_every_argument(self):
        for index, bad in product(range(3), (-1, True, 1.0, "1", None)):
            arguments = [[0, 1], [8, 9], [0, 1]]
            arguments[index] = [bad]
            with self.subTest(index=index, bad=bad):
                with self.assertRaises(ValueError):
                    plan_direct_loss_upper(*arguments)

    def test_invalid_containers_are_rejected(self):
        for index, bad in product(range(3), (None, 1, "1", b"1", {1: 2})):
            arguments = [[0, 1], [8, 9], [0, 1]]
            arguments[index] = bad
            with self.subTest(index=index, bad=bad):
                with self.assertRaises(ValueError):
                    plan_direct_loss_upper(*arguments)

    def test_invalid_mirror_capacity_is_rejected(self):
        for bad in (-1, True, 1.0, "8", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    plan_direct_loss_upper([0, 1], [8, 9], [0, 1], bad)

    def test_current_victims_propagate_only_to_later_plans(self):
        possible = set()
        first = plan_direct_loss_upper([0, 1], [4, 5], possible, 2)
        self.assertEqual(first, 0)
        self.assertEqual(possible, set())
        possible.update([4, 5])
        second = plan_direct_loss_upper([4, 6], [0, 1], possible, 2)
        self.assertEqual(second, 1)
        self.assertEqual(possible, {4, 5})

    def test_old_entry_identity_survives_a_skipped_writeback(self):
        # Expert 0 claims the only ring buffer before its own GPU overwrite.
        # That overwrite skips; last-K-GPU-victims would wrongly drop 0.
        oracle = FinitePlan([0, 1], [8, None], [0], [(0,), (1,)])
        state = oracle.initial
        for task in (0, 0, 0, 0, 1, 1, 1, 1):
            state = oracle.step(state, task)
        self.assertEqual(state[1], (0,))
        possible = {0}
        possible.update([8])
        self.assertEqual(plan_direct_loss_upper([0, 2], [1, 3], possible, 1),
                         1)

    def test_reservation_loses_source_before_identity_publication(self):
        oracle = FinitePlan([0, 1], [8, 9], [1], [(0,), (1,)])
        state = oracle.step(oracle.initial, 0)  # Claim 0: read.
        state = oracle.step(state, 0)  # Reserve buffer containing 1.
        self.assertEqual(state[1], (1,))
        self.assertNotEqual(state[2], (-1,))
        state = oracle.step(state, 1)  # Claim 1 before publication: read.
        self.assertEqual(state[1], (1,))
        self.assertEqual(state[5], (-1, -1))
        self.assertEqual(state[-1].bit_count(), 1)
        for task in (0, 0, 1, 1, 1):
            state = oracle.step(state, task)
        self.assertEqual(state[2], (-1,))

    def test_claim_first_protects_same_mirror_against_another_task(self):
        oracle = FinitePlan([0, 1], [8, 9], [1], [(0,), (1,)])
        state = oracle.step(oracle.initial, 1)  # Claim entry mirror 1.
        for task in (0, 0, 0, 0, 1, 1, 1):
            state = oracle.step(state, task)
        self.assertEqual(state[-1], 0)
        self.assertEqual(state[1], (1,))
        self.assertEqual(state[5], (-1, 0))

    def test_same_task_claim_all_prevents_loss_despite_positive_bound(self):
        self.assertEqual(plan_direct_loss_upper([0, 1, 2], [8, 9, 10],
                                               [1, 2], 2), 2)
        for order in permutations((0, 1, 2)):
            oracle = FinitePlan([0, 1, 2], [8, 9, 10], [1, 2], [order])
            self.assertEqual(oracle.terminal_losses(), {0})

    def test_distinct_tasks_can_attain_two_losses_with_identical_gpu_plan(self):
        oracle = FinitePlan([0, 1, 2], [8, 9, 10], [1, 2],
                            [(0,), (1,), (2,)])
        self.assertEqual(oracle.terminal_losses(), {0, 1, 2})
        self.assertEqual(plan_direct_loss_upper([0, 1, 2], [8, 9, 10],
                                               [1, 2], 2), 2)

    def test_finite_interleavings_obey_bound_with_every_small_entry_subset(self):
        cases = 0
        for size in range(3):
            missing = tuple(range(size))
            partitions = [(missing,)]
            if size == 2:
                partitions.extend([((1, 0),), ((0,), (1,))])
            for capacity in range(3):
                for entry in subsets(missing, min(size, capacity)):
                    for ordered_entry in permutations(entry):
                        ring = ordered_entry + tuple(
                            range(40, 40 + capacity - len(entry)))
                        for mask in product((False, True), repeat=size):
                            victims = tuple(20 + i if occupied else None
                                            for i, occupied in enumerate(mask))
                            occupied = [v for v in victims if v is not None]
                            for tasks in partitions:
                                oracle = FinitePlan(missing, victims, ring,
                                                    tasks)
                                observed = oracle.terminal_losses()
                                upper = plan_direct_loss_upper(
                                    missing, occupied, ring, capacity)
                                with self.subTest(missing=missing, ring=ring,
                                                  victims=victims, tasks=tasks):
                                    self.assertTrue(observed)
                                    self.assertLessEqual(max(observed), upper)
                                cases += 1
        self.assertGreater(cases, 100)

    def test_three_miss_partial_tasks_and_inflight_targets_obey_bound(self):
        # Include empty GPU slots and a ring smaller than the missing set;
        # asynchronous completions permit both skipped and reused targets.
        for victims, ring, tasks in (
            ([8, 9, 10], [0], [(0,), (1,), (2,)]),
            ([8, None, None], [1, 2], [(0,), (1,), (2,)]),
            ([8, 9, 10], [1, 2], [(0, 1), (2,)]),
            ([8, 9, 10], [0, 2], [(0,), (1, 2)]),
        ):
            oracle = FinitePlan([0, 1, 2], victims, ring, tasks)
            upper = plan_direct_loss_upper(
                [0, 1, 2], [v for v in victims if v is not None], ring,
                len(ring))
            self.assertLessEqual(max(oracle.terminal_losses()), upper)

    def test_l2_capacity_protects_all_entry_needed_ids_in_finite_claim_orders(self):
        # Worst case: every expert absent from entry L2 reads into L2. A
        # mirror hit would allocate fewer buffers. All old non-needed events
        # are complete at entry, while each admitted source remains claimed.
        for count in range(1, 5):
            needed = frozenset(range(count))
            for capacity in range(count, 6):
                for present in subsets(tuple(needed)):
                    for order in permutations(needed - set(present)):
                        buffers = list(present) + list(
                            range(20, 20 + capacity - len(present)))
                        claimed = set()
                        for expert in order:
                            choices = [i for i, old in enumerate(buffers)
                                       if old not in needed and i not in claimed]
                            self.assertTrue(choices)
                            chosen = choices[-1]  # Independent of LRU order.
                            buffers[chosen] = expert
                            claimed.add(chosen)
                            self.assertTrue(set(present) <= set(buffers))
                        self.assertTrue(needed <= set(buffers))


if __name__ == "__main__":
    unittest.main()
