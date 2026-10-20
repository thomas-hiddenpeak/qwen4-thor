"""Host-only cases for previously conflated performance decisions."""
import unittest
from performance_decision import assess


def ledger(**changes):
    value = dict(candidate_sha256='a' * 64, plan_reference='frozen plan',
                 evidence_reference='audited results', route_reference='specific route',
                 route='engineering', validity='valid', contracts='pass',
                 measurement='complete', regression='not_established',
                 statistics='inconclusive', engineering_decision='pending',
                 request_budget=60, requests_used=60)
    value.update(changes)
    return value


class Decisions(unittest.TestCase):
    def test_measured_is_not_unmeasured_or_failed(self):
        self.assertEqual(assess(ledger())['status'], 'ENGINEERING_DECISION_PENDING')

    def test_acceptance_not_reopened_by_statistical_uncertainty(self):
        result = assess(ledger(engineering_decision='accepted', decision_reference='user decision'))
        self.assertEqual(result['status'], 'ENGINEERING_ACCEPTED')
        self.assertEqual(result['statistics'], 'inconclusive')
        self.assertFalse(result['deployment_authorized'])

    def test_missing_measurement_not_excused_by_acceptance(self):
        result = assess(ledger(measurement='not_run', requests_used=0,
                               engineering_decision='accepted', decision_reference='decision'))
        self.assertEqual(result['status'], 'MEASUREMENT_INCOMPLETE')
        self.assertEqual(result['remaining_request_budget'], 60)

    def test_environment_is_not_candidate_failure(self):
        self.assertEqual(assess(ledger(validity='env_abort'))['status'], 'ENV_ABORT')

    def test_contract_failure_overrides_acceptance(self):
        self.assertEqual(assess(ledger(contracts='fail', engineering_decision='accepted',
                                      decision_reference='decision'))['status'], 'CONTRACT_FAILURE')

    def test_confirmed_regression_overrides_acceptance(self):
        self.assertEqual(assess(ledger(regression='confirmed', regression_reference='causal evidence',
                                      engineering_decision='accepted', decision_reference='decision'))['status'],
                         'REGRESSION_CONFIRMED')

    def test_statistical_route_does_not_accept_engineering_vote(self):
        result = assess(ledger(route='statistical', engineering_decision='accepted',
                               decision_reference='decision'))
        self.assertEqual(result['status'], 'STATISTICAL_INCONCLUSIVE')
        self.assertFalse(result['automatic_resampling'])

    def test_statistics_need_analysis_reference(self):
        with self.assertRaises(ValueError):
            assess(ledger(route='statistical', statistics='noninferior'))
        self.assertEqual(assess(ledger(route='statistical', statistics='noninferior',
                                      statistical_analysis_reference='reviewed interval'))['status'],
                         'STATISTICAL_CRITERION_MET')

    def test_no_acceptance_without_decision_record(self):
        with self.assertRaises(ValueError):
            assess(ledger(engineering_decision='accepted'))

    def test_budget_violation_is_not_silently_accepted(self):
        self.assertEqual(assess(ledger(requests_used=61))['status'], 'BUDGET_EXCEEDED')
        with self.assertRaises(ValueError):
            assess(ledger(request_budget=True))

    def test_rejected_candidate_not_resampled(self):
        self.assertEqual(assess(ledger(engineering_decision='rejected',
                                      decision_reference='reject'))['status'], 'REJECTED')


if __name__ == '__main__':
    unittest.main()
