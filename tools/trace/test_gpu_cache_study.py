"""Bounded runner contracts using synthetic routes only; no real replay."""

from collections import Counter
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import run_gpu_cache_study as study


SCOPE_SHA = 'a' * 64
SOURCE_SHA = 'b' * 64
RUNTIME_COMMIT = 'c' * 40
ALIASES = {'prefill_lookups': 'shape_multi_lookups',
           'decode_lookups': 'shape_single_lookups',
           'prefill_misses': 'shape_multi_misses',
           'decode_misses': 'shape_single_misses'}


def counters(forwards=0, multi_rows=0, single_rows=0):
    result = dict.fromkeys(study.COUNTERS, 0)
    result.update(resolve_calls=forwards * 48,
                  expert_lookups=(multi_rows + single_rows) * 480,
                  hits=(multi_rows + single_rows) * 480,
                  prefill_lookups=multi_rows * 480,
                  decode_lookups=single_rows * 480)
    return result


def layers(clock):
    return [{'layer': layer, 'slot_experts': list(range(256)),
             'slot_ticks': [clock] * 10 + [0] * 246,
             'slot_protected': [0] * 256, 'slot_clock': clock}
            for layer in range(48)]


def manifest_fixture():
    """Analytic all-hit state/counter oracle, independent of replay calls."""
    result = dict(schema=1, scope_sha256=SCOPE_SHA,
                  source_sha256={}, counter_aliases=dict(ALIASES), groups=[],
                  runtime_source_commit=RUNTIME_COMMIT,
                  runtime_binary_sha256=SOURCE_SHA,
                  prior_delivery_commit=RUNTIME_COMMIT)
    for group_index, gid in enumerate(study.GROUPS):
        group = dict(id=gid, arm='A' if group_index in (0, 3) else 'C',
                     requests=[])
        prior_forwards = prior_multi = prior_single = 0
        for position in range(4):
            prompt = 8193 if position == 0 and group_index >= 2 else 1024
            prefill = [(1, 0, min(prompt, 8192))]
            if prompt == 8193:
                prefill.append((1, 8192, 1))
            schedule = prefill + [(2, prompt + step, 1) for step in range(255)]
            forwards = [dict(forward_id=prior_forwards + index + 1,
                             stage=stage, position=start, rows=rows)
                        for index, (stage, start, rows) in enumerate(schedule)]
            path = Path('/synthetic') / gid / f'request-{position + 1}.bin'
            package = dict(schema=1, complete=True, failure='none', layers=48,
                           experts=512, top_k=10, max_rows=8192,
                           max_length=262144, requests_started=4,
                           requests_published=4, binary_sha256=SOURCE_SHA,
                           model_index_sha256=SOURCE_SHA,
                           workload_sha256=SOURCE_SHA)
            binding_paths = (path, path.with_suffix('.json'),
                             path.with_suffix('.tokens'),
                             path.parent / 'manifest.json')
            bindings = {str(p): {'sha256': SOURCE_SHA, 'stat': [1, 2, 3, 4]}
                        for p in binding_paths}
            result['source_sha256'].update({str(p): SOURCE_SHA
                                            for p in binding_paths})
            validation = dict(schema=1, path=str(path),
                              trace_sha256=SOURCE_SHA, manifest=package,
                              metadata={'request_id': position + 1,
                                        'http_id': f'chatcmpl-auto-{position}',
                                        'prompt_tokens': prompt},
                              summary={'forwards': len(forwards),
                                       'prefill_rows': prompt,
                                       'decode_rows': 255,
                                       'route_ids': (prompt + 255) * 480,
                                       'output_tokens': 256},
                              bindings=bindings,
                              validation_origin='reused completed observation_contract',
                              prior_decision_path=f'/synthetic/{gid}-decision.json',
                              prior_decision_sha256=SOURCE_SHA,
                              prior_validation_pointer=
                              f'/observation_contract/requests/{position}/trace',
                              new_checker_execution=False)
            result['source_sha256'][validation['prior_decision_path']] = SOURCE_SHA
            prefill_multi = sum(rows for _, _, rows in prefill if rows > 1)
            prefill_single = sum(rows for _, _, rows in prefill if rows == 1)
            snapshots = []
            for index, (event, decode) in enumerate(study.ENDPOINTS):
                done = index != 0
                clock = prior_forwards + (len(prefill) + decode if done else 0)
                count = counters(clock,
                                 prior_multi + (prefill_multi if done else 0),
                                 prior_single + (prefill_single + decode if done else 0))
                count.update({raw: count[alias] for alias, raw in ALIASES.items()})
                snapshots.append(dict(event=event, decode_forwards=decode,
                                      counters=count,
                                      gpu_layers=layers(clock) if index in (0, 1, 5)
                                      else None))
            group['requests'].append(dict(position=position,
                role='conditioning' if position == 0 else 'probe',
                response_id=f'chatcmpl-auto-{position}', input_tokens=prompt,
                output_tokens=256, trace_path=str(path), validation=validation,
                forwards=forwards, snapshots=snapshots))
            prior_forwards += len(forwards)
            prior_multi += prefill_multi
            prior_single += prefill_single + 255
        result['groups'].append(group)
    return result


