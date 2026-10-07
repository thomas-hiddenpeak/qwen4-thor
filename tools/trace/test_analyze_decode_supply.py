"""Synthetic contracts only; no model, real trace or worker simulation."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from analyze_decode_supply import (
    COUNTERS, GROUPS, analyze_request, comparisons, execute,
    identify_layer_supply, interval_index, lower_layer, ordered_plan,
    supply_delta, validate_scope, validate_scope_bindings,
)
from gpu_cache_replay import GpuCacheState


def layer_fixture(index=0, step=0):
    return dict(layer=index, slot_experts=list(range(256)),
        slot_ticks=[10 + step if e < 10 and step else 1 for e in range(256)],
        slot_protected=[0] * 256, slot_clock=10 + step,
        l2_experts=list(range(16)), l2_ticks=[1] * 16, l2_clock=16,
        mirror_experts=list(range(256, 264)), mirror_cursor=0)


def decode_counts(steps):
    value = dict.fromkeys(COUNTERS, 0)
    value.update(resolve_calls=48 * steps, expert_lookups=480 * steps,
                 hits=480 * steps, decode_lookups=480 * steps)
    return value


def request_fixture():
    snapshots = []
    events = [('prefill_begin', 0), ('prefill_end_decode_begin', 0),
              ('decode_prefix', 1), ('decode_prefix', 8),
              ('decode_prefix', 32), ('inference_end', 255)]
    for index, (event, step) in enumerate(events):
        lower = ([layer_fixture(layer, step) for layer in range(48)]
                 if index in (0, 1, 5) else None)
        gpu = ([{key: value for key, value in row.items()
                 if key == 'layer' or key.startswith('slot_')}
                for row in lower] if lower is not None else None)
        snapshots.append(dict(event=event, decode_forwards=step,
            counters=dict(decode_counts(step), l2_hits=0, l2_misses=0,
                          mirror_hits=0), gpu_layers=gpu, lower_layers=lower,
            lower_stats=dict(l2_hits=0, l2_misses=0, mirror_hits=0)))
    forwards = [dict(forward_id=1, stage=1, position=0, rows=1024)]
    forwards.extend(dict(forward_id=step + 1, stage=2,
                         position=1023 + step, rows=1)
                    for step in range(1, 256))
    request = dict(position=1, input_tokens=1024, output_tokens=256,
        snapshots=snapshots, forwards=forwards, trace_path='synthetic-only',
        validation={})
    prior = dict(phases=dict(decode=decode_counts(255)),
                 bounds=dict(decode=dict(layers=[dict(layer=layer, loads=0)
                                                for layer in range(48)])))
    return dict(id=GROUPS[0], arm='A'), request, prior


def synthetic_trace(request, *, fail_after_end=False):
    prefill = list(range(500, 510)) * 1024
    for forward in request['forwards']:
        for layer in range(48):
            yield dict(**forward, layer=layer, top_k=10,
                       topk_ids=prefill if forward['stage'] == 1
                       else list(range(10)))
    if fail_after_end:
        raise ValueError('synthetic parser trailer failure')


def contrast_fixture():
    rows = []
    for group in GROUPS:
        for position in range(4):
            short = group in GROUPS[:2]
            layers = [dict(layer=layer, loads=10,
                actual_mirror_hits=4 if short else 3,
                actual_software_read_misses=6 if short else 7,
                capped_direct_loss_upper=5 if short else 0)
                for layer in range(48)]
            rows.append(dict(group=group, position=position, complete=True,
                supply=dict(mirror_hits=192 if short else 144,
                            l2_misses=288 if short else 336),
                counters=dict(loads=480), layers=layers,
                capped_direct_loss_upper=240 if short else 0))
    return rows


class SupplyProjectionContracts(unittest.TestCase):
    def test_ordered_first_misses_and_occupied_victims(self):
        original = layer_fixture()
        original['slot_experts'] = list(range(10))
        original['slot_ticks'] = list(range(1, 11))
        original['slot_protected'] = [0] * 10
        cache = GpuCacheState(original)
        needed = [12, 0, 11, 2, 3, 4, 5, 6, 7, 8]
        missing, victims, counts = ordered_plan(cache, needed)
        self.assertEqual(missing, [12, 11])
        self.assertEqual(victims, [1, 9])
        self.assertEqual(counts['loads'], 2)
        self.assertEqual(counts['decode_lookups'], 10)
        self.assertEqual(cache.snapshot()['slot_experts'][1], 12)

    def test_empty_victims_filtered_and_bad_router_rows_rejected(self):
        entry = layer_fixture()
        entry['slot_experts'][0:2] = [-1, -1]
        missing, victims, counts = ordered_plan(
            GpuCacheState(entry), list(range(10)))
        self.assertEqual((missing, victims, counts['loads']), ([0, 1], [], 2))
        for needed in ([0] * 10, list(range(9)), list(range(9)) + [512],
                       list(range(9)) + [True]):
            with self.subTest(needed=needed), self.assertRaises(ValueError):
                ordered_plan(GpuCacheState(layer_fixture()), needed)

    def test_clock_supply_uses_delta_not_tick_max_or_sum(self):
        before = dict(l2_clock=100, l2_ticks=[100] * 16)
        after = dict(l2_clock=107, l2_ticks=[1] * 16)
        self.assertEqual(identify_layer_supply(11, before, after), (7, 4))
        for end in (99, 112, True):
            with self.subTest(end=end), self.assertRaises(ValueError):
                identify_layer_supply(11, before, dict(l2_clock=end))

    def test_lower_metadata_requires_same_gpu_and_unique_valid_lower_ids(self):
        original = layer_fixture()
        gpu = deepcopy(original)
        lower_layer(original, gpu, 0)
        for key, bad in [('slot_clock', 11), ('l2_experts', [0] * 16),
                         ('mirror_experts', [512] * 8), ('l2_ticks', [17] * 16),
                         ('mirror_cursor', 8), ('l2_clock', True)]:
            changed = deepcopy(original)
            changed[key] = bad
            with self.subTest(key=key), self.assertRaises(ValueError):
                lower_layer(changed, gpu, 0)

    def test_supply_delta_rejects_reset_or_boolean(self):
        before = dict(l2_hits=0, l2_misses=10, mirror_hits=3)
        after = dict(l2_hits=0, l2_misses=13, mirror_hits=5)
        self.assertEqual(supply_delta(after, before),
                         dict(l2_hits=0, l2_misses=3, mirror_hits=2))
        for change in (dict(after, l2_misses=9), dict(after, l2_hits=True)):
            with self.assertRaises(ValueError):
                supply_delta(change, before)

    def test_interval_boundaries_include_full_223_forward_tail(self):
        self.assertEqual([interval_index(step) for step in (1, 2, 8, 9,
                         32, 33, 255)], [0, 1, 1, 2, 2, 3, 3])
        for step in (0, 256, True):
            with self.assertRaises(ValueError):
                interval_index(step)

    def test_full_synthetic_decode_skips_prefill_and_closes_all_layers(self):
        group, request, prior = request_fixture()
        result = {}
        with patch('analyze_decode_supply.iter_layers',
                   return_value=synthetic_trace(request)):
            analyze_request(group, request, prior, result)
        self.assertTrue(result['complete'])
        self.assertTrue(result['iterator_exhausted'])
        self.assertTrue(result['exact_gpu_endpoint_checked'])
        self.assertFalse(result['counterfactual_history_simulated'])
        self.assertEqual(result['counters'], decode_counts(255))
        self.assertEqual(result['capped_direct_loss_upper'], 0)
        self.assertEqual(len(result['layers']), 48)
        for row in result['layers']:
            self.assertEqual(row['plan_miss_hist'], [255] + [0] * 10)
            self.assertEqual(row['actual_software_read_misses'], 0)
            self.assertEqual(row['last32_raw_upper'], 0)
        self.assertEqual(result['intervals']['decode_32_end']['counters']
                         ['resolve_calls'], 223 * 48)

    def test_nonzero_aggregate_l2_hits_rejects_before_trace(self):
        group, request, prior = request_fixture()
        request['snapshots'][-1]['lower_stats']['l2_hits'] = 1
        request['snapshots'][-1]['counters']['l2_hits'] = 1
        with patch('analyze_decode_supply.iter_layers') as iterator:
            with self.assertRaisesRegex(ValueError, 'zero-L2-hit'):
                analyze_request(group, request, prior, {})
            iterator.assert_not_called()

    def test_lower_stats_must_match_bound_counter_projection(self):
        for key in ('l2_hits', 'l2_misses', 'mirror_hits'):
            group, request, prior = request_fixture()
            request['snapshots'][2]['lower_stats'][key] = 1
            with self.subTest(key=key), patch(
                    'analyze_decode_supply.iter_layers') as iterator:
                with self.assertRaisesRegex(ValueError, 'stats/counter identity'):
                    analyze_request(group, request, prior, {})
                iterator.assert_not_called()

    def test_prefix_counter_mismatch_is_fatal(self):
        group, request, prior = request_fixture()
        request['snapshots'][2]['counters']['hits'] -= 1
        with patch('analyze_decode_supply.iter_layers',
                   return_value=synthetic_trace(request)):
            with self.assertRaisesRegex(ValueError, 'COUNTER_MISMATCH'):
                analyze_request(group, request, prior, {})

    def test_exhaustion_checks_parser_trailer_after_last_layer(self):
        group, request, prior = request_fixture()
        result = {}
        with patch('analyze_decode_supply.iter_layers',
                   return_value=synthetic_trace(request, fail_after_end=True)):
            with self.assertRaisesRegex(ValueError, 'trailer failure'):
                analyze_request(group, request, prior, result)
        self.assertFalse(result['complete'])
        self.assertFalse(result['iterator_exhausted'])

    def test_per_layer_prior_identity_and_gpu_endpoint_are_mandatory(self):
        for changed_field in ('prior_load', 'endpoint_tick'):
            group, request, prior = request_fixture()
            if changed_field == 'prior_load':
                prior['bounds']['decode']['layers'][0]['loads'] = 1
                expected = 'per-layer baseline'
            else:
                request['snapshots'][-1]['gpu_layers'][0]['slot_ticks'][200] = 2
                request['snapshots'][-1]['lower_layers'][0]['slot_ticks'][200] = 2
                expected = 'STATE_MISMATCH'
            with self.subTest(field=changed_field), patch(
                    'analyze_decode_supply.iter_layers',
                    return_value=synthetic_trace(request)):
                with self.assertRaisesRegex(ValueError, expected):
                    analyze_request(group, request, prior, {})

    def test_actual_read_mismatch_cannot_be_hidden_by_valid_gpu_replay(self):
        group, request, prior = request_fixture()
        request['snapshots'][-1]['lower_layers'][0]['l2_clock'] += 1
        with patch('analyze_decode_supply.iter_layers',
                   return_value=synthetic_trace(request)):
            with self.assertRaisesRegex(ValueError, 'clock delta'):
                analyze_request(group, request, prior, {})


class SupplyDecisionContracts(unittest.TestCase):
    def test_execution_binds_every_frozen_scope_source_identity(self):
        bindings = {'/frozen/baseline.json': 'a' * 64,
                    '/frozen/review.json': 'b' * 64}
        scope = dict(source_sha256=bindings)
        validate_scope_bindings(scope, dict(bindings, unrelated='c' * 64))
        for path in bindings:
            missing = dict(bindings)
            del missing[path]
            changed = dict(bindings)
            changed[path] = 'd' * 64
            for sources in (missing, changed):
                with self.subTest(path=path, sources=sources):
                    with self.assertRaisesRegex(ValueError, 'scope sources'):
                        validate_scope_bindings(scope, sources)
        with self.assertRaisesRegex(ValueError, 'scope sources'):
            validate_scope_bindings(dict(source_sha256={}), bindings)

    def test_scope_rejects_new_sampling_dimensions_or_wrong_primary_gate(self):
        scope = dict(
            stage='decode_supply_identification_and_direct_race_bound',
            fixed_groups=GROUPS, requests_per_group=4,
            decode_forwards_per_request=255, layers=48, experts=512,
            top_k=10, gpu_slots=256, l2_slots=16, mirror_slots=8,
            new_runtime_changes_initial_stage=False,
            new_model_runs_initial_stage=0, new_HTTP_initial_stage=0,
            bench=False, decision_gate=dict(primary_position=1,
                max_direct_extra_losses='U_long, never U_long minus U_short'))
        validate_scope(scope)
        for key, value in [('new_HTTP_initial_stage', 1), ('mirror_slots', 16),
                           ('decode_forwards_per_request', 32)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_scope(dict(scope, **{key: value}))
        changed = deepcopy(scope)
        changed['decision_gate']['primary_position'] = 3
        with self.assertRaises(ValueError):
            validate_scope(changed)

    def test_long_upper_alone_is_used_not_long_minus_short(self):
        rows = contrast_fixture()
        # U_L=60 >= deficit48, although U_L-U_S is negative. It must not
        # exclude a full direct explanation or use S's loose bound as loss.
        for row in rows:
            if row['group'] == GROUPS[3] and row['position'] == 1:
                row['capped_direct_loss_upper'] = 60
        result, decision = comparisons(rows)
        primary_a = next(r for r in result if r['arm'] == 'A' and
                         r['position'] == 1)
        self.assertEqual(primary_a['long_direct_upper'], 60)
        self.assertFalse(primary_a['positive_deficit_not_fully_explained'])
        self.assertEqual(decision,
            'DIRECT_RACE_NOT_EXCLUDED_OBSERVER_APPENDIX_REQUIRED')

    def test_both_primary_arms_strict_exclusion_and_reversals_retained(self):
        rows = contrast_fixture()
        long_k2 = next(r for r in rows if r['group'] == GROUPS[3] and
                       r['position'] == 2)
        long_k2['supply']['mirror_hits'] = 200
        result, decision = comparisons(rows)
        self.assertEqual(decision, 'DIRECT_RACE_FULL_EXPLANATION_EXCLUDED')
        reversal = next(r for r in result if r['arm'] == 'A' and
                        r['position'] == 2)
        self.assertEqual(reversal['mirror_deficit'], -8)
        self.assertFalse(reversal['positive_deficit_not_fully_explained'])
        self.assertEqual(len(result), 6)
        self.assertTrue(all(len(row['layers']) == 48 for row in result))

    def test_exact_equality_is_not_strict_exclusion_and_incomplete_rejected(self):
        rows = contrast_fixture()
        rows[13]['capped_direct_loss_upper'] = 48
        _, decision = comparisons(rows)
        self.assertEqual(decision,
            'DIRECT_RACE_NOT_EXCLUDED_OBSERVER_APPENDIX_REQUIRED')
        rows[-1]['complete'] = False
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            comparisons(rows)

    def test_output_is_exclusive_and_failed_input_is_retained(self):
        # Patched open avoids even temporary artifact creation during this
        # contract; the CLI must propagate no-overwrite instead of truncating.
        output = Path(__file__).resolve().parents[3] / 'synthetic-report.json'
        args = SimpleNamespace(output=output, scope_sha256='0' * 64,
            manifest_sha256='1' * 64, execution_plan_sha256='2' * 64,
            scope=Path('missing-scope'))
        with patch.object(Path, 'open', side_effect=FileExistsError) as opened:
            with self.assertRaises(FileExistsError):
                execute(args)
            self.assertEqual(opened.call_args.args, ('x',))

        from io import StringIO
        class RetainedStream(StringIO):
            def close(self):
                pass
        stream = RetainedStream()
        with patch.object(Path, 'open', return_value=stream), patch(
                'analyze_decode_supply.read_bound',
                side_effect=ValueError('synthetic identity mismatch')):
            self.assertEqual(execute(args), 1)
        report = json.loads(stream.getvalue())
        self.assertFalse(report['passed'])
        self.assertEqual(report['status'], 'FAIL_RETAINED')
        self.assertIn('identity mismatch', report['failure'])
        self.assertFalse(report['performance_acceptance'])


if __name__ == '__main__':
    unittest.main()
