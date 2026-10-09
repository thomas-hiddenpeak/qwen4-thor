"""Finite host-only contracts for cycle schema/accounting; no model inference."""
import copy
import json
import math
import unittest

import analyze_mtp_cycle_timing as cycle


def fixture(clipped=False):
    counts = [1, 2, 3, 4]
    maximum = 9 if clipped else 10
    response = {'response_id': 'fixture_cycle_1', 'actual_input': 1024,
                'actual_output': maximum, 'finish': ['length'],
                'actual_path': {'mtp_steps': '4'}}
    steps, position, delivered = [], 1024, 0
    for index, count in enumerate(counts):
        start = 100.0 + index * 25.0
        marks = [{'name': name, 'at_ms': start + i}
                 for i, name in enumerate(cycle.STEP_MARKS)]
        details = []
        for phase in cycle.PHASES:
            for name in cycle.DETAILS:
                calls = cycle.EXPECTED_DETAIL_CALLS[phase][name]
                details.append({'phase': phase, 'name': name,
                                'calls': calls, 'host_ms': 0.001 * calls})
        generated = min(count, maximum - delivered)
        steps.append({'index': index, 'position': position, 'k': 3,
                      'accepted_drafts': count - 1, 'returned': count,
                      'verify_rows': 4, 'extend_rows': count, 'generated': generated,
                      'token_piece_writes': generated, 'nonempty_writes': generated,
                      'marks': marks, 'details': details})
        position += count
        delivered += generated
    values = [1.0, 2.0, 50.0, 80.0, 116.1, 116.2, 200.0]
    raw = {'schema_version': 1, 'response_id': response['response_id'],
           'prompt_tokens': 1024, 'max_tokens': maximum, 'k': 3, 'max_seq': 1,
           'text_only': True, 'complete': True, 'valid': True, 'error': '',
           'success': True, 'plain_tail': False, 'finish_reason': 'length',
           'fallback': 'none', 'generated': maximum, 'mtp_steps': 4,
           'step_capacity': maximum, 'diagnostic_only': True,
           'host_origin': 'handle_chat_entry', 'unit': 'ms', 'clock': 'steady_clock',
           'contains_cuda_events': False, 'host_details_additive': False,
           'setup_host_ms': 1.0, 'report_host_ms': 0.5,
           'request_marks': [{'name': n, 'at_ms': v} for n, v in zip(cycle.REQUEST_MARKS, values)],
           'steps': steps}
    return raw, response


