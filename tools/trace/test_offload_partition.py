"""Frozen partition-study contracts; no real model or GPU dependencies."""

import argparse
from array import array
import copy
import hashlib
import json
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from offload_replay import ForwardTrace
from run_offload_partition import (
    DIMENSIONS, ROOT, THRESHOLDS, first_prefill_layers, gpu_replay,
    helper_output, run, sample_decision, validate_chunks, validate_plan,
)


HELPER = None


def plan_fixture():
    return dict(schema=1, algorithm='min_new_csr_v1', dimensions=DIMENSIONS,
                work_budget_multiplier=32, thresholds=THRESHOLDS,
                execution_order='construction_order',
                tie_break='original_cpp_lex_rank',
                initial_state='reset_same_hot_per_sample_and_policy',
                required_initial_decision='NO_GO', runtime_changed=False,
                physical_io_prediction=False, requests=[
                    dict(name=name, sha256=str(i) * 64,
                         selection='first_committed_prefill_forward_8192')
                    for i, name in enumerate(
                        ['external-45056', 'business-fv-long-first'])])


class PartitionStudyContracts(unittest.TestCase):
    def test_shared_production_source_identity_is_required(self):
        plan = plan_fixture()
        plan['tool_sources'] = [dict(
            path=str(ROOT / 'tools/trace/offload_partition.h'),
            sha256='0' * 64)]
        with patch('run_offload_partition.verify_identity') as verify:
            with self.assertRaisesRegex(ValueError, 'shared production'):
                validate_plan(plan)
            verify.assert_not_called()
            plan['tool_sources'].append(dict(
                path=str(ROOT / 'include/q4t/model/moe_partition.h'),
                sha256='1' * 64))
            validate_plan(plan)
            self.assertEqual(verify.call_args_list,
                             [unittest.mock.call(item)
                              for item in plan['tool_sources']])

    def test_policy_and_gate_are_frozen(self):
        validate_plan(plan_fixture(), check_sources=False)
        for key, value in [('work_budget_multiplier', 64),
                           ('execution_order', 'greedy_overlap'),
                           ('tie_break', 'expert_id'),
                           ('initial_state', 'continuous_requests'),
                           ('required_initial_decision', 'GO')]:
            plan = copy.deepcopy(plan_fixture())
            plan[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_plan(plan, check_sources=False)
        plan = copy.deepcopy(plan_fixture())
        plan['thresholds']['minimum_gpu_miss_reduction_percent'] = 14
        with self.assertRaises(ValueError):
            validate_plan(plan, check_sources=False)

    def test_each_gate_and_exact_15_percent_boundary(self):
        baseline = dict(gpu_misses=1000, transition_lower_bound=800, subchunks=10)
        candidate = dict(gpu_misses=850, transition_lower_bound=700, subchunks=10)
        self.assertTrue(sample_decision(baseline, candidate, 0)['go'])
        for changed in [dict(candidate, gpu_misses=851),
                        dict(candidate, transition_lower_bound=800),
                        dict(candidate, subchunks=11)]:
            self.assertFalse(sample_decision(baseline, changed, 0)['go'])
        self.assertFalse(sample_decision(baseline, candidate, 1)['go'])
        self.assertFalse(sample_decision(dict(baseline, gpu_misses=0),
                                         dict(candidate, gpu_misses=0), 0)['go'])

    def test_whole_row_capacity_and_permutation_are_required(self):
        ids = [0, 1, 0, 2, 0, 3, 1, 2]
        self.assertEqual(validate_chunks([[0, 1, 3], [2]], ids, 2, 3),
                         [{0, 1, 2}, {0, 3}])
        for chunks in [[], [[]], [[0, 1, 2, 3]], [[0, 1], [2]],
                       [[0, 1, 1], [2, 3]], [[0, 1], [2, 4]],
                       [[True, 0], [2, 3]]]:
            with self.subTest(chunks=chunks), self.assertRaises(ValueError):
                validate_chunks(chunks, ids, 2, 3)

    def test_gpu_loads_and_bound_have_hand_checked_counterexample(self):
        ids = [0, 1, 0, 2, 0, 3, 1, 2]
        baseline = gpu_replay([[0, 1], [2], [3]], ids, 2, 3, [0, 1, 2])
        candidate = gpu_replay([[0, 1, 3], [2]], ids, 2, 3, [0, 1, 2])
        self.assertEqual((baseline['gpu_misses'], candidate['gpu_misses']), (2, 1))
        self.assertEqual((baseline['transition_lower_bound'],
                          candidate['transition_lower_bound']), (2, 1))
        self.assertEqual(baseline['true_prefill_lookups'], 8)
        self.assertEqual(baseline['singleton_runtime_decode_misses'], 2)

    def test_gpu_matches_independent_per_lookup_plan_reference(self):
        rng = random.Random(91)
        for _ in range(80):
            rows = [rng.sample(range(9), 2) for _ in range(8)]
            ids = [e for row in rows for e in row]
            order = list(range(len(rows)))
            rng.shuffle(order)
            chunks = [order[i:i+2] for i in range(0, len(order), 2)]
            slots, ticks, tick, misses = [0, 1, 2, 3], [1, 2, 3, 4], 4, 0
            for chunk in chunks:
                tick += 1
                needed = {e for row in chunk for e in rows[row]}
                planned, reserved = {}, set()
                for row in chunk:
                    for e in rows[row]:
                        slot = slots.index(e) if e in slots else planned.get(e, -1)
                        if slot >= 0:
                            ticks[slot] = tick
                            continue
                        eligible = [s for s in range(4)
                                    if s not in reserved and slots[s] not in needed]
                        slot = min(eligible, key=lambda s: (ticks[s], s))
                        planned[e] = slot
                        reserved.add(slot)
                        misses += 1
                for e, slot in planned.items():
                    slots[slot], ticks[slot] = e, tick
            actual = gpu_replay(chunks, ids, 2, 4, [0, 1, 2, 3])
            expected_sha = hashlib.sha256(
                json.dumps(slots, separators=(',', ':')).encode()).hexdigest()
            self.assertEqual(actual['gpu_misses'], misses)
            self.assertEqual(actual['final_gpu_slots_sha256'], expected_sha)

    def test_selects_only_first_complete_48_layer_prefill_but_exhausts_parser(self):
        seen = []

        def layers(*_):
            for fid in (1, 2):
                for layer in range(48):
                    seen.append((fid, layer))
                    yield dict(stage=1, position=(fid-1)*8192, rows=8192,
                               forward_id=fid, layer=layer)

        with patch('run_offload_partition.iter_layers', layers):
            result = first_prefill_layers(None, None)
        self.assertEqual(len(result), 48)
        self.assertTrue(all(r['forward_id'] == 1 for r in result))
        self.assertEqual(len(seen), 96)
        for invalid in [[dict(stage=1, position=0, rows=4096, forward_id=1, layer=0)],
                        [dict(stage=1, position=0, rows=8192, forward_id=1, layer=i)
                         for i in range(47)]]:
            with patch('run_offload_partition.iter_layers', return_value=iter(invalid)):
                with self.assertRaises(ValueError):
                    first_prefill_layers(None, None)

    def test_no_go_gate_failure_is_saved_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work') as temp:
            directory = Path(temp)
            decision = directory / 'initial.json'
            decision.write_text('{"decision":"GO"}')
            output = directory / 'attempt'
            rc = run(directory / 'missing-plan', directory / 'missing-build',
                     decision, output)
            self.assertEqual(rc, 1)
            saved = (output / 'exit.json').read_bytes()
            status = json.loads(saved)
            self.assertFalse(status['complete'])
            self.assertFalse(status['performance_acceptance'])
            self.assertIn('NO_GO', status['failure'])
            with self.assertRaises(FileExistsError):
                run(directory / 'missing-plan', directory / 'missing-build',
                    decision, output)
            self.assertEqual((output / 'exit.json').read_bytes(), saved)

    def test_real_helper_wire_and_counter_contract(self):
        ids = array('H', list(range(10)) + list(range(10, 20)))
        order = array('I', [0, 1])
        payload = struct.pack('<4I', 512, 256, 10, 2)
        payload += ids.tobytes() + order.tobytes()
        process = subprocess.run([str(HELPER)], input=payload, capture_output=True,
                                 check=True, timeout=10)
        self.assertEqual(process.stderr, b'')
        trace = ForwardTrace(0, '1', 'fixture', 'prefill', ids, 10, order)
        value = helper_output(process.stdout, trace)
        self.assertFalse(value['fallback'])
        self.assertEqual(value['candidate_chunks'], [[0, 1]])
        for bad in (b'', payload + b'x', struct.pack('<4I', 512, 256, 10, 0)):
            invalid = subprocess.run([str(HELPER)], input=bad, capture_output=True,
                                     timeout=10)
            self.assertNotEqual(invalid.returncode, 0)
        broken = copy.deepcopy(value)
        broken['counters']['work_used'] += 1
        with self.assertRaises(ValueError):
            helper_output(json.dumps(broken), trace)
        broken = copy.deepcopy(value)
        broken['fallback'] = True
        with self.assertRaises(ValueError):
            helper_output(json.dumps(broken), trace)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--helper', type=Path, required=True)
    arguments, rest = parser.parse_known_args()
    HELPER = arguments.helper.resolve()
    unittest.main(argv=['test_offload_partition.py'] + rest)
