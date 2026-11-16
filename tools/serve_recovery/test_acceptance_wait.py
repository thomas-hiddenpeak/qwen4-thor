"""A killed control endpoint is transient; deadlines and bad data still fail."""
from pathlib import Path
import unittest
from unittest import mock

from tools.serve_recovery import acceptance


class WaitTests(unittest.TestCase):
    def stage(self):
        stage = acceptance.Stage.__new__(acceptance.Stage)
        stage.started = 0
        stage.plan = {'limits': {'seconds': 100}}
        stage.output = Path('CPU_ONLY_NO_IO')
        stage.process = mock.Mock()
        stage.process.poll.return_value = None
        stage.engine = mock.Mock()
        def require(value, reason):
            if not value:raise RuntimeError(reason)
        stage.engine.require.side_effect = require
        stage.sample = mock.Mock()
        return stage

    def test_control_reset_after_owned_fault_retries_only_observation(self):
        stage = self.stage();ready = dict(business_ready=True)
        stage.sample.side_effect = [ConnectionResetError(), ConnectionRefusedError(), ready]
        with mock.patch.object(acceptance.time, 'monotonic', return_value=1), \
                mock.patch.object(acceptance.time, 'sleep') as sleep:
            self.assertEqual(stage.wait(lambda s:s['business_ready']), ready)
        self.assertEqual(stage.sample.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        stage.process.send_signal.assert_not_called()

    def test_repeated_reset_still_hits_original_deadline(self):
        stage = self.stage();stage.sample.side_effect = ConnectionResetError()
        now = [0]
        def advance(seconds):now[0] += seconds
        with mock.patch.object(acceptance.time, 'monotonic', side_effect=lambda:now[0]), \
                mock.patch.object(acceptance.time, 'sleep', side_effect=advance), \
                mock.patch.object(acceptance, 'save') as save, \
                self.assertRaisesRegex(TimeoutError, 'bounded business recovery deadline'):
            stage.wait(lambda _:True, seconds=1)
        self.assertEqual(stage.sample.call_count, 4)
        save.assert_called_once_with(stage.output/'wait-timeout.json', None)

    def test_invalid_protocol_is_not_reclassified_as_a_restart_gap(self):
        stage = self.stage();stage.sample.side_effect = ValueError('invalid control identity')
        with mock.patch.object(acceptance.time, 'monotonic', return_value=1), \
                self.assertRaisesRegex(ValueError, 'invalid control identity'):
            stage.wait(lambda _:True)
        self.assertEqual(stage.sample.call_count, 1)

    def test_dead_launcher_is_not_retried(self):
        stage = self.stage();stage.process.poll.return_value = 1
        with mock.patch.object(acceptance.time, 'monotonic', return_value=1), \
                self.assertRaisesRegex(RuntimeError, 'launcher exited unexpectedly'):
            stage.wait(lambda _:True)
        stage.sample.assert_not_called()


if __name__ == '__main__': unittest.main()