class CycleContracts(unittest.TestCase):
    def reject(self, mutate):
        raw, response = fixture()
        mutate(raw, response)
        with self.assertRaises((ValueError, KeyError, TypeError)):
            cycle.validate_cycle(raw, response)

    def test_valid_all_acceptance_bins(self):
        raw, row = fixture()
        result = cycle.validate_cycle(raw, row)
        self.assertEqual(result['accepted_drafts_histogram'], [1, 1, 1, 1])
        self.assertEqual(result['accepted_drafts_total'], 6)
        self.assertEqual(result['generated'], 10)
        self.assertEqual(result['engine_returned'], 10)
        self.assertEqual(result['quota_clipped'], 0)
        self.assertEqual(result['verify_rows_per_generated_token'], 1.6)

    def test_final_quota_clip_separate_denominators(self):
        raw, row = fixture(clipped=True)
        result = cycle.validate_cycle(raw, row)
        self.assertEqual(result['engine_returned'], 10)
        self.assertEqual(result['generated'], 9)
        self.assertEqual(result['quota_clipped'], 1)
        self.assertNotEqual(result['cost_per_generated_token_ms'], result['cost_per_engine_returned_token_ms'])

    def test_detail_not_added_to_parent(self):
        raw, row = fixture()
        first = cycle.validate_cycle(raw, row)
        for item in raw['steps'][0]['details']:
            if item['calls']:
                item['host_ms'] = 0.9
        second = cycle.validate_cycle(raw, row)
        self.assertEqual(first['sum_phase_host_ms'], second['sum_phase_host_ms'])
        self.assertFalse(second['nested_details_added_to_parent'])

    def test_first_step_and_steady_are_separate(self):
        raw, row = fixture()
        result = cycle.validate_cycle(raw, row)
        self.assertEqual(result['first_step']['index'], 0)
        self.assertEqual(result['steady_step_count'], 3)
        self.assertEqual(result['steady_cost_per_generated_token_ms']['draft'], 3 / 9)

    def test_position_advances_returned_after_clip(self):
        raw, row = fixture(clipped=True)
        result = cycle.validate_cycle(raw, row)
        self.assertEqual(result['last_step']['position'], 1030)
        self.assertEqual(result['last_step']['engine_returned'], 4)
        self.assertEqual(result['last_step']['generated'], 3)

    def test_nonempty_is_not_usage(self):
        raw, row = fixture()
        raw['steps'][1]['nonempty_writes'] = 0
        result = cycle.validate_cycle(raw, row)
        self.assertEqual(result['generated'], 10)

    def test_missing_acceptance_bins_not_invented(self):
        raw, row = fixture()
        raw['steps'] = [copy.deepcopy(raw['steps'][-1])]
        raw['steps'][0].update(index=0, position=1024)
        raw['steps'][0]['marks'] = [{'name': n, 'at_ms': 100 + i} for i, n in enumerate(cycle.STEP_MARKS)]
        raw.update(generated=4, max_tokens=4, step_capacity=4, mtp_steps=1)
        row.update(actual_output=4, actual_path={'mtp_steps': '1'})
        result = cycle.validate_cycle(raw, row)
        self.assertEqual(result['accepted_drafts_histogram'], [0, 0, 0, 1])
        self.assertIsNone(result['acceptance_conditioned'][0]['phase_mean_ms'])

    def test_duplicate_trace(self):
        raw, row = fixture()
        line = cycle.PREFIX + json.dumps(raw)
        with self.assertRaises(ValueError):
            cycle.audit_cycles(line + '\n' + line, [row], True)

    def test_disabled_trace_rejected(self):
        raw, row = fixture()
        with self.assertRaises(ValueError):
            cycle.audit_cycles(cycle.PREFIX + json.dumps(raw), [row], False)

    def test_disabled_empty_passes(self):
        self.assertEqual(cycle.audit_cycles('ordinary server log', [], False), [])

    def test_missing_trace(self):
        _, row = fixture()
        with self.assertRaises(ValueError):
            cycle.audit_cycles('', [row], True)

    def test_duplicate_json_key(self):
        with self.assertRaises(ValueError):
            cycle.records(cycle.PREFIX + '{"response_id":"a","response_id":"b"}')

    def test_malformed_prefix(self):
        with self.assertRaises(ValueError):
            cycle.records('extra ' + cycle.PREFIX + '{}')

    def test_nonstandard_nan_json(self):
        with self.assertRaises(ValueError):
            cycle.records(cycle.PREFIX + '{"value":NaN}')

    def test_wrong_response_id(self):
        self.reject(lambda r, _: r.update(response_id='another'))

    def test_boolean_schema_version(self):
        self.reject(lambda r, _: r.update(schema_version=True))

    def test_incomplete(self):
        self.reject(lambda r, _: r.update(complete=False))

    def test_invalid(self):
        self.reject(lambda r, _: r.update(valid=False))

    def test_recorded_error(self):
        self.reject(lambda r, _: r.update(error='detail_nonfinite'))

    def test_plain_tail(self):
        self.reject(lambda r, _: r.update(plain_tail=True))

    def test_eos_not_accepted_in_length_matrix(self):
        self.reject(lambda r, _: r.update(finish_reason='stop'))

    def test_wrong_step_count(self):
        self.reject(lambda r, _: r.update(mtp_steps=3))

    def test_wrong_capacity(self):
        self.reject(lambda r, _: r.update(step_capacity=1024))

    def test_missing_marker(self):
        self.reject(lambda r, _: r['steps'][0]['marks'].pop())

    def test_reordered_marker(self):
        def mutate(r, _):
            r['steps'][0]['marks'][3:5] = reversed(r['steps'][0]['marks'][3:5])
        self.reject(mutate)

    def test_negative_time(self):
        self.reject(lambda r, _: r['steps'][0]['marks'][0].update(at_ms=-1))

    def test_nonfinite_time(self):
        self.reject(lambda r, _: r['steps'][0]['marks'][0].update(at_ms=math.inf))

    def test_overlapping_steps(self):
        self.reject(lambda r, _: r['steps'][1]['marks'][0].update(at_ms=110))

    def test_position_continuity(self):
        self.reject(lambda r, _: r['steps'][1].update(position=1024))

    def test_accepted_out_of_range(self):
        self.reject(lambda r, _: r['steps'][0].update(accepted_drafts=4))

    def test_returned_contract(self):
        self.reject(lambda r, _: r['steps'][0].update(returned=2))

    def test_verify_shape(self):
        self.reject(lambda r, _: r['steps'][0].update(verify_rows=3))

    def test_extend_shape(self):
        self.reject(lambda r, _: r['steps'][0].update(extend_rows=2))

    def test_generated_usage(self):
        self.reject(lambda r, _: r.update(generated=9))

    def test_nonfinal_clip(self):
        self.reject(lambda r, _: r['steps'][1].update(generated=1, token_piece_writes=1))

    def test_missing_successful_sse_write(self):
        self.reject(lambda r, _: r['steps'][1].update(token_piece_writes=1))

    def test_nonempty_exceeds_writes(self):
        self.reject(lambda r, _: r['steps'][1].update(nonempty_writes=3))

    def test_first_content_outside_first_emit(self):
        self.reject(lambda r, _: r['request_marks'][4].update(at_ms=110))

    def test_no_first_nonempty(self):
        self.reject(lambda r, _: r['steps'][0].update(nonempty_writes=0))

    def test_missing_tls_detail(self):
        self.reject(lambda r, _: r['steps'][0]['details'].pop())

    def test_wrong_tls_detail_phase(self):
        self.reject(lambda r, _: r['steps'][0]['details'][0].update(phase='verify'))

    def test_zero_call_nonzero_duration(self):
        self.reject(lambda r, _: r['steps'][0]['details'][2].update(host_ms=0.01))

    def test_detail_exceeds_parent(self):
        self.reject(lambda r, _: r['steps'][0]['details'][7].update(host_ms=1000))

    def test_missing_draft_call_coverage(self):
        self.reject(lambda r, _: r['steps'][0]['details'][7].update(calls=1))

    def test_missing_positions_leaf_hook(self):
        self.reject(lambda r, _: r['steps'][0]['details'][0].update(calls=0, host_ms=0))

    def test_missing_verify_counts_leaf_hook(self):
        self.reject(lambda r, _: r['steps'][0]['details'][13].update(calls=0, host_ms=0))

    def test_missing_linear_allocation_leaf_hook(self):
        self.reject(lambda r, _: r['steps'][0]['details'][14].update(calls=0, host_ms=0))

    def test_missing_gather_free_leaf_hook(self):
        self.reject(lambda r, _: r['steps'][0]['details'][39].update(calls=0, host_ms=0))

    def test_unexpected_accept_phase_detail(self):
        self.reject(lambda r, _: r['steps'][0]['details'][22].update(calls=1, host_ms=0.01))

    def test_wrong_clock(self):
        self.reject(lambda r, _: r.update(clock='cuda'))

    def test_gpu_event_claim_rejected(self):
        self.reject(lambda r, _: r.update(contains_cuda_events=True))

    def test_additive_detail_claim_rejected(self):
        self.reject(lambda r, _: r.update(host_details_additive=True))


if __name__ == '__main__':
    unittest.main()