def report_fixture():
    manifest = manifest_fixture()
    result = dict(schema=1, mode='baseline', passed=True,
                  status='EXACT_BASELINE_REPRODUCED', failure=None,
                  scope_sha256=SCOPE_SHA, manifest_sha256=SOURCE_SHA,
                  helper_sha256=SOURCE_SHA, execution_plan_sha256=SOURCE_SHA,
                  performance_acceptance=False, requests=[])
    for group in manifest['groups']:
        for request in group['requests']:
            snapshots = request['snapshots']
            endpoints, intervals = [], {}
            for index in range(1, 6):
                snapshot = snapshots[index]
                current = study.subtract(snapshot['counters'], snapshots[0]['counters'])
                endpoints.append(dict(index=index, event=snapshot['event'],
                    decode_forwards=snapshot['decode_forwards'], cumulative=current,
                    observed_cumulative=dict(current),
                    gpu_state_sha256=SOURCE_SHA if index in (1, 5) else None,
                    exact_observed_state_checked=index in (1, 5)))
                intervals[study.INTERVALS[index - 1]] = study.subtract(
                    snapshot['counters'], snapshots[index - 1]['counters'])
            phases = {'prefill': study.subtract(snapshots[1]['counters'],
                                                snapshots[0]['counters']),
                      'decode': study.subtract(snapshots[5]['counters'],
                                               snapshots[1]['counters'])}
            result['requests'].append(dict(group=group['id'], arm=group['arm'],
                position=request['position'], input_tokens=request['input_tokens'],
                output_tokens=256, complete=True, endpoints=endpoints,
                intervals=intervals, phases=phases, bounds={},
                entry_gpu_state_sha256=SOURCE_SHA, entry_exact_checked=True,
                schedule_sha256=SOURCE_SHA,
                partition_counts={'layer_forwards': len(request['forwards']) * 48,
                                  'applied': 0, 'fallback': 0}))
    return result


class SyntheticState:
    """Runner wiring oracle; routing/LRU semantics belong to core contracts."""
    created = []

    def __init__(self, value, policy='physical_slot'):
        self.value = deepcopy(value)
        self.policy = policy
        self.created.append(self)

    def snapshot(self):
        return deepcopy(self.value)

    def resolve(self, needed, decode_phase=False):
        self.value['slot_clock'] += 1
        self.value['slot_ticks'][:10] = [self.value['slot_clock']] * 10
        result = dict.fromkeys(study.COUNTERS, 0)
        result.update(resolve_calls=1, expert_lookups=len(needed), hits=len(needed))
        result['decode_lookups' if decode_phase else 'prefill_lookups'] = len(needed)
        return result


class SyntheticScheduler:
    def __init__(self, helper):
        self.helper_calls = 0

    def chunks(self, trace, allow_partition):
        return [list(range(trace['rows']))], dict(partition_applied=False, fallback=False)


