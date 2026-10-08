"""Finite host-only T4 MoE parser contracts; no server, CUDA or model loads."""
import hashlib
import json
import math
from pathlib import Path
import unittest

import analyze_mtp_verify_moe_timing as moe


def chain(names, intervals, timestamps, value):
    return {'gpu_marks': [{'name': name, 'at_ns': stamp, 'recorded': True,
                           'ready': True} for name, stamp in zip(names, timestamps)],
            'gpu_intervals': [{'name': name, 'begin': names[i], 'end': names[i + 1],
                               'stream_ms': value} for i, name in enumerate(intervals)],
            'stream_total_ms': len(intervals) * value}


def fixture(steps=2, rows=2, clipped=False):
    maximum = steps * 4 - int(clipped)
    response = {'response_id': 'fixture_verify_moe', 'actual_input': 1024,
                'actual_output': maximum, 'finish': ['length'],
                'actual_path': {'mtp_steps': str(steps)}}
    active = 40 // rows
    histogram = [512 - active, 0, 0, 0, 0]
    histogram[rows] = active
    calls, samples, delivered = [], [], 0
    for index in range(steps):
        base = 100000 + index * 10000
        times = [base, base + 1000, base + 8000, base + 8500,
                 base + 9500, base + 10000]
        step = {'index': index, 'position': 1024 + index * 4, 'k': 3,
                'accepted_drafts': 3, 'returned': 4, 'verify_rows': 4,
                'extend_rows': 4, 'delivered': min(4, maximum - delivered),
                'host_marks': [{'name': n, 'at_ns': t} for n, t in zip(moe.STEP_MARKS, times)],
                'layers': []}
        delivered += step['delivered']
        for layer in range(48):
            sampled = index in moe.SAMPLE_STEPS and layer in moe.SAMPLE_LAYERS
            step['layers'].append({'layer': layer, 'rows': 4, 'experts': 512,
                'top_k': 10, 'histogram': list(histogram), 'routes': 40,
                'active_experts': active, 'invalid_counts': 0, 'streams': 4,
                'sampled': sampled})
            if not sampled:
                continue
            h = base + 1000 + moe.SAMPLE_LAYERS.index(layer) * 1000
            host = [h, h + 20, h + 100, h + 200, h + 230,
                    h + 250, h + 600, h + 700, h + 900]
            stamps = [h + x for x in (1, 10, 30, 90, 150, 650, 680, 730, 760, 800, 890)]
            sample = {'step': index, 'layer': layer, 'rows': 4, 'streams': 4,
                      'counts': [{'expert_id': e, 'rows': rows} for e in range(active)],
                      'host_marks': [{'name': n, 'at_ns': t} for n, t in zip(moe.HOST_MARKS, host)],
                      **chain(moe.GPU_MARKS, moe.GPU_INTERVALS, stamps, 1.0), 'experts': []}
            for n, ordinal in enumerate(moe.SAMPLE_ORDINALS):
                present = ordinal < active
                expert = {'ordinal': ordinal, 'present': present,
                          'expert_id': ordinal if present else -1,
                          'rows': rows if present else 0,
                          'stream_index': ordinal % 4 if present else -1}
                expert.update(chain(moe.EXPERT_MARKS, moe.EXPERT_INTERVALS,
                                    [h + 260 + n * 60 + i * 5 for i in range(5)], 0.25)
                              if present else {'gpu_marks': [], 'gpu_intervals': [],
                                               'stream_total_ms': None})
                sample['experts'].append(expert)
            samples.append(sample)
        calls.append(step)
    events = sum(11 + 5 * sum(e['present'] for e in s['experts']) for s in samples)
    raw = {'schema_version': 1, 'policy': moe.POLICY, 'response_id': response['response_id'],
           'diagnostic_only': True, 'valid': True, 'complete': True, 'error': '',
           'supported': True, 'prompt_tokens': 1024, 'max_tokens': maximum,
           'k': 3, 'max_seq': 1, 'text_only': True, 'known_positions': True,
           'host_unit': 'ns', 'gpu_unit': 'ms', 'stream_time_is_gpu_active': False,
           'host_and_stream_times_additive': False, 'setup_host_ns': 100,
           'report_host_ns': 10, 'step_capacity': min(maximum, 1024),
           'step_count': steps, 'layer_count': steps * 48, 'sample_call_count': len(samples),
           'event_capacity': 496, 'event_created': 496, 'event_recorded': events,
           'event_ready': events, 'sample_steps': list(moe.SAMPLE_STEPS),
           'sample_layers': list(moe.SAMPLE_LAYERS), 'sample_ordinals': list(moe.SAMPLE_ORDINALS),
           'generated': maximum, 'mtp_steps': steps, 'plain_tail': False,
           'finish_reason': 'length', 'fallback': 'none', 'success': True,
           'steps': calls, 'samples': samples}
    return raw, response


