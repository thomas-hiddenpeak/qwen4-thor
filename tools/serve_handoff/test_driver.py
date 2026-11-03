"""Acceptance must reject wrong error, dropped usage and extra attempts."""
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_handoff import stage, verify


class DriverTests(unittest.TestCase):
    def setUp(self):
        root = stage.BASE/'driver-tests'; root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.run = verify.Run(dict(port=18121, started=time.time()),
                              Path(self.temp.name)/'run')

    def test_strict_stream_requests_usage_and_fresh_ids(self):
        answer = SimpleNamespace(finish_reason='stop', message=dict(content='{"ok":true}'))
        with mock.patch.object(self.run, 'complete', return_value=answer) as complete:
            self.run.strict('json'); self.run.strict('sse', True)
        one, two = [call.args[1] for call in complete.call_args_list]
        self.assertFalse(one['stream'])
        self.assertTrue(two['stream_options']['include_usage'])
        self.assertNotEqual(one['request_id'], two['request_id'])

    def test_expected_incomplete_error_requires_complete_and_correct_wire(self):
        def capture(label, body):
            self.run.budget('generations')
            self.assertEqual(body['max_tokens'], 1)
            verify.client.pkg.save(self.run.out/'incomplete-http.json', self.http)
            verify.client.pkg.save(self.run.out/'incomplete-wire.bin', dict(error=dict(code=self.code)))
            raise RuntimeError('generation HTTP failure')
        with mock.patch.object(self.run, 'complete', side_effect=capture):
            self.http = dict(status=502, eof=True)
            self.code = 'structured_output_incomplete'
            self.run.incomplete()
            self.assertEqual(self.run.counts['generations'], 1)
            self.http = dict(status=503, eof=True)
            self.code = 'structured_capacity_exhausted'
            with self.assertRaisesRegex(RuntimeError, 'incorrect error terminal'):
                self.run.incomplete()
            self.http = dict(status=502, eof=False)
            self.code = 'structured_output_incomplete'
            with self.assertRaisesRegex(RuntimeError, 'incorrect error terminal'):
                self.run.incomplete()

    def test_unexpected_success_or_transport_failure_is_not_expected_negative(self):
        with mock.patch.object(self.run, 'complete', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'reported success'):
                self.run.incomplete()
        with mock.patch.object(self.run, 'complete', side_effect=RuntimeError('socket failed')):
            with self.assertRaisesRegex(RuntimeError, 'unexpected client error'):
                self.run.incomplete()

    def test_budget_is_attempt_based_and_cannot_extend_on_failure(self):
        for _ in range(stage.LIMITS['generations']):
            self.run.budget('generations')
        with self.assertRaisesRegex(RuntimeError, 'attempt budget'):
            self.run.budget('generations')
        self.run.plan['started'] -= stage.LIMITS['seconds']
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            self.run.budget()


if __name__ == '__main__':
    unittest.main()
