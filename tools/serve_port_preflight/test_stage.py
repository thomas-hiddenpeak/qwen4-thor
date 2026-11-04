"""Fault cleanup of the two bounded preflight workers; no sockets/model/GPU."""
import unittest
from unittest.mock import Mock, patch
from tools.serve_port_preflight import stage as s


class Driver(unittest.TestCase):
    def test_probe_stop_failure_still_stops_managed_services(self):
        run = object.__new__(s.Run)
        run.stop_probes = Mock(side_effect=RuntimeError('probe cleanup failure'))
        with patch.object(s.profile.Run, 'cleanup') as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'probe cleanup failure'):
                run.cleanup()
            cleanup.assert_called_once()

    def test_probe_escalation_only_targets_owned_running_probe(self):
        run = object.__new__(s.Run)
        probe = Mock()
        probe.poll.return_value = None
        probe.wait.side_effect = [s.subprocess.TimeoutExpired('owned', 5), 0]
        controller = Mock()
        completed = Mock()
        completed.poll.return_value = 1
        run.processes = {'controller': controller, 'port-check-public': completed,
                         'port-check-operations': probe}
        run.stop_probes()
        probe.terminate.assert_called_once()
        probe.kill.assert_called_once()
        controller.poll.assert_not_called()
        completed.terminate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
