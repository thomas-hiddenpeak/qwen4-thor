"""Budget and failed-response custody for the bounded business driver."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.serve_service import closeout_acceptance as stage


class CloseoutTests(unittest.TestCase):
    def setUp(self):
        parent = stage.ROOT/'build/closeout-driver-tests'
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)
        patch = mock.patch.object(stage, 'OUT', self.out)
        patch.start()
        self.addCleanup(patch.stop)
        self.driver = object.__new__(stage.Stage)
        self.driver.counts = dict(starts=0, chats=0, controls=0)

    def test_budgets_reject_before_an_extra_operation(self):
        for key in self.driver.counts:
            for _ in range(stage.LIMITS[key]):
                self.driver.reserve(key)
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                self.driver.reserve(key)
            self.assertEqual(self.driver.counts[key], stage.LIMITS[key])
        self.assertEqual(json.loads((self.out/'counts.json').read_text()), self.driver.counts)

    def test_http_error_is_counted_archived_and_never_retried(self):
        connection = mock.Mock()
        response = connection.getresponse.return_value
        response.status = 503
        response.read.return_value = b'{"error":"fixture"}'
        with mock.patch.object(stage.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 503'):
                self.driver.complete(self.out, 'failed', {'stream': False})
        self.assertEqual(self.driver.counts['chats'], 1)
        connection.request.assert_called_once()
        connection.close.assert_called_once()
        self.assertEqual((self.out/'failed-wire.bin').read_bytes(), response.read.return_value)
        self.assertFalse((self.out/'failed-completion.json').exists())

    def test_exhausted_chat_budget_never_opens_connection(self):
        self.driver.counts['chats'] = stage.LIMITS['chats']
        with mock.patch.object(stage.http.client, 'HTTPConnection') as connect:
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                self.driver.complete(self.out, 'extra', {})
        connect.assert_not_called()
        self.assertFalse((self.out/'extra-request.json').exists())

    def test_recovery_marker_disables_only_owned_fault_injection(self):
        folder = self.out/'v9'
        folder.mkdir()
        self.assertFalse(stage.fault_enabled('v9'))
        (folder/'fault.request').touch()
        self.assertTrue(stage.fault_enabled('v9'))
        self.assertFalse(stage.fault_enabled('v10'))
        (folder/'recover.request').touch()
        self.assertFalse(stage.fault_enabled('v9'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
