"""Host-only metadata, interval and conditional-cache contracts."""
from functools import lru_cache
import itertools
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest

from offload_working_set import (WorkingSetObserver, _cache_counts,
                                build_layout, contiguous_next_by_layer,
                                interval_ledger, validate_layout, _write_new)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / '.q4t-work/offload-replay-goal-20261002/test-fixtures'


def layout(n=4, size=100):
    return dict(experts=[dict(layer=0, expert=i, logical_bytes=size,
                              contig_next=i + 1 < n,
                              spans=[dict(file='weights', offset=i * size,
                                          length=size)]) for i in range(n)])


def event(index, expert, request='r1', forward='f1', phase='prefill',
          kind='nvme_read'):
    return dict(event_index=index, expert=expert, layer=0, request_id=request,
                forward_id=forward, runtime_phase=phase, kind=kind)


def brute(stream, cap):
    @lru_cache(None)
    def solve(i, resident):
        if i == len(stream):
            return 0
        requested = stream[i]
        if requested in resident:
            return solve(i + 1, resident)
        if cap == 0:
            return 1 + solve(i + 1, ())
        if len(resident) < cap:
            return 1 + solve(i + 1, tuple(sorted(resident + (requested,))))
        return 1 + min(solve(i + 1, tuple(sorted(
            tuple(x for x in resident if x != victim) + (requested,))))
                       for victim in resident)
    return solve(0, ())