def synthetic_iterator(manifest, bad_tail=False, wrong_forward=False):
    lookup = {r['trace_path']: r for g in manifest['groups'] for r in g['requests']}
    ids = {rows: list(range(10)) * rows for rows in (1, 1024, 8192)}

    def iterate(path, validation):
        request = lookup[path]
        for forward in request['forwards']:
            for layer in range(48):
                row = dict(forward, layer=layer, top_k=10,
                           request_id=request['position'] + 1,
                           topk_ids=ids[forward['rows']])
                if wrong_forward and layer == 0:
                    row['forward_id'] += 1
                yield row
        if bad_tail:
            raise ValueError('synthetic corrupt trailing run-end')
    return iterate


class GPUCacheStudyContracts(unittest.TestCase):
    def test_manifest_accepts_exact_fixed_synthetic_scope(self):
        study.validate_manifest(manifest_fixture(), SCOPE_SHA)

    def test_manifest_rejects_fixed_scope_and_state_dimension_changes(self):
        changes = [
            lambda m: m['groups'].pop(),
            lambda m: m['groups'][0]['requests'].pop(),
            lambda m: m['groups'][0].update(arm='C'),
            lambda m: m['groups'][0]['requests'][0].update(input_tokens=1023),
            lambda m: m['groups'][2]['requests'][0].update(input_tokens=8192),
            lambda m: m['groups'][0]['requests'][0].update(output_tokens=255),
            lambda m: m['groups'][0]['requests'][0]['snapshots'].pop(4),
            lambda m: m['groups'][0]['requests'][0]['snapshots'][0]['gpu_layers'].pop(),
            lambda m: m['groups'][0]['requests'][0]['snapshots'][0]['gpu_layers'][0]
            ['slot_experts'].pop(),
            lambda m: m['groups'][0]['requests'][0]['snapshots'][0]['gpu_layers'][0]
            ['slot_ticks'].pop(),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                value = manifest_fixture()
                change(value)
                with self.assertRaises((ValueError, KeyError)):
                    study.validate_manifest(value, SCOPE_SHA)

    def test_manifest_rejects_provenance_and_trace_contract_changes(self):
        changes = [
            lambda m, r: m.update(scope_sha256='0' * 64),
            lambda m, r: m['source_sha256'].update({r['trace_path']: 'not-a-sha'}),
            lambda m, r: r['validation'].update(path='/different/trace.bin'),
            lambda m, r: r['validation']['metadata'].update(prompt_tokens=42),
            lambda m, r: r['validation']['metadata'].update(request_id=2),
            lambda m, r: r['validation']['metadata'].update(http_id='wrong-request'),
            lambda m, r: r['validation'].update(trace_sha256='0' * 64),
            lambda m, r: r['validation']['manifest'].update(layers=47),
            lambda m, r: r['validation']['manifest'].update(top_k=9),
            lambda m, r: r['validation']['manifest'].update(complete=False),
            lambda m, r: r['validation']['manifest'].update(binary_sha256='0' * 64),
            lambda m, r: r['validation']['summary'].update(decode_rows=254),
            lambda m, r: r['validation']['summary'].update(route_ids=1),
            lambda m, r: r['validation']['bindings'][r['trace_path']]
            .update(sha256='0' * 64),
            lambda m, r: r['validation']['bindings'][r['trace_path']]
            .update(stat=[1, 2, 3]),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                value = manifest_fixture()
                change(value, value['groups'][0]['requests'][0])
                with self.assertRaises((ValueError, KeyError)):
                    study.validate_manifest(value, SCOPE_SHA)

    def test_all_count_fields_and_state_fields_must_match_exactly(self):
        original = counters(2, 10, 1)
        for key in study.COUNTERS:
            with self.subTest(counter=key):
                changed = dict(original)
                changed[key] += 1
                with self.assertRaisesRegex(ValueError, 'COUNTER_MISMATCH'):
                    study.compare_counts(changed, original, 'fixture')
        state = SyntheticState({k: v for k, v in layers(1)[0].items() if k != 'layer'})
        for field in study.FIELDS:
            with self.subTest(state=field):
                expected = [dict(layer=0, **state.snapshot())]
                if isinstance(expected[0][field], list):
                    expected[0][field][0] += 1
                else:
                    expected[0][field] += 1
                with self.assertRaisesRegex(ValueError, 'STATE_MISMATCH'):
                    study.compare_states([state], expected, 'fixture')

    def test_prefix_checks_counts_without_inventing_unrecorded_gpu_state(self):
        request = manifest_fixture()['groups'][0]['requests'][0]
        current = study.subtract(request['snapshots'][2]['counters'],
                                 request['snapshots'][0]['counters'])
        row = dict(group=study.GROUPS[0], position=0, endpoints=[], intervals={})
        with patch.object(study, 'compare_states', side_effect=AssertionError('not captured')):
            study.checkpoint(row, [], request, 2, current,
                             dict.fromkeys(study.COUNTERS, 0), True)
        self.assertFalse(row['endpoints'][0]['exact_observed_state_checked'])
        self.assertIsNone(row['endpoints'][0]['gpu_state_sha256'])
        current['loads'] += 1
        with self.assertRaisesRegex(ValueError, 'COUNTER_MISMATCH'):
            study.checkpoint(row, [], request, 2, current,
                             dict.fromkeys(study.COUNTERS, 0), True)

    def test_manifest_rejects_gpu_state_at_unrecorded_prefix(self):
        value = manifest_fixture()
        value['groups'][0]['requests'][0]['snapshots'][2]['gpu_layers'] = layers(1)
        with self.assertRaises(ValueError):
            study.validate_manifest(value, SCOPE_SHA)

    def test_baseline_gate_requires_all_identity_and_complete_evidence(self):
        baseline = report_fixture()
        study.validate_baseline(baseline, SCOPE_SHA, SOURCE_SHA,
                                SOURCE_SHA, SOURCE_SHA)
        changes = [
            lambda b: b.update(passed=False),
            lambda b: b.update(mode='candidate'),
            lambda b: b.update(scope_sha256='0' * 64),
            lambda b: b.update(manifest_sha256='0' * 64),
            lambda b: b.update(helper_sha256='0' * 64),
            lambda b: b.update(execution_plan_sha256='0' * 64),
            lambda b: b['requests'].pop(),
            lambda b: b['requests'][-1].update(complete=False),
            lambda b: b['requests'][-1].update(entry_exact_checked=False),
            lambda b: b['requests'][-1].update(schedule_sha256='not-a-sha'),
            lambda b: b['requests'][-1].pop('schedule_sha256'),
            lambda b: b['requests'][-1]['phases'].pop('decode'),
            lambda b: b['requests'][-1]['intervals'].pop('decode_32_end'),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                value = deepcopy(baseline)
                change(value)
                with self.assertRaises((ValueError, KeyError)):
                    study.validate_baseline(value, SCOPE_SHA, SOURCE_SHA,
                                            SOURCE_SHA, SOURCE_SHA)

    def test_candidate_cannot_use_failed_baseline(self):
        baseline = report_fixture()
        candidate = deepcopy(baseline)
        baseline['passed'] = False
        with self.assertRaises(ValueError):
            study.candidate_decision(candidate, baseline)

    def test_candidate_equal_work_does_not_meet_strict_prefill_gain(self):
        baseline = report_fixture()
        candidate = deepcopy(baseline)
        study.candidate_decision(candidate, baseline)
        self.assertEqual(candidate['offline_work_decision'], 'NO_GO_OFFLINE_WORK')
        self.assertFalse(candidate['offline_work_gates']['any_prefill_strict_reduction'])

    def test_candidate_reduction_is_offline_consideration_only(self):
        baseline = report_fixture()
        baseline['requests'][0]['phases']['prefill']['loads'] = 1
        candidate = deepcopy(baseline)
        candidate['requests'][0]['phases']['prefill']['loads'] = 0
        study.candidate_decision(candidate, baseline)
        self.assertEqual(candidate['offline_work_decision'], 'GO_FOR_RUNTIME_CONSIDERATION')
        self.assertFalse(candidate['performance_acceptance'])

    def test_earlier_prefill_gain_cannot_cancel_later_decode_regression(self):
        baseline = report_fixture()
        baseline['requests'][0]['phases']['prefill']['loads'] = 100
        candidate = deepcopy(baseline)
        candidate['requests'][0]['phases']['prefill']['loads'] = 0
        candidate['requests'][-1]['phases']['decode']['loads'] = 1
        study.candidate_decision(candidate, baseline)
        self.assertEqual(candidate['offline_work_decision'], 'NO_GO_OFFLINE_WORK')
        self.assertTrue(candidate['offline_work_gates']['any_prefill_strict_reduction'])
        self.assertFalse(candidate['offline_work_gates']['all_decode_nonincreasing'])

    def test_candidate_cannot_change_pair_or_schedule_work(self):
        baseline = report_fixture()
        for change in ('pair', 'calls', 'lookups', 'schedule', 'fallback', 'complete'):
            with self.subTest(change=change):
                candidate = deepcopy(baseline)
                row = candidate['requests'][-1]
                if change == 'pair':
                    row['position'] = 0
                elif change == 'schedule':
                    row['schedule_sha256'] = '0' * 64
                elif change == 'fallback':
                    row['partition_counts']['fallback'] += 1
                elif change == 'complete':
                    row['complete'] = False
                else:
                    key = 'resolve_calls' if change == 'calls' else 'expert_lookups'
                    row['phases']['decode'][key] += 1
                with self.assertRaises(ValueError):
                    study.candidate_decision(candidate, baseline)

    def synthetic_patches(self, manifest, **iterator_options):
        stack = ExitStack()
        stack.enter_context(patch.object(study, 'GpuCacheState', SyntheticState))
        stack.enter_context(patch.object(study, 'Scheduler', SyntheticScheduler))
        stack.enter_context(patch.object(study, 'iter_layers',
                                         synthetic_iterator(manifest, **iterator_options)))
        return stack

    def test_chain_keeps_service_state_and_all_singleton_tail_boundaries(self):
        manifest = manifest_fixture()
        requests = [r for g in manifest['groups'] for r in g['requests']]
        for baseline in (True, False):
            with self.subTest(baseline=baseline):
                SyntheticState.created = []
                report = dict(requests=[])
                with self.synthetic_patches(manifest):
                    study.replay(manifest, '/synthetic/helper', baseline, report)
                self.assertEqual(len(SyntheticState.created), 4 * 48)
                self.assertEqual(len(report['requests']), 16)
                self.assertTrue(all(row['complete'] for row in report['requests']))
                self.assertEqual(sum(len(row['intervals']) for row in report['requests']), 80)
                for row, request in zip(report['requests'], requests):
                    expected_entry = [{key: layer[key] for key in study.FIELDS}
                                      for layer in request['snapshots'][0]['gpu_layers']]
                    self.assertEqual(row['entry_gpu_state_sha256'], study.digest(expected_entry))
                    self.assertEqual(row['phases']['decode']['resolve_calls'], 255 * 48)
                    self.assertEqual(row['intervals']['decode_32_end']['resolve_calls'], 223 * 48)
                    if row['input_tokens'] == 8193:
                        self.assertEqual(row['phases']['prefill']['decode_lookups'], 480)
                        self.assertEqual(row['phases']['prefill']['prefill_lookups'], 8192 * 480)

    def test_wrong_forward_identity_rejects_before_complete_request(self):
        manifest = manifest_fixture()
        report = dict(requests=[])
        with self.synthetic_patches(manifest, wrong_forward=True):
            with self.assertRaises(ValueError):
                study.replay(manifest, '/synthetic/helper', True, report)
        self.assertFalse(report['requests'][0]['complete'])

    def test_corrupt_iterator_tail_is_retained_failure_not_published_pass(self):
        manifest = manifest_fixture()
        workspace = Path(study.__file__).resolve().parents[2]
        temporary_root = workspace / '.q4t-work'
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='gpu-study-contract-',
                                         dir=temporary_root) as folder:
            root = Path(folder)
            paths = {name: root / (name + '.json') for name in
                     ('manifest', 'scope', 'execution-plan', 'helper', 'output')}
            required = [Path(study.__file__).resolve(), paths['helper'],
                        *[workspace / name for name in (
                            'tools/trace/gpu_cache_replay.py',
                            'tools/trace/offload_trace.py', 'tools/trace/analyze.py',
                            'tools/trace/gpu_cache_schedule.cpp',
                            'include/q4t/model/moe_partition.h', 'src/model/moe.cu',
                            'src/quant/moe_residency.cpp',
                            'include/q4t/quant/moe_residency.h')]]
            values = {paths['manifest']: manifest,
                      paths['scope']: {'scope': 'source_faithful_gpu_cache_replay_v1'},
                      paths['execution-plan']: {
                          'scope_sha256': SCOPE_SHA,
                          'manifest_sha256': SOURCE_SHA,
                          'source_sha256': {str(path): SOURCE_SHA for path in required}}}
            argv = ['run_gpu_cache_study.py', '--mode', 'baseline']
            for name, path in paths.items():
                argv.extend(['--' + name, str(path)])
                if name != 'output':
                    argv.extend(['--' + name + '-sha256',
                                 SCOPE_SHA if name == 'scope' else SOURCE_SHA])
            with self.synthetic_patches(manifest, bad_tail=True), ExitStack() as stack:
                stack.enter_context(patch.object(sys, 'argv', argv))
                stack.enter_context(patch.object(study, 'read_bound',
                                                  side_effect=lambda p, _: values[Path(p)]))
                stack.enter_context(patch.object(study, 'sha', return_value=SOURCE_SHA))
                # Dependency admission is separately exercised; this targets
                # terminal iterator error propagation and output publication.
                stack.enter_context(patch.object(study, 'validate_manifest', return_value=None))
                stack.enter_context(redirect_stdout(io.StringIO()))
                returncode = study.main()
            retained = json.loads(paths['output'].read_text())
            self.assertEqual(returncode, 1)
            self.assertFalse(retained['passed'])
            self.assertEqual(retained['status'], 'FAIL_RETAINED')
            self.assertIn('synthetic corrupt trailing run-end', retained['failure'])
            self.assertFalse(retained['requests'][0]['complete'])

    def test_bound_read_rejects_mutated_bytes(self):
        workspace = Path(study.__file__).resolve().parents[2]
        temporary_root = workspace / '.q4t-work'
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='gpu-study-contract-',
                                         dir=temporary_root) as folder:
            path = Path(folder) / 'source.json'
            path.write_text('{"value": 1}\n')
            original = study.sha(path)
            self.assertEqual(study.read_bound(path, original), {'value': 1})
            path.write_text('{"value": 2}\n')
            with self.assertRaisesRegex(ValueError, 'SHA differs'):
                study.read_bound(path, original)

    def test_cli_refuses_overwriting_retained_evidence(self):
        workspace = Path(study.__file__).resolve().parents[2]
        temporary_root = workspace / '.q4t-work'
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='gpu-study-contract-',
                                         dir=temporary_root) as folder:
            output = Path(folder) / 'retained.json'
            payload = b'{"old_failure": true}\n'
            output.write_bytes(payload)
            argv = ['run_gpu_cache_study.py', '--mode', 'baseline',
                    '--output', str(output)]
            for name in ('manifest', 'scope', 'helper', 'execution-plan'):
                argv.extend(['--' + name, str(Path(folder) / (name + '.json')),
                             '--' + name + '-sha256', SOURCE_SHA])
            with patch.object(sys, 'argv', argv):
                with self.assertRaisesRegex(ValueError, 'overwriting'):
                    study.main()
            self.assertEqual(output.read_bytes(), payload)


if __name__ == '__main__':
    unittest.main()
