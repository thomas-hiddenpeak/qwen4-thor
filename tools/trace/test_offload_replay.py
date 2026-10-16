"""Independent host contracts for runtime offload replay; no model or CUDA."""

from collections import Counter
from dataclasses import replace
from itertools import combinations, product
import random
import unittest

from offload_replay import (
    Chunk, ForwardTrace, ReplayConfig, fixed_partition_oracle,
    partition_forward, replay,
)


def config(capacity=3, l2=2, mirror=0, threads=2, **kwargs):
    return ReplayConfig(experts=16, capacity=capacity, l2_slots=l2,
                        mirror_slots=mirror, load_threads=threads,
                        pread_merge=False, **kwargs)


def forward(rows, *, fid="0", request="0", phase="prefill", layer=0,
            order=None):
    if order is None:
        order = sorted(range(len(rows)), key=lambda t: sorted(rows[t]))
    return ForwardTrace(layer, fid, request, phase,
                        [e for row in rows for e in row], len(rows[0]), order)


def chunk(ids, index=0):
    return Chunk(index, (index,), tuple(ids), len(ids), dict(Counter(ids)))


class ReplayContracts(unittest.TestCase):
    def test_effective_configuration_rejects_invented_caches(self):
        for kwargs in ({"l2_slots": 0}, {"l2_slots": 8},
                       {"hot_protect": True}, {"mirror_slots": 33},
                       {"load_threads": 17}, {"inline_miss_limit": 3}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ReplayConfig(**kwargs)

    def test_runtime_partition_reprobes_overlap_after_flush(self):
        trace = forward([[0, 1], [1, 2], [2, 3], [3, 4]])
        chunks = partition_forward(trace, config())
        self.assertEqual([c.token_indices for c in chunks], [(0, 1), (2, 3)])
        self.assertEqual([c.experts for c in chunks], [(0, 1, 2), (2, 3, 4)])
        self.assertEqual(sum(c.lookups for c in chunks), 8)

    def test_token_order_is_actual_input_not_python_tie_assumption(self):
        trace = forward([[0, 1], [1, 0]])
        with self.assertRaisesRegex(ValueError, "C\\+\\+ token_order"):
            partition_forward(replace(trace, token_order=None), config())
        orders = []
        for order in ((0, 1), (1, 0)):
            first = replace(trace, token_order=order)
            result = replay([first, forward([[2, 3]], fid="1")], config(), {0: []})
            orders.append(result.final_states[0]["gpu_slots"])
        self.assertEqual(orders, [(3, 1, 2), (3, 0, 2)])

    def test_bad_input_is_not_silently_repaired(self):
        trace = forward([[0, 1], [2, 3]])
        for changed in (replace(trace, token_order=(1, 0)),
                        replace(trace, token_order=(0, 0)),
                        replace(trace, topk_ids=(0, 16, 2, 3)),
                        replace(trace, topk_ids=(0, 1, 2)),
                        replace(trace, phase="unknown")):
            with self.subTest(trace=changed), self.assertRaises(ValueError):
                partition_forward(changed, config())
        with self.assertRaises(ValueError):
            partition_forward(trace, config(capacity=1))

    def test_missing_hot_or_contiguity_metadata_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "hot metadata missing"):
            replay([forward([[0]])], config(), {})
        with self.assertRaisesRegex(ValueError, "contig_next"):
            replay([], replace(config(), pread_merge=True), {0: []})
        with self.assertRaisesRegex(ValueError, "duplicate initial"):
            replay([], config(), {0: [1, 1]})

    def test_init_hot_order_ticks_l2_tail_and_duplicate_occupancy(self):
        result = replay([], config(capacity=4), {0: [-1, 3, 1, 2, 0, 16, 5]},
                        retain_events=True)
        state = result.final_states[0]
        self.assertEqual(state["gpu_slots"], (3, 1, 2, 0))
        self.assertEqual(state["gpu_ticks"], (1, 2, 3, 4))
        self.assertEqual(set(state["l2_slots"]), {0, 2})
        self.assertEqual(state["occupancy"]["host_extra_experts"], 0)
        self.assertEqual(result.aggregate["nvme_reads"], 4)
        self.assertEqual(result.aggregate["gpu_misses"], 0)
        self.assertEqual(result.aggregate["gpu_loads"], 4)
        self.assertEqual(result.by_trace_phase["initial_hot"]["nvme_reads"], 4)
        self.assertTrue(all(e.trace_phase == "initial_hot" for e in result.events))

    def test_plan_counts_planned_repeats_as_hits(self):
        result = replay([forward([[0, 1], [0, 1]])], config(), {0: []})
        counts = result.forwards[0].counts
        self.assertEqual((counts["gpu_lookups"], counts["gpu_hits"],
                          counts["gpu_misses"]), (4, 2, 2))
        self.assertEqual(counts["gpu_resident_route_hits"], 0)
        self.assertEqual(counts["gpu_planned_route_hits"], 2)

    def test_needed_mark_protects_oldest_gpu_resident(self):
        result = replay([forward([[3, 0]])], config(), {0: [0, 1, 2]})
        self.assertEqual(result.final_states[0]["gpu_slots"], (0, 3, 2))
        self.assertEqual(result.forwards[0].counts["gpu_misses"], 1)

    def test_state_carries_across_request_boundaries_and_is_layer_local(self):
        traces = [forward([[0]], phase="decode"),
                  forward([[0]], request="1", phase="decode"),
                  forward([[0]], request="1", phase="decode", layer=1)]
        result = replay(traces, config(), {0: [], 1: []})
        self.assertEqual([f.counts["gpu_misses"] for f in result.forwards], [1, 0, 1])

    def test_true_prefill_and_runtime_singleton_decode_are_both_reported(self):
        result = replay([forward([[0, 1], [1, 2], [3, 4]])], config(), {0: []})
        self.assertEqual(result.by_trace_phase["prefill"]["gpu_misses"], 5)
        self.assertEqual(result.by_runtime_phase["prefill"]["gpu_misses"], 3)
        self.assertEqual(result.by_runtime_phase["decode"]["gpu_misses"], 2)
        self.assertEqual(result.forwards[0].chunk_sizes, [2, 1])

    def test_l2_is_shared_staging_not_an_independent_extra_cache(self):
        result = replay([forward([[4]])], config(capacity=4), {0: [0, 1, 2, 3]})
        state = result.final_states[0]
        self.assertEqual(set(state["l2_slots"]), {3, 4})
        self.assertEqual(state["occupancy"]["total_unique_experts"], 4)
        self.assertEqual(state["occupancy"]["host_extra_experts"], 0)

    def test_l2_hit_wins_over_mirror_and_does_not_read(self):
        cfg = config(capacity=1, l2=2, threads=1, mirror=1)
        result = replay([forward([[1]]), forward([[0]], fid="1")], cfg, {0: [0]})
        second = result.forwards[1].counts
        self.assertEqual((second["l2_hits"], second["mirror_hits"],
                          second["nvme_reads"]), (1, 0, 0))

    def test_claimed_mirror_source_cannot_be_overwritten(self):
        cfg = config(capacity=1, l2=1, threads=1, mirror=1)
        result = replay([forward([[1]]), forward([[0]], fid="1")], cfg, {0: [0]})
        second = result.forwards[1].counts
        self.assertEqual((second["mirror_hits"], second["mirror_skips"],
                          second["nvme_reads"]), (1, 1, 0))
        self.assertEqual(result.final_states[0]["mirror_slots"], (0,))
        self.assertEqual(result.final_states[0]["occupancy"]["host_extra_experts"], 1)

    def test_schedule_counterexample_is_not_hidden_as_exact_execution(self):
        traces = [forward([[2, 3]]), forward([[0]], fid="1")]
        a = replay(traces, config(capacity=2, mirror=1), {0: [0, 1]})
        b = replay(traces, config(capacity=2, mirror=1,
                                 schedule="batch_deferred"), {0: [0, 1]})
        self.assertEqual(a.forwards[0].counts["mirror_writes"], 2)
        self.assertEqual(b.forwards[0].counts["mirror_writes"], 1)
        self.assertEqual(b.forwards[0].counts["mirror_skips"], 1)
        self.assertEqual(a.forwards[1].counts["nvme_reads"], 1)
        self.assertEqual(b.forwards[1].counts["mirror_hits"], 1)
        self.assertFalse(a.assumptions["lower_cache_is_actual_execution"])
        self.assertFalse(b.assumptions["lower_cache_schedules_are_bounds"])

    def test_merge_batches_sort_only_within_load_threads_and_respect_gaps(self):
        cfg = replace(config(capacity=8, l2=4, threads=4), pread_merge=True,
                      pread_merge_cap=2, contiguous_next_by_layer={0: {0, 1, 2, 4}})
        result = replay([], cfg, {0: [3, 2, 1, 0, 7, 6, 5, 4]}, retain_events=True)
        reads = [e.expert for e in result.events if e.kind == "nvme_read"]
        self.assertEqual(reads, [0, 1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(result.aggregate["stage_batches"], 2)
        self.assertEqual(result.aggregate["dispatch_tasks"], 5)
        self.assertEqual(result.final_states[0]["gpu_slots"], (3, 2, 1, 0, 7, 6, 5, 4))

    def test_deferred_pressure_wait_releases_prior_stream_events(self):
        cfg = config(capacity=6, l2=2, threads=2, mirror=1,
                     schedule="batch_deferred")
        result = replay([forward([[6, 7], [8, 9], [10, 11]])], cfg,
                        {0: [0, 1, 2, 3, 4, 5]})
        self.assertEqual(result.forwards[0].counts["gpu_misses"], 6)
        self.assertEqual(result.forwards[0].counts["nvme_reads"], 6)
        self.assertGreater(result.forwards[0].counts["mirror_writes"], 1)
        self.assertGreater(result.forwards[0].counts["mirror_skips"], 0)

    def test_schedule_and_merge_variants_keep_gpu_contract_and_source_totals(self):
        rng = random.Random(71)
        traces = [forward([rng.sample(range(12), 2) for _ in range(7)], fid=str(i))
                  for i in range(25)]
        gpu_results = []
        for schedule, merge, inline in product(
                ("serial_eager", "batch_deferred"), (False, True), (0, 1, 2)):
            cfg = replace(config(capacity=5, l2=4, threads=4, mirror=2,
                                 schedule=schedule, inline_miss_limit=inline),
                          pread_merge=merge,
                          contiguous_next_by_layer={0: set(range(11))})
            result = replay(traces, cfg, {0: [0, 1, 2, 3, 4]})
            c = result.aggregate
            self.assertEqual(c["gpu_loads"], c["l2_hits"] + c["mirror_hits"]
                             + c["nvme_reads"])
            gpu_results.append([(f.resident_after, f.counts["gpu_misses"])
                                for f in result.forwards])
        self.assertTrue(all(r == gpu_results[0] for r in gpu_results))

    def test_greedy_uses_current_gpu_overlap_preserves_chunks_and_decode(self):
        cfg = config(capacity=2)
        traces = [forward([[0, 1], [2, 3], [4, 5]]),
                  forward([[1, 5]], fid="1", phase="decode")]
        baseline = replay(traces, cfg, {0: [4, 5]})
        greedy = replay(traces, cfg, {0: [4, 5]}, policy="greedy_overlap")
        self.assertEqual(baseline.forwards[0].chunk_order, [0, 1, 2])
        self.assertEqual(greedy.forwards[0].chunk_order, [2, 0, 1])
        self.assertEqual(greedy.forwards[0].counts["gpu_misses"], 4)
        self.assertEqual(baseline.forwards[0].counts["gpu_misses"], 6)
        self.assertEqual(greedy.forwards[0].chunk_sizes, baseline.forwards[0].chunk_sizes)
        self.assertEqual(greedy.forwards[1].chunk_order, [0])

    def test_event_observer_is_streaming_and_phase_metadata_is_complete(self):
        captured = []
        result = replay([forward([[2]], fid="f", request="r")],
                        config(capacity=1, l2=1, threads=1, mirror=1),
                        {0: [0]}, observer=captured.append)
        self.assertEqual(result.events, [])
        self.assertEqual([e.event_index for e in captured], list(range(len(captured))))
        reads = [e for e in captured if e.kind == "nvme_read"]
        self.assertEqual([e.expert for e in reads], [0, 2])
        self.assertEqual((reads[1].request_id, reads[1].forward_id,
                          reads[1].trace_phase, reads[1].runtime_phase),
                         ("r", "f", "prefill", "decode"))

    def test_oracle_matches_exhaustive_set_cache_dynamic_program(self):
        # Independent enumerate-all-successor-cache reference, not Belady.
        requests = [(0,), (1,), (2,), (0, 1), (0, 2), (1, 2)]
        for sequence in product(requests, repeat=3):
            states = {frozenset({0, 3}): 0}
            for need_seq in sequence:
                need, next_states = set(need_seq), {}
                for resident, cost in states.items():
                    misses = len(need - resident)
                    extra = resident - need
                    retain = min(2 - len(need), len(extra))
                    for kept in combinations(extra, retain):
                        successor = frozenset(need | set(kept))
                        value = cost + misses
                        next_states[successor] = min(next_states.get(successor, 99), value)
                states = next_states
            self.assertEqual(fixed_partition_oracle(
                [chunk(ids, i) for i, ids in enumerate(sequence)], 2, {0, 3}),
                min(states.values()), sequence)

    def test_gpu_state_matches_independent_entry_by_entry_runtime_reference(self):
        rng = random.Random(43)
        cfg = config(capacity=5, mirror=2)
        traces = [forward([rng.sample(range(9), 2) for _ in range(9)], fid=str(i))
                  for i in range(30)]
        result = replay(traces, cfg, {0: [4, 0, 3]})
        slots, ticks, tick = [4, 0, 3, -1, -1], [1, 2, 3, 0, 0], 3
        for trace, observed in zip(traces, result.forwards):
            misses = 0
            for c in partition_forward(trace, cfg):
                tick += 1
                planned, reserved = {}, set()
                for token in c.token_indices:
                    for e in trace.topk_ids[token*2:token*2+2]:
                        s = slots.index(e) if e in slots else planned.get(e, -1)
                        if s >= 0:
                            ticks[s] = tick
                            continue
                        empty = [s for s in range(5) if slots[s] < 0 and s not in reserved]
                        eligible = [s for s in range(5)
                                    if s not in reserved and slots[s] not in c.experts]
                        s = empty[0] if empty else min(eligible, key=lambda s: (ticks[s], s))
                        reserved.add(s)
                        planned[e] = s
                        misses += 1
                for e, s in planned.items():
                    slots[s], ticks[s] = e, tick
            self.assertEqual(observed.resident_after, tuple(slots))
            self.assertEqual(observed.counts["gpu_misses"], misses)
            self.assertLessEqual(observed.fixed_partition_oracle_gpu_misses, misses)


if __name__ == "__main__":
    unittest.main()
