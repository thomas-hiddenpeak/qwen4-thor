"""Offline driver guards: reject vacuous order/accounting evidence."""
import unittest
from tools.serve_client_governance import stage as s
from tools.serve_gateway import client_policy as cp


class DriverTests(unittest.TestCase):
    def test_frozen_policy_fits_global_resources(self):
        p = cp.validate(s.frozen_policy())
        self.assertEqual(p['version'], 2)
        self.assertTrue(p['fair_queue'])
        self.assertEqual(sum(c['max_inflight'] for c in p['clients']), 8)
        self.assertEqual([c['requests_per_minute'] for c in p['clients']], [6, 20])
        self.assertEqual(s.LIMITS, dict(starts=2, generation_attempts=24, controls=250, seconds=480))
        self.assertEqual(s.PLANNED['successes']+s.PLANNED['cancelled']+s.PLANNED['refusals'], 16)

    def test_order_requires_actual_admission_event(self):
        log = ('[q4t][request] instance=1 id=a event=stage stage=encode seq=-1\n'
               '[q4t][request] instance=1 id=a event=admitted stage=queue seq=0\n'
               '[q4t][request] instance=2 id=b event=end stage=queue seq=-1\n')
        self.assertEqual(s.admitted(log), ['a'])
        self.assertEqual(s.admitted(''), [])

    def test_metric_identity_and_missing_value_fail_closed(self):
        values = {'q4t_client_rate_tokens{client="alpha"}': 4,
                  'q4t_client_rate_tokens{client="beta"}': 7}
        self.assertEqual(s.rate(values, 'alpha', 'tokens'), 4)
        with self.assertRaises(KeyError):
            s.rate(values, 'unknown', 'tokens')
        with self.assertRaises(KeyError):
            s.rate(values, 'alpha', 'reserved_tokens')
