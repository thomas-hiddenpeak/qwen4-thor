"""NVML classifications with synthetic results; never import the driver."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_service import nvml_policy as policy


def failure(code, kind='NVMLError'):
    return dict(status='ERROR', pids=None,
                error=dict(type=kind, message='fixture', nvml_code=code))


class ClassificationTests(unittest.TestCase):
    def test_timeouts_retain_distinct_origins(self):
        self.assertEqual(policy.classify(dict(status='TIMEOUT')).category,
                         'QUERY_TIMEOUT')
        self.assertEqual(policy.classify(failure(10)),
                         policy.Decision('NVML_TIMEOUT', 'retry', 10))

    def test_only_explicit_transient_codes_retry(self):
        for code in (20, 23, 27):
            self.assertEqual(policy.classify(failure(code)).action, 'retry')
        for code in (6, 19, 21, 999, 9999):
            result = policy.classify(failure(code))
            self.assertEqual((result.category, result.action, result.nvml_code),
                             ('NVML_UNCLASSIFIED', 'suspend', code))

    def test_configuration_does_not_spend_automatic_retry_budget(self):
        for code in (3, 4, 9, 12, 13, 17, 18, 22, 24, 25, 26, 30):
            self.assertEqual(policy.classify(failure(code)),
                             policy.Decision('CONFIGURATION', 'suspend', code))
        self.assertEqual(policy.classify(failure(None, 'ModuleNotFoundError')).action,
                         'suspend')

    def test_explicit_device_failures_are_fatal(self):
        for code in (8, 11, 14, 15, 16, 28):
            self.assertEqual(policy.classify(failure(code)),
                             policy.Decision('DEVICE_FAILURE', 'fatal', code))

    def test_programming_and_python_errors_are_not_vendor_outages(self):
        for code in (1, 2, 5, 7, 29):
            self.assertEqual(policy.classify(failure(code)).category, 'QUERY_ERROR')
        self.assertEqual(policy.classify(failure(None, 'ValueError')).action, 'fatal')

    def test_shutdown_fault_cannot_be_hidden_by_primary_timeout(self):
        value = failure(10)
        value['shutdown_error'] = failure(15)['error']
        self.assertEqual(policy.classify(value),
                         policy.Decision('DEVICE_FAILURE', 'fatal', 15))

    def test_reported_device_error_wins_over_supervisor_timeout_or_unreaped(self):
        for status in ('TIMEOUT', 'CLEANUP_FAILED'):
            value = dict(status=status, observed_errors=[failure(16)['error']])
            self.assertEqual(policy.classify(value),
                             policy.Decision('DEVICE_FAILURE', 'fatal', 16))
        value = dict(status='TIMEOUT', protocol_error='stale identity')
        self.assertEqual(policy.classify(value).category, 'PROTOCOL_ERROR')

    def test_partial_error_protocol_must_be_bounded_and_consistent(self):
        for status, observed in (('OK', [failure(10)['error']]),
                                 ('TIMEOUT', 'invalid'),
                                 ('TIMEOUT', [failure(10)['error']]*3)):
            self.assertEqual(policy.classify(dict(status=status, pids=[],
                observed_errors=observed)).category, 'PROTOCOL_ERROR')

    def test_error_text_does_not_grant_retry_or_recovery(self):
        value = failure(15)
        value['error']['message'] = 'timeout; safe to retry'
        self.assertEqual(policy.classify(value).category, 'DEVICE_FAILURE')

    def test_malformed_observations_fail_closed(self):
        for value in (None, [], {}, dict(status='unexpected'),
                      dict(status='ERROR', error='timeout'), failure(True),
                      failure('10'), failure(0), failure(-1),
                      dict(status='OK', pids=[True]), dict(status='OK', pids=None),
                      dict(status='OK', pids=[], error=failure(10)['error'])):
            with self.subTest(value=value):
                self.assertEqual(policy.classify(value),
                                 policy.Decision('PROTOCOL_ERROR', 'fatal'))

    def test_success_and_unreaped_worker_are_separate_states(self):
        self.assertEqual(policy.classify(dict(status='OK', pids=[])).action, 'observe')
        self.assertEqual(policy.classify(dict(status='CLEANUP_FAILED')).category,
                         'WORKER_UNREAPED')


if __name__ == '__main__':
    unittest.main()
