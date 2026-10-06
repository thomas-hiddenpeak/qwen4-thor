"""Mirror-recycle runner contracts; run only after the first quality HTTP."""
import argparse
import copy
import unittest

from mirror_recycle_protocol import (COUNTERS, PREFIX, SCHEMA,
                                      mirror_recycle_evidence)
from offload_policy import BASE_ENVIRONMENT, check_policy_protocol, policy_environment
from run_budget_experiment import experiment_environment, parse_binary_switch


class MirrorEnvironmentContracts(unittest.TestCase):
    def test_explicit_off_on_and_scrub_inherited_experiments(self):
        for state in (0, 1):
            env = experiment_environment(0, {'PATH': '/bin',
                'Q4T_FP8_BAD': '1', 'Q4T_MOE_REQUEST_PARTITION': '1',
                'Q4T_MOE_DECODE_SUPPLY_OBSERVER': '1'},
                policy_axis='mirror-recycle', mirror_gpu_recycle=state)
            expected = {**BASE_ENVIRONMENT, 'Q4T_MOE_CHUNK_ORDER': '0',
                'Q4T_MOE_PARTITION': '0', 'Q4T_MOE_REQUEST_PARTITION': '0',
                'Q4T_MOE_DECODE_PARTITION_LOG_QUIET': '0',
                'Q4T_MOE_MIRROR_GPU_RECYCLE': str(state)}
            self.assertEqual(env, {'PATH': '/bin', **expected})
            protocol = dict(policy_axis='mirror-recycle', partition=0,
                request_partition=0, decode_partition_log_quiet=0,
                mirror_gpu_recycle=state, chunk_order=0,
                effective_environment=expected)
            check_policy_protocol(protocol, state, 'mirror-recycle')
            with self.assertRaises(ValueError):
                check_policy_protocol(protocol, 1-state, 'mirror-recycle')

    def test_reject_other_policy_or_diagnostics(self):
        for change in (dict(partition=1), dict(request_partition=1),
                       dict(decode_partition_log_quiet=1),
                       dict(chunk_order=1), dict(phase_diagnostics=True)):
            args = dict(chunk_order=0, policy_axis='mirror-recycle',
                        mirror_gpu_recycle=1)
            args.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                policy_environment(**args)
        for axis in ('chunk-order', 'partition', 'request-partition',
                     'request-partition-log'):
            with self.subTest(axis=axis), self.assertRaises(ValueError):
                policy_environment(0, policy_axis=axis, mirror_gpu_recycle=1)

    def test_strict_switch_and_legacy_unchanged(self):
        for value in (True, False, -1, 2, '1', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy_environment(0, policy_axis='mirror-recycle',
                                   mirror_gpu_recycle=value)
        self.assertEqual(policy_environment(0),
                         {**BASE_ENVIRONMENT, 'Q4T_MOE_CHUNK_ORDER': '0'})
        for raw in ('', 'true', '01', ' 1', '1 ', '-1', '2'):
            with self.subTest(raw=raw), self.assertRaises(argparse.ArgumentTypeError):
                parse_binary_switch(raw)
        self.assertEqual([parse_binary_switch(x) for x in ('0', '1')], [0, 1])

    def test_tampered_protocol_and_hidden_observation_rejected(self):
        base = dict(policy_axis='mirror-recycle', partition=0,
            request_partition=0, decode_partition_log_quiet=0,
            mirror_gpu_recycle=1, chunk_order=0,
            effective_environment=policy_environment(0,
                policy_axis='mirror-recycle', mirror_gpu_recycle=1))
        for change in (dict(mirror_gpu_recycle=True), dict(partition=1),
                       dict(request_partition=1), dict(decode_partition_log_quiet=1),
                       dict(chunk_order=1), dict(phase_diagnostics=True),
                       dict(diagnostic_scope='hidden')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                check_policy_protocol({**base, **change}, 1, 'mirror-recycle')
        changed = copy.deepcopy(base)
        changed['effective_environment']['Q4T_MOE_DECODE_SUPPLY_OBSERVER'] = '1'
        with self.assertRaises(ValueError):
            check_policy_protocol(changed, 1, 'mirror-recycle')


class MirrorSummaryContracts(unittest.TestCase):
    def fixture(self, state=1):
        requests = [dict(response_id=f'req-{i}', response_id_valid=True,
                         actual_output=3) for i in range(2)]
        rows = ([dict(plans=10, attempts=7, preferred=3, changed=2,
                      fallback=3, unavailable=1, published=2),
                 dict.fromkeys(COUNTERS, 0)] if state else
                [dict.fromkeys(COUNTERS, 0) for _ in requests])
        lines = [f'{PREFIX} enabled={state} scope=explicit_single_decode schema={SCHEMA}']
        lines += [f'{PREFIX} id={request["response_id"]} ' +
                  ' '.join(f'{key}={row[key]}' for key in COUNTERS)
                  for request, row in zip(requests, rows)]
        return requests, '\n'.join(lines)

    def test_enabled_counts_close_and_zero_request_allowed(self):
        requests, log = self.fixture()
        result = mirror_recycle_evidence(log, requests, 1)
        self.assertTrue(result['passed'])
        self.assertEqual(result['request_count'], 2)
        self.assertEqual(result['totals']['changed'], 2)
        self.assertTrue(result['selection_change_observed'])
        self.assertFalse(result['performance_acceptance'])
        self.assertFalse(result['runtime_phase_gate_proven_by_parser'])

    def test_disabled_all_zero_and_preferred_alone_not_activation(self):
        requests, log = self.fixture(0)
        self.assertFalse(mirror_recycle_evidence(log, requests, 0)[
            'selection_change_observed'])
        requests, log = self.fixture()
        log = log.replace('changed=2', 'changed=0')
        self.assertFalse(mirror_recycle_evidence(log, requests, 1)[
            'selection_change_observed'])

    def test_startup_scope_state_schema_missing_or_duplicate_rejected(self):
        requests, log = self.fixture()
        for changed in (log.replace('enabled=1', 'enabled=0'),
                        log.replace('explicit_single_decode', 'T1'),
                        log.replace(SCHEMA, 'unknown'),
                        '\n'.join(log.splitlines()[1:]), log + '\n' + log,
                        log.replace('enabled=1', 'enabled=01')):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                mirror_recycle_evidence(changed, requests, 1)

    def test_missing_duplicate_out_of_order_unknown_or_malformed_summary(self):
        requests, log = self.fixture()
        lines = log.splitlines()
        for changed in ('\n'.join(lines[:-1]),
                        '\n'.join([lines[0], lines[1], lines[1]]),
                        '\n'.join([lines[0], lines[2], lines[1]]),
                        log.replace('id=req-1', 'id=unknown'),
                        log.replace('published=2', 'published=-1'),
                        log.replace('published=2', 'published=2 extra=3')):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                mirror_recycle_evidence(changed, requests, 1)

    def test_counter_closure_subsets_and_disabled_work_rejected(self):
        requests, log = self.fixture()
        for changed in (log.replace('attempts=7', 'attempts=8'),
                        log.replace('changed=2', 'changed=4'),
                        log.replace('published=2', 'published=4'),
                        log.replace('plans=10', 'plans=0'),
                        log.replace('plans=10', 'plans=97'),
                        log.replace('plans=10', f'plans={2**64}')):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                mirror_recycle_evidence(changed, requests, 1)
        with self.assertRaises(ValueError):
            mirror_recycle_evidence(log.replace('enabled=1', 'enabled=0'), requests, 0)

    def test_invalid_http_identity_output_or_duplicate_id_rejected(self):
        requests, log = self.fixture()
        for change in (dict(response_id_valid=False), dict(response_id=''),
                       dict(actual_output=0), dict(actual_output=True),
                       dict(actual_output=1)):
            altered = copy.deepcopy(requests)
            altered[0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                mirror_recycle_evidence(log, altered, 1)
        requests[1]['response_id'] = requests[0]['response_id']
        with self.assertRaises(ValueError):
            mirror_recycle_evidence(log.replace('id=req-1', 'id=req-0'), requests, 1)


if __name__ == '__main__':
    unittest.main()
