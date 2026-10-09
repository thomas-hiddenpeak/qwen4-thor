"""Synthetic mode/measurement contracts; no model or server invocation."""
import unittest

from acceptance_mode import (performance_metrics, request_mode_evidence,
                             startup_evidence)


def startup_log(mtp):
    return (
        f'[q4t][capabilities] effective mtp={int(mtp)} media_allowed=0 '
        'vision_loaded=0 max_seq=1\n'
        '[q4t][capacity] requested_max_len=208896 requested_max_seq=1 '
        'requested_max_prefill=8192 effective_max_len=208896 '
        'effective_max_seq=1 effective_max_prefill=8192 '
        'budget_enabled=1 budget_feasible=true\n')


def terminal(request_id='r1', mtp=True, path='mtp_multi_b1', steps=2,
             fallback='none', tail=0):
    return (f'[q4t][decode_path] id={request_id} requested_mtp={int(mtp)} '
            f'path={path} mtp_steps={steps} fallback={fallback} '
            f'plain_tail_tokens={tail}\n')


def response(request_id='r1', output=8):
    return {'response_id': request_id, 'actual_output': output, 'finish': ['length']}


def sequential_terminal(path='mtp_sequential_b1', steps=2, tail=2,
                        reason='output_limit', targets=6, extends=2):
    return terminal(path=path, steps=steps, tail=tail).rstrip() + (
        f' verifier=sequential tail_reason={reason} '
        f'draft_forward_calls={steps * 2} target_t1_calls={targets} '
        f'target_t4_calls=0 extend_forward_calls={extends} '
        'forward_count_scope=sequential_attempts\n')


class SequentialModeTest(unittest.TestCase):
    def check(self, log, output=8, finish='length'):
        row = response(output=output)
        row['finish'] = [finish]
        return request_mode_evidence(log, [row], True,
                                     'sequential')['passed']

    def test_startup_requires_actual_sequential_verifier(self):
        legacy = startup_log(True)
        strict = legacy.replace('max_seq=1\n', 'max_seq=1 verifier=sequential\n')
        self.assertTrue(startup_evidence(strict, True, 'sequential')['passed'])
        self.assertFalse(startup_evidence(legacy, True, 'sequential')['passed'])
        self.assertFalse(startup_evidence(strict, True)['passed'])
        self.assertFalse(startup_evidence(strict, False, 'sequential')['passed'])

    def test_actual_sequential_with_output_tail(self):
        self.assertTrue(self.check(sequential_terminal()))
        self.assertFalse(request_mode_evidence(sequential_terminal(),
                                              [response()], True)['passed'])

    def test_legacy_and_mislabelled_paths_rejected(self):
        for log in (terminal(), sequential_terminal(path='mtp_multi_b1'),
                    sequential_terminal().replace('verifier=sequential', ''),
                    sequential_terminal().replace('fallback=none',
                                                  'fallback=init_failed')):
            self.assertFalse(self.check(log))

    def test_pure_tail_and_prefill_have_no_speculative_work(self):
        for output in (1, 2, 3, 4):
            self.assertTrue(self.check(sequential_terminal(
                path='plain_tail_b1', steps=0, tail=output,
                reason='context_limit', targets=0, extends=0), output))
        prefill = sequential_terminal(path='prefill_only', steps=0, tail=0,
                                      reason='none', targets=0, extends=0)
        self.assertTrue(self.check(prefill, 1))
        self.assertFalse(self.check(prefill, 2))
        self.assertFalse(self.check(prefill.replace('tail_reason=none',
                                                    'tail_reason=output_limit'), 1))
        self.assertFalse(self.check(sequential_terminal(
            path='plain_tail_b1', steps=0, tail=2, targets=1, extends=0), 2))

    def test_terminal_step_skips_exactly_one_extend(self):
        self.assertTrue(self.check(sequential_terminal(tail=0, reason='none',
                                                       extends=1), 7, 'stop'))
        self.assertFalse(self.check(sequential_terminal(tail=0, reason='none',
                                                        extends=1), 8, 'stop'))
        self.assertFalse(self.check(sequential_terminal(tail=0, reason='none',
                                                        extends=1), 7, 'length'))
        self.assertFalse(self.check(sequential_terminal(extends=1)))
        self.assertFalse(self.check(sequential_terminal(tail=0, reason='none',
                                                        extends=0)))

    def test_invalid_or_missing_forward_counts_rejected(self):
        log = sequential_terminal()
        for old, new in [('draft_forward_calls=4', 'draft_forward_calls=3'),
                         ('target_t1_calls=6', 'target_t1_calls=9'),
                         ('target_t1_calls=6', 'target_t1_calls=1'),
                         ('target_t4_calls=0', 'target_t4_calls=1'),
                         ('extend_forward_calls=2', 'extend_forward_calls=3'),
                         ('target_t1_calls=6', 'target_t1_calls=-1'),
                         ('target_t1_calls=6', ''),
                         ('forward_count_scope=sequential_attempts', '')]:
            with self.subTest(new=new):
                self.assertFalse(self.check(log.replace(old, new)))

    def test_tail_reason_and_count_must_agree(self):
        for log in (sequential_terminal(reason='none'),
                    sequential_terminal(reason='context_tail'),
                    sequential_terminal(tail=5)):
            self.assertFalse(self.check(log))

    def test_completed_output_binds_actual_target_consumption(self):
        self.assertFalse(self.check(sequential_terminal(targets=5)))
        self.assertFalse(self.check(sequential_terminal(), 256))
        self.assertFalse(self.check(sequential_terminal(tail=0, reason='none'), 6))


