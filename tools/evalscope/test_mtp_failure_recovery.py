"""Pure offline fixtures; no network, subprocess, model or CUDA calls."""
import json
import unittest

import mtp_failure_recovery_contract as contract


def response(label='failure-target2', error=True, finish=False, usage=False):
    events = [{'id': label, 'choices': [{'delta': {'role': 'assistant'}}]}]
    if finish:
        events.append({'id': label, 'choices': [{'finish_reason': 'length'}]})
    if usage:
        events.append({'id': label, 'usage': {'completion_tokens': 1}})
    if error:
        events.append({'error': {'message': 'generation failed',
                                 'type': 'server_error',
                                 'code': 'generation_failed'}})
    return {'status': 200, 'body': ''.join('data: ' + json.dumps(event) + '\n\n'
                                         for event in events) +
            'data: [DONE]\n\n'}


def log_fixture():
    lines = [contract.VARIANT]
    for ordinal, (label, kind) in enumerate(contract.PLAN, 1):
        failed = kind != 'success'
        target, draft, extend, tail, steps = ((252, 200, 100, 4, 100)
                                              if not failed else
                                              ((2, 2, 0, 0, 0) if ordinal == 2
                                               else (3, 2, 1, 0, 0)))
        counts = f'target_calls={target} draft_calls={draft} extend_calls={extend}'
        lines += [f'[q4t][fault_begin] ordinal={ordinal} real_ok=1 slot=0 '
                  'stage=prefill position=0 history=0 pending=0',
                  f'[q4t][fault_reset] ordinal={ordinal} real_ok=1 slot=0 '
                  'completion=stream_ordered']
        if failed:
            lines += [f'[q4t][fault_injection] ordinal={ordinal} kind={kind} '
                      f'real_ok=1 {counts} position=1024 history=1024 pending=0',
                      f'[q4t][fault_drain] ordinal={ordinal} real_ok=1 '
                      'stream_match=1 thread_match=1']
        path = 'plain' if failed else 'mtp_sequential_b1'
        reason = 'none' if failed else 'output_limit'
        lines += [f'[q4t][decode_path] id={label} requested_mtp=1 path={path} '
                  f'mtp_steps={steps} fallback=none plain_tail_tokens={tail} '
                  f'verifier=sequential tail_reason={reason} '
                  f'draft_forward_calls={draft} target_t1_calls={target} '
                  f'target_t4_calls=0 extend_forward_calls={extend} '
                  'forward_count_scope=sequential_attempts',
                  f'[q4t][fault_end] ordinal={ordinal} real_ok=1 '
                  f'injected={int(failed)} drained={int(failed)} '
                  f'stage=idle position=0 history=0 pending=0 {counts} '
                  f'tail_calls={0 if failed else 3}']
        if failed:
            lines.append(f'[q4t] generation failed id={label}')
    return '\n'.join(lines) + '\n'


class FailureRecoveryContractTest(unittest.TestCase):
    def test_five_request_log_binds_counts_resets_and_drains(self):
        result = contract.validate_log(log_fixture())
        self.assertEqual(len(result['groups']), 5)

    def test_logical_error_allows_no_content_before_failure(self):
        result = contract.failed_result(response(), 'failure-target2')
        self.assertEqual(result['text'], '')

    def test_fault_response_rejects_normal_finish(self):
        with self.assertRaises(ValueError):
            contract.failed_result(response(finish=True), 'failure-target2')

    def test_fault_response_rejects_usage(self):
        with self.assertRaises(ValueError):
            contract.failed_result(response(usage=True), 'failure-target2')

    def test_fault_response_rejects_wrong_actual_identity(self):
        with self.assertRaises(ValueError):
            contract.failed_result(response(label='other'), 'failure-target2')

    def test_fault_response_requires_error_last(self):
        value = response()
        value['body'] = value['body'].replace('data: [DONE]',
            'data: {"id":"failure-target2","choices":[]}\n\ndata: [DONE]')
        with self.assertRaises(ValueError):
            contract.failed_result(value, 'failure-target2')

    def test_normal_production_binary_is_rejected(self):
        with self.assertRaises(ValueError):
            contract.validate_log(log_fixture().replace(contract.VARIANT, ''))

    def test_failure_requires_real_forward(self):
        with self.assertRaises(ValueError):
            contract.validate_log(log_fixture().replace(
                'kind=first_extend real_ok=1', 'kind=first_extend real_ok=0'))

    def test_failure_requires_production_drain(self):
        lines = [line for line in log_fixture().splitlines()
                 if not line.startswith('[q4t][fault_drain] ordinal=2')]
        with self.assertRaises(ValueError):
            contract.validate_log('\n'.join(lines))

    def test_recovery_requires_fresh_draft_reset(self):
        lines = [line for line in log_fixture().splitlines()
                 if not line.startswith('[q4t][fault_reset] ordinal=3')]
        with self.assertRaises(ValueError):
            contract.validate_log('\n'.join(lines))

    def test_failure_rejects_fallback_even_with_error_response(self):
        with self.assertRaises(ValueError):
            contract.validate_log(log_fixture().replace(
                'mtp_steps=0 fallback=none', 'mtp_steps=0 fallback=load_failed', 1))

    def test_failure_rejects_plain_work_after_fault(self):
        with self.assertRaises(ValueError):
            contract.validate_log(log_fixture().replace('tail_calls=0',
                                                        'tail_calls=1', 1))

    def test_failure_rejects_forged_core_attempt_counts(self):
        with self.assertRaises(ValueError):
            contract.validate_log(log_fixture().replace('target_t1_calls=2 ',
                                                        'target_t1_calls=1 ', 1))

    def test_recovery_rejects_stale_host_history(self):
        with self.assertRaises(ValueError):
            contract.validate_log(log_fixture().replace(
                'stage=prefill position=0 history=0',
                'stage=prefill position=0 history=1', 1))

    def test_log_rejects_duplicate_observer_fields(self):
        with self.assertRaises(ValueError):
            contract.validate_log(log_fixture().replace('ordinal=2 real_ok=1',
                'ordinal=2 ordinal=2 real_ok=1', 1))

    def test_log_rejects_changed_recovery_accounting(self):
        with self.assertRaises(ValueError):
            contract.validate_log(log_fixture().replace(
                'id=failure-extend-recovery requested_mtp=1',
                'id=failure-extend-recovery requested_mtp=0'))

    def test_metrics_distinguish_generation_failure_from_cancel(self):
        contract.require_counter_delta((0, 0, 0, 0), (1, 0, 1, 0), False)
        with self.assertRaises(ValueError):
            contract.require_counter_delta((0, 0, 0, 0), (1, 0, 0, 1), False)

    def test_metrics_reject_missing_or_duplicate_errors(self):
        body = '\n'.join(name + ' 0' for name in contract.COUNTERS)
        self.assertEqual(contract.counters(body), (0, 0, 0, 0))
        with self.assertRaises(ValueError):
            contract.counters(body + '\nq4t_requests_error_total 0')


if __name__ == '__main__':
    unittest.main()
