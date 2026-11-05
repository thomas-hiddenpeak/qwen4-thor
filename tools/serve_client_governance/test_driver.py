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
        self.assertEqual(s.LIMITS, dict(starts=1, generation_attempts=20, controls=246, seconds=300))
        self.assertEqual(s.PLANNED['successes']+s.PLANNED['cancelled']+s.PLANNED['refusals'], 12)

    def test_frontend_with_real_validator_reserves_control_room(self):
        front = s.s.g.policy(s.frozen_frontend())
        self.assertEqual(front['global_inflight'], 8)
        self.assertGreater(front['per_client_inflight'], 4)

    def test_preflight_uses_existing_closed_envelope(self):
        body = s.echo()
        envelope = s.preflight_envelope(body)
        self.assertEqual(set(envelope), {'request'})
        self.assertEqual(envelope['request'], body)
        schema = s.cpu.read(s.ROOT/'docs/serve-preflight-v1.schema.json')
        from jsonschema import ValidationError
        with self.assertRaises(ValidationError):
            s.s.Draft202012Validator(schema['$defs']['request']).validate(body)

    def test_poll_file_selection_uses_numeric_suffix(self):
        from tools.serve_client_governance.report import latest_poll
        from tempfile import TemporaryDirectory
        from pathlib import Path
        root = s.BASE/'cpu-fixtures'
        root.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=root) as folder:
            folder = Path(folder)
            for number in (8, 10, 100):
                (folder/f'queue-{number}-wire.bin').write_text('')
            self.assertEqual(latest_poll(folder, 'queue').name, 'queue-100-wire.bin')
            with self.assertRaises(ValueError):
                latest_poll(folder, 'absent')

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