class StartupModeTest(unittest.TestCase):
    def test_explicit_plain_and_mtp(self):
        for mtp in [False, True]:
            result = startup_evidence(startup_log(mtp), mtp)
            self.assertTrue(result['passed'], result['errors'])

    def test_actual_mode_mismatch(self):
        for mtp in [False, True]:
            self.assertFalse(startup_evidence(startup_log(not mtp), mtp)['passed'])

    def test_missing_startup_evidence(self):
        log = startup_log(True)
        for incomplete in ['', log.splitlines()[0], log.splitlines()[1]]:
            self.assertFalse(startup_evidence(incomplete, True)['passed'])

    def test_changed_capacity_or_media_is_not_the_matrix(self):
        log = startup_log(True)
        for old, new in [('effective_max_len=208896', 'effective_max_len=8192'),
                         ('effective_max_seq=1', 'effective_max_seq=2'),
                         ('effective_max_prefill=8192', 'effective_max_prefill=4096'),
                         ('budget_enabled=1', 'budget_enabled=0'),
                         ('budget_feasible=true', 'budget_feasible=false'),
                         ('media_allowed=0', 'media_allowed=1'),
                         ('vision_loaded=0', 'vision_loaded=1')]:
            with self.subTest(new=new):
                self.assertFalse(startup_evidence(log.replace(old, new), True)['passed'])

    def test_duplicate_records_fields_or_malformed_tokens(self):
        log = startup_log(True)
        for changed in [log + log, log.replace('mtp=1', 'mtp=0 mtp=1'),
                        log.replace('mtp=1', 'mtp=1 stray')]:
            self.assertFalse(startup_evidence(changed, True)['passed'])


