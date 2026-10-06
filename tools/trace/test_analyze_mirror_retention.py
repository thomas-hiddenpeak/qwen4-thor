"""Synthetic retention runner contracts; never opens real traces or models."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from analyze_mirror_retention import (
    COUNTERS, DEPENDENCIES, ENDPOINTS, GROUPS, analyze_request, artifact_path, comparisons,
    execute, observer_counts, validate_ledger, validate_manifest,
    validate_execution, validate_request, validate_scope,
)
from run_gpu_cache_study import sha


OBSERVER_KEYS = ('plans_started plans_complete plans_failed plans_scope_mismatch '
    'planned_loads source_l2 source_mirror source_read committed_loads '
    'claim_errors read_errors commit_errors entry_l2_present entry_mirror_present '
    'entry_mirror_candidate entry_candidate_to_mirror entry_candidate_to_l2 '
    'entry_candidate_to_read_direct_active entry_candidate_to_read_direct_published '
    'entry_candidate_to_read_other entry_candidate_unclaimed writeback_reservations '
    'writeback_pending_missing_targets writeback_pending_entry_candidate_targets '
    'writeback_published writeback_aborted writeback_skipped '
    'source_mirror_outside_entry_candidates duplicate_claims entry_claimed_mirrors '
    'counter_overflow samples_confirmed_total samples_overwritten').split()


def layer_fixture(index=0, step=0):
    return dict(layer=index, slot_experts=list(range(256)),
        slot_ticks=[10 + step if e < 10 and step else 1 for e in range(256)],
        slot_protected=[0] * 256, slot_clock=10 + step,
        l2_experts=list(range(16)), l2_ticks=[1] * 16, l2_clock=16,
        mirror_experts=list(range(256, 264)), mirror_cursor=0)


def decode_counts(steps):
    result = dict.fromkeys(COUNTERS, 0)
    result.update(resolve_calls=48 * steps, expert_lookups=480 * steps,
                  hits=480 * steps, decode_lookups=480 * steps)
    return result


def observed_fixture(plans):
    result = dict.fromkeys(OBSERVER_KEYS, 0)
    result.update(plans_started=plans, plans_complete=plans)
    return result


def request_fixture():
    snapshots = []
    for index, (event, step) in enumerate(ENDPOINTS):
        counters = decode_counts(step)
        stats = dict(counters, l2_hits=0, l2_misses=0, mirror_hits=0,
            shape_multi_lookups=0, shape_single_lookups=480 * step,
            shape_multi_misses=0, shape_single_misses=0,
            mirror_writebacks=0, mirror_skips=0)
        snapshots.append(dict(event=event, decode_forwards=step,
            decode_forward_count=step, counters=counters, stats=stats,
            layers=[layer_fixture(i, step) for i in range(48)]
                   if index in (0, 1, 5) else None))
    forwards = [dict(forward_id=1, stage=1, position=0, rows=1024)]
    forwards.extend(dict(forward_id=step + 1, stage=2,
        position=1023 + step, rows=1) for step in range(1, 256))
    observer = dict(scope='actual_decode', counters=observed_fixture(48 * 255),
        layers=[dict(layer=i, counters=observed_fixture(255), l2_clock_delta=0)
                for i in range(48)], direct_read_losses=0,
        true_decode_entry_premise_applied=True,
        prefix_layer_deltas_available=False)
    return dict(position=0, response_id='synthetic', input_tokens=1024,
        output_tokens=256, snapshots=snapshots, forwards=forwards,
        trace_path='/synthetic-only/request-1.bin', validation={},
        observer_decode=observer)


def synthetic_trace(request, *, fail_after_end=False, wrong_forward=False):
    prefill = list(range(500, 510)) * 1024
    for forward in request['forwards']:
        for layer in range(48):
            row = dict(**forward, layer=layer, top_k=10,
                       topk_ids=prefill if forward['stage'] == 1 else list(range(10)))
            if wrong_forward and forward['stage'] == 2 and layer == 0:
                row['forward_id'] += 1
            yield row
    if fail_after_end:
        raise ValueError('synthetic corrupt trailer')


def scope_fixture():
    return dict(schema=1, stage='mirror_retention_opportunities_offline',
        groups=GROUPS, requests_per_group=4, layers=48, experts=512,
        decode_forwards=255, top_k=10, gpu_slots=256, l2_slots=16,
        mirror_slots=8, primary_position=1, new_HTTP=0, model_runs=0,
        runtime_changes=False, decision_gate=dict(hypothesis_confirmable_here=False))


def manifest_fixture():
    groups, ledger = [], {}
    for group in GROUPS:
        requests = []
        groups.append(dict(id=group, requests=requests))
        for position in range(4):
            q = request_fixture()
            q['position'] = position
            q['trace_path'] = f'/synthetic-only/{group}/request-{position + 1}.bin'
            n = 8193 if group == GROUPS[1] and position == 0 else 1024
            q['input_tokens'] = n
            q['forwards'] = [dict(forward_id=1, stage=1, position=0, rows=min(n, 8192))]
            if n == 8193:
                q['forwards'].append(dict(forward_id=2, stage=1, position=8192, rows=1))
            prefill_forwards = len(q['forwards'])
            q['forwards'].extend(dict(forward_id=i + prefill_forwards + 1,
                stage=2, position=n + i, rows=1) for i in range(255))
            path = Path(q['trace_path'])
            paths = [path, path.with_suffix('.json'), path.with_suffix('.tokens'),
                     path.parent / 'manifest.json']
            for p in paths:
                ledger[str(p)] = 'a' * 64
            q['validation'] = dict(path=str(path),
                manifest=dict(schema=1, complete=True, failure='none', layers=48,
                    experts=512, top_k=10, max_rows=8192, requests_started=4,
                    requests_published=4, binary_sha256='b' * 64),
                metadata=dict(request_id=position + 1, prompt_tokens=n,
                              http_id='synthetic'),
                summary=dict(forwards=257 if n == 8193 else 256,
                    prefill_rows=n, decode_rows=255,
                    route_ids=(n + 255) * 48 * 10, output_tokens=256),
                bindings={str(p): dict(sha256='a' * 64, stat=[1, 2, 3, 4])
                          for p in paths})
            requests.append(q)
    return dict(schema=1, scope_sha256='c' * 64, source_sha256=ledger,
                runtime_binary_sha256='b' * 64, groups=groups)


def contrast_fixture():
    requests, plans, groups = [], [], []
    for group in GROUPS:
        original = []
        groups.append(dict(id=group, requests=original))
        for k in range(4):
            n = 8193 if group == GROUPS[1] and k == 0 else 1024
            endpoints = [dict(layers=[]) for _ in range(3)]
            endpoints[1]['layers'] = [dict(layer=i,
                counts=dict(gpu_only=1, both=0, sole=7)) for i in range(48)]
            requests.append(dict(group=group, position=k, input_tokens=n,
                complete=True, endpoints=endpoints,
                observer_counters=dict(entry_mirror_candidate=50),
                layers=[dict(observed=dict(entry_mirror_candidate=1))
                        for _ in range(48)]))
            plans.append(dict(group=group, position=k, complete=True,
                layers=[dict(plans=[]) for _ in range(48)]))
            trace = f'/synthetic-only/{group}/request-{k + 1}.bin'
            token_sha = 'b' * 64 if group == GROUPS[1] and k == 0 else 'a' * 64
            original.append(dict(position=k, trace_path=trace,
                validation=dict(bindings={str(Path(trace).with_suffix('.tokens')):
                    dict(sha256=token_sha)}),
                snapshots=[{}, dict(layers=[{} for _ in range(48)])]))
    return requests, plans, dict(groups=groups)


class RetentionRunnerContracts(unittest.TestCase):
    def test_frozen_scope_rejects_budget_scope_and_boolean_drift(self):
        validate_scope(scope_fixture())
        for key, value in [('new_HTTP', 1), ('runtime_changes', True),
                           ('primary_position', 2), ('layers', True),
                           ('groups', GROUPS[::-1])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_scope(dict(scope_fixture(), **{key: value}))

    def test_invalid_ledger_rejected(self):
        for ledger in ({}, {'relative': 'a' * 64}, {'/absolute': 'bad'},
                       {'/absolute': True}):
            with self.subTest(ledger=ledger), self.assertRaises(ValueError):
                validate_ledger(ledger)

    def test_manifest_requires_all_positions_and_exact_group_order(self):
        manifest = manifest_fixture()
        validate_manifest(manifest, 'c' * 64)
        for mode in ('omit', 'reorder', 'position'):
            bad = deepcopy(manifest)
            if mode == 'omit':
                bad['groups'][0]['requests'].pop()
            elif mode == 'reorder':
                bad['groups'].reverse()
            else:
                bad['groups'][0]['requests'][2]['position'] = 1
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_manifest(bad, 'c' * 64)

    def test_manifest_wrong_trace_hash_and_runtime_identity_fail(self):
        for key in ('sha', 'binary', 'summary', 'sidecar'):
            bad = manifest_fixture()
            validation = bad['groups'][0]['requests'][0]['validation']
            if key == 'sha':
                validation['bindings'][validation['path']]['sha256'] = 'd' * 64
            elif key == 'binary':
                validation['manifest']['binary_sha256'] = 'd' * 64
            elif key == 'summary':
                validation['summary']['decode_rows'] = 254
            else:
                validation['bindings'].pop(next(iter(validation['bindings'])))
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_manifest(bad, 'c' * 64)

    def test_execution_requires_passed_contract_receipt_for_same_source(self):
        workspace = Path(__file__).resolve().parents[2]
        sources = {str(workspace / name): 'a' * 64 for name in DEPENDENCIES}
        execution = dict(schema=1, scope_sha256='b' * 64,
            manifest_sha256='c' * 64, source_sha256=sources,
            validation_results_path='/synthetic-only/contracts.json',
            validation_results_sha256='d' * 64, request_count=8,
            actual_trace_passes=1, new_HTTP=0, model_runs=0)
        for mode in ('passed', 'failed', 'different_source'):
            receipt = dict(schema=1, passed=mode != 'failed',
                           source_sha256=deepcopy(sources))
            if mode == 'different_source':
                receipt['source_sha256'][next(iter(sources))] = 'e' * 64
            with patch('analyze_mirror_retention.read_bound', return_value=receipt):
                if mode == 'passed':
                    self.assertEqual(validate_execution(execution, 'b' * 64,
                        'c' * 64, workspace), sources)
                else:
                    with self.assertRaises(ValueError):
                        validate_execution(execution, 'b' * 64, 'c' * 64, workspace)

    def test_snapshot_source_alias_and_observer_layer_identity_fail(self):
        for mode in ('alias', 'layer', 'prefix', 'state'):
            q = request_fixture()
            if mode == 'alias':
                q['snapshots'][2]['stats']['shape_single_lookups'] += 1
            elif mode == 'layer':
                q['observer_decode']['layers'][0]['layer'] = 1
            elif mode == 'prefix':
                q['snapshots'][2]['layers'] = []
            else:
                q['snapshots'][1]['layers'][0]['slot_protected'][0] = 1
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_request(q)

    def test_observer_unknown_loss_or_incomplete_source_cannot_pass(self):
        for key in ('plans_failed', 'planned_loads', 'entry_candidate_to_read_other',
                    'source_mirror_outside_entry_candidates', 'entry_mirror_candidate'):
            c = observed_fixture(255)
            c[key] = 1
            with self.subTest(key=key), self.assertRaises(ValueError):
                observer_counts(c, 255)

    def test_full_trace_closes_gpu_prefixes_sources_and_retains_all_plans(self):
        q = request_fixture()
        result, plans = {}, {}
        with patch('analyze_mirror_retention.iter_layers',
                   return_value=synthetic_trace(q)):
            analyze_request(dict(id=GROUPS[0]), q, result, plans)
        self.assertTrue(result['complete'])
        self.assertTrue(result['iterator_exhausted'])
        self.assertTrue(result['exact_gpu_endpoint_checked'])
        self.assertEqual(result['counters'], decode_counts(255))
        self.assertEqual(len(plans['layers']), 48)
        self.assertTrue(all(len(layer['plans']) == 255 for layer in plans['layers']))
        self.assertEqual(plans['layers'][0]['plans'][0],
                         dict(needed=list(range(10)), missing=[], victims=[]))
        self.assertEqual([p['decode_forwards'] for p in result['prefixes']],
                         [1, 8, 32, 255])
        self.assertEqual(len(result['endpoints']), 3)

    def test_corrupt_tail_after_last_decode_is_not_a_partial_pass(self):
        q = request_fixture()
        result, plans = {}, {}
        with patch('analyze_mirror_retention.iter_layers',
                   return_value=synthetic_trace(q, fail_after_end=True)):
            with self.assertRaisesRegex(ValueError, 'corrupt trailer'):
                analyze_request(dict(id=GROUPS[0]), q, result, plans)
        self.assertFalse(result['complete'])
        self.assertFalse(result['iterator_exhausted'])
        self.assertFalse(plans['complete'])
        self.assertEqual(len(plans['layers'][0]['plans']), 255)

    def test_wrong_forward_identity_fails_before_any_projected_plan(self):
        q = request_fixture()
        result, plans = {}, {}
        with patch('analyze_mirror_retention.iter_layers',
                   return_value=synthetic_trace(q, wrong_forward=True)):
            with self.assertRaisesRegex(ValueError, 'forward identity'):
                analyze_request(dict(id=GROUPS[0]), q, result, plans)
        self.assertFalse(result['complete'])
        self.assertEqual(plans['layers'][0]['plans'], [])

    def test_wrong_prefix_counter_fails_at_that_prefix(self):
        q = request_fixture()
        q['snapshots'][3]['counters']['hits'] += 1
        q['snapshots'][3]['stats']['hits'] += 1
        result, plans = {}, {}
        with patch('analyze_mirror_retention.iter_layers',
                   return_value=synthetic_trace(q)):
            with self.assertRaisesRegex(ValueError, 'COUNTER_MISMATCH'):
                analyze_request(dict(id=GROUPS[0]), q, result, plans)
        self.assertFalse(result['complete'])
        self.assertEqual(len(plans['layers'][0]['plans']), 8)

    def test_wrong_physical_endpoint_fails_after_eof(self):
        q = request_fixture()
        q['snapshots'][5]['layers'][0]['slot_ticks'][0] -= 1
        result, plans = {}, {}
        with patch('analyze_mirror_retention.iter_layers',
                   return_value=synthetic_trace(q)):
            with self.assertRaisesRegex(ValueError, 'STATE_MISMATCH'):
                analyze_request(dict(id=GROUPS[0]), q, result, plans)
        self.assertTrue(result['iterator_exhausted'])
        self.assertFalse(result['complete'])

    def test_wrong_per_layer_l2_clock_fails_after_gpu_closure(self):
        q = request_fixture()
        q['snapshots'][5]['layers'][0]['l2_clock'] += 1
        result, plans = {}, {}
        with patch('analyze_mirror_retention.iter_layers',
                   return_value=synthetic_trace(q)):
            with self.assertRaisesRegex(ValueError, 'source/load/clock'):
                analyze_request(dict(id=GROUPS[0]), q, result, plans)
        self.assertTrue(result['iterator_exhausted'])
        self.assertFalse(result['complete'])

    def test_candidate_requires_positive_common_bound_and_ordered_route_equality(self):
        for lower, route, wanted in [(1, True, 'FUTURE_'),
                                    (0, True, 'DEMAND_'),
                                    (-1, True, 'DEMAND_'),
                                    (1, False, 'DEMAND_')]:
            comparison = dict(route_equal=route,
                common_candidate_difference_interval=dict(lower=lower, upper=2))
            with patch('analyze_mirror_retention.compare_layers',
                       return_value=comparison):
                rows, decision = comparisons(*contrast_fixture())
            self.assertEqual([r['position'] for r in rows], [0, 1, 2, 3])
            self.assertFalse(rows[0]['same_input_length'])
            self.assertTrue(decision.startswith(wanted))

    def test_same_length_and_routes_with_different_input_tokens_cannot_nominate(self):
        args = contrast_fixture()
        request = args[2]['groups'][1]['requests'][1]
        token_path = str(Path(request['trace_path']).with_suffix('.tokens'))
        request['validation']['bindings'][token_path]['sha256'] = 'c' * 64
        with patch('analyze_mirror_retention.compare_layers', return_value=dict(
                route_equal=True,
                common_candidate_difference_interval=dict(lower=1, upper=2))):
            rows, decision = comparisons(*args)
        primary = rows[1]
        self.assertTrue(primary['same_input_length'])
        self.assertTrue(primary['route_equal'])
        self.assertFalse(primary['same_input_tokens'])
        self.assertEqual(primary['input_token_sha256_S'], 'a' * 64)
        self.assertEqual(primary['input_token_sha256_L'], 'c' * 64)
        self.assertTrue(decision.startswith('DEMAND_'))

    def test_l2_only_or_different_endpoint_redundancy_cannot_nominate(self):
        args = contrast_fixture()
        for request in args[0]:
            request['endpoints'][0]['layers'] = deepcopy(request['endpoints'][1]['layers'])
            for layer in request['endpoints'][1]['layers']:
                layer['counts'].update(gpu_only=0, both=0, l2_only=1)
        with patch('analyze_mirror_retention.compare_layers', return_value=dict(
                route_equal=True,
                common_candidate_difference_interval=dict(lower=1, upper=2))):
            rows, decision = comparisons(*args)
        self.assertTrue(decision.startswith('DEMAND_'))
        self.assertEqual(rows[1]['mixed_GPU_covered_and_sole_decode_entry_layers'], [])

    def test_incomplete_or_omitted_comparison_position_cannot_pass(self):
        for mode in ('omit', 'incomplete'):
            args = contrast_fixture()
            if mode == 'omit':
                args[0].pop()
            else:
                args[1][0]['complete'] = False
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                comparisons(*args)

    def test_wrong_input_hash_retains_failure_and_existing_output_is_preserved(self):
        workspace = Path(__file__).resolve().parents[2]
        root = workspace / '.q4t-work'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            directory = Path(directory)
            scope = directory / 'scope.json'
            scope.write_text(json.dumps(scope_fixture()))
            args = SimpleNamespace(scope=scope, scope_sha256='0' * 64,
                manifest=directory / 'unused-manifest.json', manifest_sha256='1' * 64,
                execution_plan=directory / 'unused-plan.json',
                execution_plan_sha256='2' * 64, output=directory / 'result.json',
                plans_output=directory / 'plans.json')
            self.assertEqual(execute(args), 1)
            result = json.loads(args.output.read_text())
            self.assertFalse(result['passed'])
            self.assertIn('SHA differs', result['failure'])
            self.assertFalse(json.loads(args.plans_output.read_text())['passed'])
            self.assertEqual(result['plans_sha256'], sha(args.plans_output))
            before = args.output.read_bytes()
            with self.assertRaises(ValueError):
                execute(args)
            self.assertEqual(args.output.read_bytes(), before)

    def test_artifact_output_rejects_tracked_source_and_external_directory(self):
        workspace = Path(__file__).resolve().parents[2]
        for path in (workspace / 'tools/trace/result.json', Path('/tmp/result.json')):
            with self.subTest(path=path), self.assertRaises(ValueError):
                artifact_path(path, workspace)


if __name__ == '__main__':
    unittest.main()