class VerifyMoeContracts(unittest.TestCase):
    def reject(self, mutate):
        raw, response = fixture()
        mutate(raw, response)
        with self.assertRaises((ValueError, KeyError, TypeError)):
            moe.validate_trace(raw, response)

    def test_valid_short_has_explicit_absent_late_steps(self):
        raw, response = fixture(steps=1)
        result = moe.validate_trace(raw, response)
        self.assertEqual(result['absent_sample_steps'], [1, 32, 63])
        self.assertEqual(result['census']['calls'], 48)
        self.assertEqual(result['sample_call_count'], 4)

    def test_full_fixed_sample_and_all_census(self):
        raw, response = fixture(steps=64)
        result = moe.validate_trace(raw, response)
        self.assertEqual(result['sample_call_count'], 16)
        self.assertEqual(result['event_recorded'], 496)
        self.assertEqual(result['census']['calls'], 64 * 48)
        self.assertEqual(result['generated'], 256)

    def test_absent_ordinals_not_replaced_or_zero_timed(self):
        raw, response = fixture(rows=4)
        result = moe.validate_trace(raw, response)
        self.assertEqual(result['samples'][0]['absent_ordinals'], [10, 15])
        self.assertEqual(result['event_recorded'], 8 * 21)

    def test_quota_clip_and_first_steady_denominators(self):
        raw, response = fixture(clipped=True)
        result = moe.validate_trace(raw, response)
        self.assertEqual((result['engine_returned'], result['generated'], result['quota_clipped']), (8, 7, 1))
        self.assertEqual(result['first_step']['index'], 0)
        self.assertEqual(result['steady_step_count'], 1)
        self.assertNotEqual(result['phase_ns_per_delivered_token'], result['phase_ns_per_returned_token'])

    def test_experts_are_not_added_to_main_total(self):
        raw, response = fixture()
        original = moe.validate_trace(raw, response)
        for sample in raw['samples']:
            for expert in sample['experts']:
                for interval in expert['gpu_intervals']:
                    interval['stream_ms'] *= 2
                expert['stream_total_ms'] *= 2
        changed = moe.validate_trace(raw, response)
        self.assertEqual(original['samples'][0]['stream_total_ms'], changed['samples'][0]['stream_total_ms'])
        self.assertFalse(changed['nested_experts_added_to_parent'])
        self.assertIsNone(moe.summarize_samples([changed])['extrapolated_total_verify_moe_cost'])

    def test_gpu_fixed_ulp_budget_accepts_one_ulp(self):
        budget = moe.gpu_closure([1.0] * 10, 10.0 + math.ldexp(1.0, -20))
        self.assertLessEqual(abs(budget['residual_ms']), budget['budget_ms'])

    def test_gpu_fixed_budget_rejects_large_residual(self):
        with self.assertRaises(ValueError):
            moe.gpu_closure([1.0] * 10, 10.001953125)

    def test_raw_ids_and_disabled_policies(self):
        raw, response = fixture()
        line = moe.PREFIX + json.dumps(raw)
        self.assertEqual(len(moe.audit_traces(line, [response], True)), 1)
        self.assertEqual(moe.audit_traces('ordinary log', [], False), [])
        for log, enabled in [(line, False), ('', True), (line + '\n' + line, True),
                             ('[q4t][mtp_cycle_timing] {}', False),
                             ('[q4t][mtp_init_timing] {}', False)]:
            with self.subTest(log=log[:50], enabled=enabled), self.assertRaises(ValueError):
                moe.audit_traces(log, [response], enabled)

    def test_json_duplicate_nonfinite_malformed_prefix(self):
        for value in ['{"x":1,"x":2}', '{"x":NaN}', '[]']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                moe.records(moe.PREFIX + value)
        with self.assertRaises(ValueError):
            moe.records('extra ' + moe.PREFIX + '{}')

    def test_policy_identity_and_shape_rejected(self):
        for key, value in [('policy', 'other'), ('response_id', 'wrong'),
                           ('schema_version', True), ('known_positions', False),
                           ('k', 4), ('prompt_tokens', 2048), ('host_unit', 'ms')]:
            with self.subTest(key=key):
                self.reject(lambda r, _, k=key, v=value: r.update({k: v}))
        self.reject(lambda r, _: r.update(sample_steps=[False, 1, 32, 63]))

    def test_source_binding_mismatch_rejected(self):
        class FixtureReader:
            def bind(self, _):
                return b'frozen bytes'
        source = {'path': '/synthetic/owned-source.py',
                  'sha256': hashlib.sha256(b'frozen bytes').hexdigest()}
        moe.check_bindings(FixtureReader(), [source], Path('/synthetic'))
        source['sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            moe.check_bindings(FixtureReader(), [source], Path('/synthetic'))

    def test_invalid_incomplete_or_unsupported_rejected(self):
        for key, value in [('valid', False), ('complete', False), ('supported', False),
                           ('error', 'event_query_failed'), ('plain_tail', True),
                           ('fallback', 'plain'), ('success', False)]:
            with self.subTest(key=key):
                self.reject(lambda r, _, k=key, v=value: r.update({k: v}))

    def test_missing_layer(self):
        self.reject(lambda r, _: r['steps'][0]['layers'].pop())

    def test_duplicate_layer(self):
        self.reject(lambda r, _: r['steps'][0]['layers'][1].update(layer=0))

    def test_bad_histogram_route_conservation(self):
        for histogram in ([491, 0, 20, 0, 0], [492, 1, 19, 0, 0], [492, False, 20, 0, 0]):
            with self.subTest(histogram=histogram):
                self.reject(lambda r, _, h=histogram: r['steps'][0]['layers'][0].update(histogram=h))

    def test_invalid_clamp_active_or_streams(self):
        for key, value in [('invalid_counts', 1), ('active_experts', 19), ('routes', 39),
                           ('rows', 3), ('streams', 1), ('sampled', False)]:
            with self.subTest(key=key):
                self.reject(lambda r, _, k=key, v=value: r['steps'][0]['layers'][0].update({k: v}))

    def test_sparse_counts_must_match_census(self):
        self.reject(lambda r, _: r['samples'][0]['counts'][0].update(rows=1))

    def test_sparse_counts_duplicate_expert(self):
        self.reject(lambda r, _: r['samples'][0]['counts'][1].update(expert_id=0))

    def test_fixed_sampling_cannot_shift(self):
        self.reject(lambda r, _: r['samples'][0].update(layer=1))

    def test_missing_sample(self):
        self.reject(lambda r, _: r['samples'].pop())

    def test_wrong_ordinal_or_stream(self):
        for key, value in [('ordinal', 1), ('stream_index', 2), ('expert_id', 7), ('rows', 3)]:
            with self.subTest(key=key):
                self.reject(lambda r, _, k=key, v=value: r['samples'][0]['experts'][0].update({k: v}))

    def test_absent_expert_cannot_have_zero_fabricated_measurement(self):
        raw, response = fixture(rows=4)
        raw['samples'][0]['experts'][2]['stream_total_ms'] = 0.0
        with self.assertRaises(ValueError):
            moe.validate_trace(raw, response)

    def test_missing_unrecorded_unready_events(self):
        self.reject(lambda r, _: r['samples'][0]['gpu_marks'].pop())
        for key in ('recorded', 'ready'):
            with self.subTest(key=key):
                self.reject(lambda r, _, k=key: r['samples'][0]['gpu_marks'][0].update({k: False}))

    def test_event_capacity_and_counts(self):
        for key, value in [('event_capacity', 497), ('event_created', 495),
                           ('event_recorded', 249), ('event_ready', 247),
                           ('step_capacity', 1024), ('layer_count', 95)]:
            with self.subTest(key=key):
                self.reject(lambda r, _, k=key, v=value: r.update({k: v}))

    def test_host_ns_is_integer_and_monotonic(self):
        for value in (-1, 100000.0, True):
            with self.subTest(value=value):
                self.reject(lambda r, _, v=value: r['steps'][0]['host_marks'][0].update(at_ns=v))
        self.reject(lambda r, _: r['steps'][0]['host_marks'][2].update(at_ns=100500))

    def test_steps_cannot_overlap(self):
        self.reject(lambda r, _: r['steps'][1]['host_marks'][0].update(at_ns=109999))

    def test_counts_event_must_precede_existing_sync_end(self):
        self.reject(lambda r, _: r['samples'][0]['gpu_marks'][4].update(at_ns=101201))

    def test_sample_host_must_remain_inside_verify(self):
        self.reject(lambda r, _: r['samples'][0]['host_marks'][0].update(at_ns=100999))

    def test_expert_record_timestamps_are_host_nested(self):
        self.reject(lambda r, _: r['samples'][0]['experts'][0]['gpu_marks'][0].update(at_ns=101249))

    def test_gpu_nonfinite_null_or_non_binary32(self):
        for value in (None, math.inf, 0.1):
            with self.subTest(value=value):
                self.reject(lambda r, _, v=value: r['samples'][0]['gpu_intervals'][0].update(stream_ms=v))

    def test_gpu_boundaries_must_be_shared(self):
        self.reject(lambda r, _: r['samples'][0]['gpu_intervals'][1].update(begin='moe_begin'))

    def test_output_and_engine_contracts(self):
        for key, value in [('delivered', 3), ('returned', 3), ('accepted_drafts', 4),
                           ('verify_rows', 3), ('extend_rows', 3), ('position', 0)]:
            with self.subTest(key=key):
                self.reject(lambda r, _, k=key, v=value: r['steps'][0].update({k: v}))
        self.reject(lambda r, _: r.update(generated=7))
        self.reject(lambda r, _: r.update(step_count=1))

    def test_strict_screen_uses_both_later_values_and_all15_cells(self):
        metric = {'ttft': 2.0, 'decode_seconds': 2.0, 'latency': 2.0}
        comparison = {'tiers': [{'length': n,
                                'left': {'later': [dict(metric), dict(metric)]},
                                'right': {'later': [dict(metric), dict(metric)]}}
                               for n in moe.LENGTHS]}
        result = moe.strict_screen(comparison)
        self.assertTrue(result['passed'])
        self.assertEqual(len(result['cells']), 15)
        comparison['tiers'][0]['right']['later'][0]['ttft'] += 0.0000001
        result = moe.strict_screen(comparison)
        self.assertFalse(result['passed'])
        self.assertEqual(len(result['failed_cells']), 1)
        self.assertFalse(result['performance_acceptance'])


if __name__ == '__main__':
    unittest.main()
