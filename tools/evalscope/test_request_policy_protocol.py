"""Policy execution, history acceptance and bounded cold setup contracts."""
import copy
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from offload_policy import (BASE_ENVIRONMENT, check_policy_protocol,
                            policy_environment)
from request_policy_protocol import (SEQUENCE, all_layer_partition_evidence,
                                     decode_log_path_evidence,
                                     request_path_evidence,
                                     sequence_metrics, sequence_output_evidence,
                                     sequence_plan)
from run_budget_experiment import (experiment_environment, prepare_cold_payload,
                                  selected_performance_plan, parse_binary_switch)

ROOT = Path(__file__).resolve().parents[2]


class RequestEnvironmentContracts(unittest.TestCase):
    def test_new_axis_clears_inherited_experiments_and_binds_master(self):
        inherited = {'PATH': '/bin', 'Q4T_MOE_PARTITION': '1',
                     'Q4T_MOE_REQUEST_PARTITION': '1', 'Q4T_FP8_BAD': '1'}
        off = experiment_environment(0, inherited, 0, 'request-partition',
                                     request_partition=0)
        self.assertEqual(off['PATH'], '/bin')
        self.assertNotIn('Q4T_FP8_BAD', off)
        self.assertEqual(off['Q4T_MOE_PARTITION'], '0')
        self.assertEqual(off['Q4T_MOE_REQUEST_PARTITION'], '0')
        for state in (0, 1):
            env = policy_environment(0, state, 'request-partition',
                                     request_partition=state)
            protocol = dict(policy_axis='request-partition', partition=state,
                            request_partition=state, chunk_order=0,
                            effective_environment=env)
            check_policy_protocol(protocol, state, 'request-partition')
            with self.assertRaises(ValueError):
                check_policy_protocol(protocol, 1 - state, 'request-partition')

    def test_conflicting_axis_state_and_diagnostics_rejected(self):
        for args in ((0, 0, 'partition', False, 1),
                     (0, 0, 'request-partition', False, 1),
                     (0, 1, 'request-partition', False, 0),
                     (1, 1, 'request-partition', False, 1),
                     (0, 1, 'request-partition', True, 1),
                     (0, 1, 'request-partition', False, True)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                policy_environment(*args)

    def test_old_environment_and_six_tier_selection_remain_explicit(self):
        self.assertEqual(policy_environment(1),
                         {**BASE_ENVIRONMENT, 'Q4T_MOE_CHUNK_ORDER': '1'})
        self.assertEqual(policy_environment(0, 1, 'partition'),
                         {**BASE_ENVIRONMENT, 'Q4T_MOE_CHUNK_ORDER': '0',
                          'Q4T_MOE_PARTITION': '1'})
        matrix = selected_performance_plan(
            'performance', '1024,4096,8192,45056,204800,261887')
        self.assertTrue(matrix['full_offload_matrix_requested'])
        self.assertFalse(matrix['partial_offload_matrix'])


class HistoryContracts(unittest.TestCase):
    def rows(self):
        return [dict(r, ttft=10., decode_tps=7.)
                for r in sequence_plan()['requests']]

    def test_frozen_order_and_independent_positions(self):
        plan = sequence_plan()
        self.assertEqual([r['input_tokens'] for r in plan['requests']],
                         list(SEQUENCE) * 3)
        self.assertEqual(len({r['case'] for r in plan['requests']}), 21)
        self.assertTrue(plan['partial'])
        self.assertFalse(plan['full_offload_matrix_requested'])
        report = sequence_metrics(self.rows(), self.rows())
        self.assertTrue(report['passed'])
        self.assertEqual(len(report['positions']), 7)
        self.assertFalse(report['performance_acceptance'])

    def test_one_short_decode_failure_cannot_hide_behind_long_gains(self):
        before, after = self.rows(), self.rows()
        for row in after:
            row['ttft'] = 1.
            row['decode_tps'] = 10.
        after[6]['decode_tps'] = 6.99999
        report = sequence_metrics(before, after)
        self.assertFalse(report['passed'])
        self.assertTrue(report['positions'][1]['passed'])
        self.assertFalse(report['positions'][6]['checks']['decode'])

    def test_same_prompt_output_drift_between_rounds_or_positions_fails(self):
        rows = []
        for item in sequence_plan()['requests']:
            length = item['input_tokens']
            rows.append(dict(actual_input=length, actual_output=256,
                             finish=['length'], success=1, text=str(length),
                             prompt_sha256=hashlib.sha256(str(length).encode()).hexdigest()))
        self.assertTrue(sequence_output_evidence(rows)['passed'])
        for index in (6, 7, 20):
            changed = copy.deepcopy(rows)
            changed[index]['text'] += 'changed'
            with self.assertRaises(ValueError):
                sequence_output_evidence(changed)

    def test_frozen_first_later_and_complete_sample_gates(self):
        for index in (0, 7, 20):
            after = self.rows()
            after[index]['ttft'] = 10.001
            self.assertFalse(sequence_metrics(self.rows(), after)['passed'])
        for modify in (lambda r: r.pop(), lambda r: r.reverse(),
                       lambda r: r[0].update(round=2),
                       lambda r: r[0].update(ttft=float('nan'))):
            after = self.rows()
            modify(after)
            with self.assertRaises(ValueError):
                sequence_metrics(self.rows(), after)
        with self.assertRaises(ValueError):
            sequence_plan(45056)


class RequestExecutionContracts(unittest.TestCase):
    def records(self):
        requests = [dict(response_id=f'request-{i}', actual_input=length,
                         response_id_valid=True)
                    for i, length in enumerate((8192, 8193, 16385, 1024))]
        lines = []
        for request in requests:
            total = request['actual_input']
            policy = int(total > 8192)
            for base in range(0, total, 8192):
                tokens = min(8192, total - base)
                applied = int(policy and tokens > 1)
                reason = ('request_legacy' if not policy else
                          'candidate' if applied else 'prefill_singleton')
                lines.append('[q4t][request_partition] '
                    f"request={request['response_id']} input_tokens={total} "
                    f'active=1 partition={policy} base={base} tokens={tokens} layer=0 '
                    f'applied={applied} fallback=0 reason={reason}')
        return requests, '\n'.join(lines)

    def test_threshold_total_and_singleton_tail_propagation(self):
        requests, log = self.records()
        report = request_path_evidence(log, requests, 1)
        self.assertEqual(report['execution_records'], 7)
        self.assertEqual(report['selected_legacy_requests'], 2)
        self.assertEqual(report['singleton_tail_chunks'], 2)
        self.assertTrue(report['runtime_eligible'])
        self.assertFalse(report['numerical_acceptance'])

    def test_missing_extra_reordered_or_fallback_execution_fails(self):
        requests, log = self.records()
        for changed in ('\n'.join(log.splitlines()[:-1]), log + '\n' + log,
                        '\n'.join(reversed(log.splitlines())),
                        log.replace('input_tokens=8193', 'input_tokens=8192'),
                        log.replace('reason=prefill_singleton', 'reason=decode'),
                        log.replace('base=8192', 'base=0'),
                        log.replace('fallback=0', 'fallback=1'),
                        log.replace('partition=1', 'partition=0')):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                request_path_evidence(changed, requests, 1)

    def test_ambiguous_http_identity_and_disabled_path_rejected(self):
        requests, log = self.records()
        for change in (dict(response_id_valid=False),
                       dict(response_id=requests[0]['response_id']),
                       dict(actual_input=8192)):
            modified = copy.deepcopy(requests)
            modified[1].update(change)
            with self.assertRaises(ValueError):
                request_path_evidence(log, modified, 1)
        with self.assertRaises(ValueError):
            request_path_evidence('', requests, 0)
        with self.assertRaises(ValueError):
            request_path_evidence(log, requests, 0)
        baseline = []
        for request in requests:
            length = request['actual_input']
            for base in range(0, length, 8192):
                tokens = min(8192, length - base)
                baseline.append('[q4t][request_partition] '
                    f"request={request['response_id']} input_tokens={length} "
                    f'active=0 partition=0 base={base} tokens={tokens} layer=0 '
                    'applied=0 fallback=0 reason=global_legacy')
        self.assertTrue(request_path_evidence('\n'.join(baseline), requests, 0)
                        ['runtime_eligible'])


class AllLayerContracts(unittest.TestCase):
    def fixture(self):
        rows = [dict(response_id='long', response_id_valid=True,
                     actual_input=8193, actual_output=2),
                dict(response_id='short', response_id_valid=True,
                     actual_input=8192, actual_output=2)]
        events = []
        def marker(name, total, policy, base, tokens, applied, reason):
            events.append('[q4t][request_partition] '
                f'request={name} input_tokens={total} active=1 partition={policy} '
                f'base={base} tokens={tokens} layer=0 applied={applied} '
                f'fallback=0 reason={reason}')
        def layers(tokens, reason):
            applied = int(tokens > 1)
            for layer in range(48):
                events.append('[q4t][residency][partition] '
                    f'layer={layer} T={tokens} policy=min_new_csr_v1 requested=1 '
                    f'applied={applied} fallback=0 reason={reason} '
                    f'chunks=1 singleton_chunks={int(tokens == 1)} '
                    f'work_used={applied} work_budget={32*tokens*10*applied} '
                    'metadata_bytes=0')
        marker('long', 8193, 1, 0, 8192, 1, 'candidate')
        layers(8192, 'candidate')
        marker('long', 8193, 1, 8192, 1, 0, 'prefill_singleton')
        layers(1, 'prefill_singleton')
        layers(1, 'decode')
        marker('short', 8192, 0, 0, 8192, 0, 'request_legacy')
        layers(1, 'decode')
        return rows, '\n'.join(events)

    def test_all_48_layers_and_real_phase_singleton_are_separate(self):
        rows, log = self.fixture()
        result = all_layer_partition_evidence(log, rows, 1)
        self.assertEqual(result['candidate_prefill_layer_forwards'], 48)
        self.assertEqual(result['singleton_prefill_layer_forwards'], 48)
        self.assertEqual(result['decode_layer_forwards'], 96)
        self.assertTrue(result['runtime_eligible'])

    def test_missing_layer_wrong_phase_or_extra_decode_fails(self):
        rows, log = self.fixture()
        for changed in ('\n'.join(log.splitlines()[:-1]),
                        log.replace('layer=47 T=8192', 'layer=46 T=8192'),
                        log.replace('fallback=0 reason=candidate',
                                    'fallback=1 reason=candidate'),
                        log.replace('T=1 policy=min_new_csr_v1 requested=1 '
                                    'applied=0 fallback=0 reason=prefill_singleton',
                                    'T=1 policy=min_new_csr_v1 requested=1 '
                                    'applied=0 fallback=0 reason=decode'),
                        log + '\n' + log.splitlines()[-1]):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                all_layer_partition_evidence(changed, rows, 1)
        rows[0]['actual_output'] += 1
        with self.assertRaises(ValueError):
            all_layer_partition_evidence(log, rows, 1)


class DecodeLogEnvironmentContracts(unittest.TestCase):
    def test_three_states_are_explicit_and_inherited_flags_are_removed(self):
        for state, quiet in ((0, 0), (1, 0), (1, 1)):
            env = experiment_environment(0, {
                'PATH': '/bin', 'Q4T_FP8_BAD': '1',
                'Q4T_MOE_DECODE_PARTITION_LOG_QUIET': 'unexpected'},
                state, 'request-partition-log', request_partition=state,
                decode_partition_log_quiet=quiet)
            self.assertEqual(env.pop('PATH'), '/bin')
            self.assertNotIn('Q4T_FP8_BAD', env)
            self.assertEqual(env['Q4T_MOE_DECODE_PARTITION_LOG_QUIET'], str(quiet))
            protocol = dict(policy_axis='request-partition-log', partition=state,
                            request_partition=state, chunk_order=0,
                            decode_partition_log_quiet=quiet,
                            effective_environment=env)
            check_policy_protocol(protocol, state, 'request-partition-log', quiet)
            for value in (1 - quiet, True, '1', None):
                changed = dict(protocol, decode_partition_log_quiet=value)
                with self.subTest(value=value), self.assertRaises(ValueError):
                    check_policy_protocol(changed, state,
                                          'request-partition-log', quiet)

    def test_old_axes_do_not_gain_a_new_environment_key(self):
        for axis, state in (('chunk-order', 0), ('partition', 1),
                            ('request-partition', 1)):
            request = state if axis == 'request-partition' else 0
            env = policy_environment(0, state, axis, request_partition=request)
            self.assertNotIn('Q4T_MOE_DECODE_PARTITION_LOG_QUIET', env)
            with self.assertRaises(ValueError):
                policy_environment(0, state, axis, request_partition=request,
                                   decode_partition_log_quiet=1)

    def test_unknown_axis_illegal_mode_and_unmatched_request_are_rejected(self):
        for value in (-1, 2, True, '1', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy_environment(0, 1, 'request-partition-log',
                    request_partition=1, decode_partition_log_quiet=value)
        for partition, request, order in ((0, 0, 0), (0, 1, 0), (1, 0, 0),
                                          (1, 1, 1)):
            with self.assertRaises(ValueError):
                policy_environment(order, partition, 'request-partition-log',
                    request_partition=request, decode_partition_log_quiet=1)
        with self.assertRaises(ValueError):
            policy_environment(0, 1, 'unknown', request_partition=1)
        with self.assertRaises(ValueError):
            check_policy_protocol({}, 0, 'unknown')

    def test_cli_switch_accepts_only_literal_zero_or_one(self):
        self.assertEqual(parse_binary_switch('0'), 0)
        self.assertEqual(parse_binary_switch('1'), 1)
        for value in ('01', '+1', ' 1 ', 'true', '', '-1', '2', '1.0'):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    parse_binary_switch(value)


class DecodeLogExecutionContracts(unittest.TestCase):
    def fixture(self, state=1, quiet=1):
        rows = [dict(response_id=f'request-{i}', response_id_valid=True,
                     actual_input=length, actual_output=2)
                for i, length in enumerate((8193, 16385, 8192, 1024, 1))]
        events = ['[q4t][residency] '
                  f'decode_partition_log_quiet={quiet} '
                  'scope=explicit_single_decode']

        def layers(tokens, reason, partition):
            applied = int(reason == 'candidate')
            for layer in range(48):
                if partition and not (quiet and reason == 'decode'):
                    events.append('[q4t][residency][partition] '
                        f'layer={layer} T={tokens} policy=min_new_csr_v1 '
                        f'requested=1 applied={applied} fallback=0 '
                        f'reason={reason} chunks=1 '
                        f'singleton_chunks={int(tokens == 1)} '
                        f'work_used={applied} '
                        f'work_budget={32 * tokens * 10 * applied} '
                        'metadata_bytes=0')
                order = 'min_new_partition' if applied else 'original_partition'
                events.append('[q4t][residency][diag] '
                    f'layer={layer} T={tokens} D=256 chunks=1 max_distinct=10 '
                    f'resident_before=256 overlap= new= chunk_order=0 '
                    f'diag_order={order} partition={partition}')

        for row in rows:
            length = row['actual_input']
            partition = int(state and length > 8192)
            for base in range(0, length, 8192):
                tokens = min(8192, length - base)
                applied = int(partition and tokens > 1)
                reason = ('global_legacy' if not state else
                          'request_legacy' if not partition else
                          'candidate' if applied else 'prefill_singleton')
                events.append('[q4t][request_partition] '
                    f"request={row['response_id']} input_tokens={length} "
                    f'active={state} partition={partition} base={base} '
                    f'tokens={tokens} layer=0 applied={applied} '
                    f'fallback=0 reason={reason}')
                layers(tokens, reason, partition)
            layers(1, 'decode', state)
        return rows, '\n'.join(events)

    def test_all_three_arms_observe_decode_even_when_partition_log_is_quiet(self):
        for state, quiet in ((0, 0), (1, 0), (1, 1)):
            rows, log = self.fixture(state, quiet)
            result = decode_log_path_evidence(log, rows, state, quiet)
            self.assertTrue(result['runtime_eligible'])
            self.assertEqual(result['decode_layer_forwards'], 5 * 48)
            self.assertEqual(result['forward_diag_records'], 13 * 48)
            self.assertEqual(result['partition_decode_log_records'],
                             5 * 48 if state and not quiet else 0)
            self.assertEqual(result['singleton_prefill_layer_forwards'],
                             2 * 48 if state else 0)
            self.assertFalse(result['numerical_acceptance'])

    def test_missing_extra_reordered_and_malformed_diagnostics_fail(self):
        rows, log = self.fixture()
        lines = log.splitlines()
        indices = [i for i, line in enumerate(lines)
                   if '[residency][diag]' in line]
        reordered = lines.copy()
        a, b = indices[0], indices[1]
        reordered[a], reordered[b] = reordered[b], reordered[a]
        for changed in ('\n'.join(lines[:indices[-1]] + lines[indices[-1] + 1:]),
                        log + '\n' + lines[indices[-1]],
                        '\n'.join(reordered),
                        log.replace('T=1 D=256', 'tokens=1 D=256', 1),
                        log.replace('D=256 chunks=1', 'D=0 chunks=1', 1),
                        log.replace('overlap= new=', 'overlap=1 new=', 1)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                decode_log_path_evidence(changed, rows, 1, 1)

    def test_quiet_does_not_allow_missing_or_mislabelled_singleton_prefill(self):
        rows, log = self.fixture()
        lines = log.splitlines()
        index = next(i for i, line in enumerate(lines)
                     if 'reason=prefill_singleton chunks=' in line)
        for changed in ('\n'.join(lines[:index] + lines[index + 1:]),
                        log.replace('reason=prefill_singleton chunks=',
                                    'reason=decode chunks=', 1),
                        log.replace('reason=prefill_singleton',
                                    'reason=decode', 1)):
            with self.assertRaises(ValueError):
                decode_log_path_evidence(changed, rows, 1, 1)

    def test_unremoved_decode_log_and_wrongly_removed_control_log_fail(self):
        rows, loud = self.fixture(1, 0)
        _, quiet = self.fixture(1, 1)
        with self.assertRaises(ValueError):
            decode_log_path_evidence(loud.replace('log_quiet=0', 'log_quiet=1'),
                                     rows, 1, 1)
        with self.assertRaises(ValueError):
            decode_log_path_evidence(quiet.replace('log_quiet=1', 'log_quiet=0'),
                                     rows, 1, 0)
        # The historical parser must never silently accept the quiet evidence.
        with self.assertRaises(ValueError):
            all_layer_partition_evidence(quiet, rows, 1)
        self.assertTrue(all_layer_partition_evidence(loud, rows, 1)
                        ['runtime_eligible'])

    def test_missing_duplicate_wrong_activation_and_illegal_modes_fail(self):
        rows, log = self.fixture()
        activation, rest = log.split('\n', 1)
        for changed in (rest, log + '\n' + activation,
                        log.replace('log_quiet=1', 'log_quiet=0'),
                        log.replace('log_quiet=1', 'log_quiet=2'),
                        log.replace('scope=explicit_single_decode',
                                    'scope=all_singletons')):
            with self.assertRaises(ValueError):
                decode_log_path_evidence(changed, rows, 1, 1)
        for mode in (None, True, -1, 2, '1'):
            with self.assertRaises(ValueError):
                decode_log_path_evidence(log, rows, 1, mode)

    def test_http_usage_or_request_order_cannot_invent_decode_evidence(self):
        rows, log = self.fixture()
        for value in (1, 3, True):
            changed = copy.deepcopy(rows)
            changed[0]['actual_output'] = value
            with self.assertRaises(ValueError):
                decode_log_path_evidence(log, changed, 1, 1)
        with self.assertRaises(ValueError):
            decode_log_path_evidence(log, rows[::-1], 1, 1)

    def test_partition_dimension_and_diagnostic_phase_must_agree(self):
        rows, log = self.fixture()
        for changed in (log.replace('reason=candidate chunks=1',
                                    'reason=candidate chunks=2', 1),
                        log.replace('work_used=1', 'work_used=0', 1),
                        log.replace('diag_order=min_new_partition',
                                    'diag_order=original_partition', 1),
                        log.replace('diag_order=original_partition partition=1',
                                    'diag_order=original_partition partition=0', 1),
                        log + '\n[q4t][residency][diag] layer=0 oi=63 t=63 '
                        'INVARIANT BROKEN book=1 actual=2 D=256'):
            with self.assertRaises(ValueError):
                decode_log_path_evidence(changed, rows, 1, 1)


class ColdPreparationContracts(unittest.TestCase):
    def observation(self, resident, complete=True):
        return {'complete_file_set_observed': complete,
                'files': [{'path': '/model/a.safetensors',
                           'resident_bytes': resident}]}

    def run_preparation(self, observations, rounds, errors=None):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work')
        self.addCleanup(temporary.cleanup)
        out = Path(temporary.name)
        with patch('run_budget_experiment.clear_target_cache',
                   return_value=errors or []) as clear, \
             patch('run_budget_experiment.observe_files',
                   side_effect=observations) as observe:
            gate = prepare_cold_payload([Path('/model/a.safetensors')],
                                        out, rounds)
        return gate, out, clear.call_count, observe.call_count

    def test_second_setup_round_retains_initial_nonzero_and_requires_zero(self):
        gate, out, clears, observations = self.run_preparation(
            [self.observation(16384), self.observation(0)], 2)
        self.assertTrue(gate['cold_payload_established'])
        self.assertEqual((clears, observations), (2, 2))
        first = json.loads((out / 'cache-advice-round-01.json').read_text())
        self.assertEqual(first['payload_resident_bytes'], 16384)
        self.assertFalse(first['cold_payload_established'])
        self.assertFalse(gate['inference_retry'])

    def test_no_third_round_no_unknown_admission_and_no_redundant_round(self):
        for observations, expected, calls in (
                ([self.observation(4096)] * 2, False, 2),
                ([self.observation(0, False)] * 2, False, 2),
                ([self.observation(0)], True, 1)):
            gate, _, clear_count, observe_count = self.run_preparation(
                observations, 2)
            self.assertEqual(gate['cold_payload_established'], expected)
            self.assertEqual((clear_count, observe_count), (calls, calls))

    def test_advice_error_stops_without_service_or_second_round(self):
        gate, _, clears, observations = self.run_preparation(
            [self.observation(0)], 2, [{'error': 'permission'}])
        self.assertFalse(gate['cold_payload_established'])
        self.assertEqual((clears, observations), (1, 1))

    def test_legacy_single_round_output_shape_unchanged(self):
        gate, out, clears, observations = self.run_preparation(
            [self.observation(4096)], 1)
        self.assertEqual((clears, observations), (1, 1))
        self.assertNotIn('rounds', gate)
        self.assertFalse((out / 'cache-advice-round-01.json').exists())
        self.assertFalse(gate['cold_payload_established'])


if __name__ == '__main__':
    unittest.main()