class Contracts(unittest.TestCase):
    def test_oracle_matches_independent_exhaustive_search(self):
        for stream in itertools.product(range(3), repeat=6):
            for cap in range(4):
                actual = _cache_counts(stream, 10, 10 * cap)
                self.assertEqual(actual['belady_misses'], brute(stream, cap))
                self.assertLessEqual(actual['belady_misses'], actual['lru_misses'])

    def test_interval_union_crosses_pages_and_keeps_file_identity(self):
        experts = [dict(spans=[dict(file='a', offset=4090, length=20),
                               dict(file='a', offset=4100, length=20),
                               dict(file='b', offset=4090, length=20)])]
        result = interval_ledger(experts)
        self.assertEqual(result['unique_logical_bytes'], 50)
        self.assertEqual(result['unique_page_cover_bytes'], 16384)

    def test_repeats_warm_state_and_startup_separate(self):
        obs = WorkingSetObserver(layout(), (200,), schedule_label='serial')
        events = [event(0, 0, request='init', forward='init', phase='initial_hot'),
                  event(1, 0), event(2, 0), event(3, 1, kind='l2_hit'),
                  event(4, 0, forward='f2'), event(5, 0, request='r2')]
        for value in events:
            obs(SimpleNamespace(**value))
        result = obs.finish()
        self.assertTrue(result['startup_reads_observed'])
        self.assertEqual(result['totals']['logical_reads'], 5)
        self.assertEqual(result['totals']['same_forward_repeat_reads'], 1)
        self.assertEqual(result['totals']['cross_forward_same_request_repeat_reads'], 1)
        self.assertEqual(result['totals']['cross_request_repeat_reads'], 1)
        self.assertEqual(result['totals']['startup_reuse_reads'], 1)
        self.assertEqual(result['ideal_expert_object_models'][0]['lru_misses'], 1)
        self.assertEqual(result['groups'][0]['phase'], 'startup')
        self.assertIsNone(result['physical_storage_read_bytes'])

    def test_cache_group_counts_partition_totals(self):
        obs = WorkingSetObserver(layout(), (0, 200), schedule_label='serial')
        for i, expert in enumerate([0, 1, 2, 0, 1, 2]):
            obs(event(i, expert, forward=str(i), phase='prefill' if i < 3 else 'decode'))
        for model in obs.finish()['ideal_expert_object_models']:
            for field in ['lru_misses', 'belady_misses']:
                self.assertEqual(model[field], sum(g[field] for g in model['groups']))

    def test_absent_startup_is_counterfactual(self):
        obs = WorkingSetObserver(layout(), (), schedule_label='serial')
        obs(event(1, 0))
        result = obs.finish()
        self.assertFalse(result['startup_reads_observed'])
        self.assertIn('counterfactual', result['initial_state'])

    def test_reject_order_unknown_experts_and_closed_forward(self):
        obs = WorkingSetObserver(layout(), (), schedule_label='serial')
        obs(event(1, 0))
        with self.assertRaises(ValueError):
            obs(event(1, 1))
        with self.assertRaises(KeyError):
            obs(event(2, 99))
        obs(event(3, 0, forward='f2'))
        with self.assertRaises(ValueError):
            obs(event(4, 0))

    def test_unequal_objects_do_not_claim_optimality(self):
        value = layout()
        value['experts'][1]['logical_bytes'] = 200
        obs = WorkingSetObserver(value, (200,), schedule_label='serial')
        obs(event(0, 0))
        self.assertEqual(obs.finish()['ideal_expert_object_models'][0]['status'], 'INDETERMINATE')

    def _fixture(self, directory, gap=False):
        header, index = {}, {}
        prefix = 'model.language_model.layers.0.mlp.experts.'
        offset = 0
        # Three separate globally expert-contiguous regions, matching packing.
        for field, size in [('scalars', 4), ('weight_scale', 512), ('weight', 4096)]:
            for expert in range(2):
                fields = ['input_scale', 'weight_scale_2'] if field == 'scalars' else [field]
                for projection in ['down_proj', 'gate_proj', 'up_proj']:
                    for suffix in fields:
                        name = f'{prefix}{expert}.{projection}.{suffix}'
                        header[name] = dict(dtype='U8', shape=[size],
                                            data_offsets=[offset, offset + size])
                        index[name] = 'experts.safetensors'
                        offset += size
                        if gap and expert == 0 and suffix == 'input_scale':
                            offset += 1
        raw = json.dumps(header).encode()
        with (directory / 'experts.safetensors').open('wb') as destination:
            destination.write(struct.pack('<Q', len(raw)))
            destination.write(raw)
            destination.truncate(8 + len(raw) + offset)
        (directory / 'model.safetensors.index.json').write_text(json.dumps(dict(weight_map=index)))
        (directory / 'config.json').write_text(json.dumps(dict(text_config=dict(
            hidden_size=128, moe_intermediate_size=64))))
        return 8 + len(raw)

    def test_header_layout_absolute_offsets_and_merge_contract(self):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=FIXTURES) as temp:
            directory = Path(temp)
            data_offset = self._fixture(directory)
            result = build_layout(directory)
            self.assertEqual(contiguous_next_by_layer(result), {0: {0}})
            first = result['experts'][0]
            self.assertEqual(first['logical_bytes'], 3 * 4096 + 3 * 512 + 24)
            self.assertEqual(first['spans'][2]['offset'], data_offset)
            self.assertEqual(len(first['spans']), 3)

    def test_scalar_gaps_force_four_scalar_reads_without_gap_bytes(self):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=FIXTURES) as temp:
            directory = Path(temp)
            self._fixture(directory, gap=True)
            result = build_layout(directory)
            first = result['experts'][0]
            self.assertFalse(first['sc_fast'])
            self.assertFalse(first['contig_next'])
            self.assertEqual(len(first['spans']), 6)
            self.assertEqual(first['logical_bytes'], 3 * 4096 + 3 * 512 + 16)

    def test_layout_validates_identity_and_complete_keys(self):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=FIXTURES) as temp:
            directory = Path(temp)
            self._fixture(directory)
            result = build_layout(directory)
            self.assertEqual(validate_layout(result, 1, 2)['files_checked'], 1)
            with self.assertRaises(ValueError):
                validate_layout(result, 1, 3)
            (directory / 'config.json').write_text('{}')
            with self.assertRaises(ValueError):
                validate_layout(result, 1, 2)

    def test_trace_prefill_singleton_runtime_decode_keeps_both(self):
        obs = WorkingSetObserver(layout(), (), schedule_label='serial')
        value = event(0, 0, phase='decode')
        value['trace_phase'] = 'prefill'
        obs(value)
        result = obs.finish()
        self.assertEqual(result['groups'][0]['runtime_phase'], 'decode')
        self.assertEqual(result['trace_phase_totals'][0]['trace_phase'], 'prefill')
        self.assertEqual(result['trace_phase_totals'][0]['runtime_phase'], 'decode')

    def test_no_overwrite_evidence(self):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=FIXTURES) as temp:
            output = Path(temp) / 'result.json'
            _write_new(output, dict(original=True))
            with self.assertRaises(FileExistsError):
                _write_new(output, dict(original=False))
            self.assertTrue(json.loads(output.read_text())['original'])


if __name__ == '__main__':
    unittest.main()