class RequestModeTest(unittest.TestCase):
    def test_actual_mtp_bound_by_response_id_not_order(self):
        log = terminal('second') + terminal('first')
        result = request_mode_evidence(log, [response('first'), response('second')], True)
        self.assertTrue(result['passed'], result['errors'])
        self.assertEqual(result['bindings'][0]['records'][0]['fields']['id'], 'first')

    def test_mtp_startup_alone_is_insufficient(self):
        self.assertFalse(request_mode_evidence(startup_log(True), [response()], True)['passed'])
        self.assertFalse(request_mode_evidence('', [], True)['passed'])

    def test_mtp_plain_fallback_rejected(self):
        for reason in ['init_failed', 'context_tail', 'none']:
            log = terminal(path='plain', steps=0, fallback=reason)
            self.assertFalse(request_mode_evidence(log, [response()], True)['passed'])

    def test_mtp_requires_actual_steps_and_expected_path(self):
        for log in [terminal(steps=0), terminal(path='mtp_scalar'),
                    terminal(mtp=False), terminal(fallback='init_failed')]:
            self.assertFalse(request_mode_evidence(log, [response()], True)['passed'])

    def test_prefill_only_short_terminal_is_separate(self):
        log = terminal(path='prefill_only', steps=0)
        result = request_mode_evidence(log, [response(output=1)], True)
        self.assertTrue(result['passed'], result['errors'])
        for output in [0, 2, 8]:
            self.assertFalse(request_mode_evidence(log, [response(output=output)], True)['passed'])
        for bad in [terminal(path='prefill_only', steps=1),
                    terminal(path='prefill_only', steps=0, tail=1)]:
            self.assertFalse(request_mode_evidence(bad, [response(output=1)], True)['passed'])

    def test_valid_mtp_with_explicit_bounded_context_tail(self):
        result = request_mode_evidence(terminal(tail=2), [response()], True)
        self.assertTrue(result['passed'], result['errors'])
        self.assertEqual(result['bindings'][0]['records'][0]['fields']['plain_tail_tokens'], '2')

    def test_new_t4_pure_tail_requires_explicit_mode_and_reason(self):
        for output in (1, 2, 3, 4):
            for reason in ('output_limit', 'context_limit'):
                log = terminal(path='plain_tail_b1', steps=0,
                               tail=output).rstrip() + (
                    f' verifier=t4 tail_reason={reason}\n')
                result = request_mode_evidence(
                    log, [response(output=output)], True, 't4')
                self.assertTrue(result['passed'], result['errors'])

    def test_t4_tail_does_not_accept_legacy_or_inconsistent_evidence(self):
        log = terminal(path='plain_tail_b1', steps=0, tail=3).rstrip() + (
            ' verifier=t4 tail_reason=output_limit\n')
        for before, after in (
                (' verifier=t4', ''), ('verifier=t4', 'verifier=sequential'),
                (' tail_reason=output_limit', ''),
                ('tail_reason=output_limit', 'tail_reason=none'),
                ('tail_reason=output_limit', 'tail_reason=context_tail'),
                ('mtp_steps=0', 'mtp_steps=1'),
                ('plain_tail_tokens=3', 'plain_tail_tokens=2'),
                ('fallback=none', 'fallback=initialization')):
            with self.subTest(before=before, after=after):
                self.assertFalse(request_mode_evidence(
                    log.replace(before, after), [response(output=3)],
                    True, 't4')['passed'])
        for output in (0, 5):
            self.assertFalse(request_mode_evidence(
                log.replace('plain_tail_tokens=3',
                            f'plain_tail_tokens={output}'),
                [response(output=output)], True, 't4')['passed'])

    def test_invalid_work_counts(self):
        for log in [terminal(steps=-1), terminal(steps='bad'),
                    terminal(tail=-1), terminal(tail=9),
                    terminal().replace(' plain_tail_tokens=0', '')]:
            self.assertFalse(request_mode_evidence(log, [response()], True)['passed'])

    def test_wrong_missing_and_duplicate_identity_rejected(self):
        for log in [terminal('other'), terminal() + terminal(),
                    terminal().replace('id=r1 ', '')]:
            self.assertFalse(request_mode_evidence(log, [response()], True)['passed'])
        self.assertFalse(request_mode_evidence(terminal(), [response(), response()], True)['passed'])

    def test_old_plain_baseline_compatible_without_new_log(self):
        result = request_mode_evidence(startup_log(False), [response()], False)
        self.assertTrue(result['passed'], result['errors'])
        self.assertEqual(result['bindings'][0]['coverage'], 'legacy_plain_startup_only')

    def test_new_plain_path_must_be_consistent_when_present(self):
        log = terminal(mtp=False, path='plain', steps=0)
        self.assertTrue(request_mode_evidence(log, [response()], False)['passed'])
        for changed in [terminal(), terminal(mtp=False, path='plain', steps=1),
                        log + terminal('unmatched', mtp=False, path='plain', steps=0)]:
            self.assertFalse(request_mode_evidence(changed, [response()], False)['passed'])

    def test_failed_http_counts_do_not_crash_mode_audit(self):
        for output in [None, False, -1, '8']:
            self.assertFalse(request_mode_evidence(terminal(), [response(output=output)], True)['passed'])


class ClientTimingTest(unittest.TestCase):
    def test_keeps_ttft_request_latency_and_distinct_throughputs(self):
        metrics = performance_metrics({'ttft': 2.0, 'latency': 7.0,
                                       'actual_output': 11})
        self.assertEqual(metrics['ttft'], 2.0)
        self.assertEqual(metrics['latency'], 7.0)
        self.assertEqual(metrics['decode_seconds'], 5.0)
        self.assertEqual(metrics['decode_tps'], 2.0)
        self.assertEqual(metrics['overall_tps'], 11 / 7)

    def test_invalid_or_nonfinite_timing_rejected(self):
        for key, value in [('ttft', None), ('latency', float('nan')),
                           ('ttft', float('inf')), ('ttft', -1),
                           ('latency', 2), ('latency', 1), ('latency', True),
                           ('actual_output', 1)]:
            row = {'ttft': 2.0, 'latency': 7.0, 'actual_output': 256}
            row[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                performance_metrics(row)


if __name__ == '__main__':
    unittest.main()
