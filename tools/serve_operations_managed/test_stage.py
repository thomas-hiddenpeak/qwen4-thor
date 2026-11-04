"""Acceptance safety checks without HTTP, models or GPU work."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.serve_operations_managed import stage as s


class Driver(unittest.TestCase):
    def test_ready_label_alone_cannot_pass_pressure_observation(self):
        state = dict(state='READY', updated_unix=101, attempts=1, restarts=0,
            observation=dict(operations_port=s.OPS_PORT,
                             metrics={'q4t_public_connections': 136}))
        self.assertTrue(s.expected_saturation(state, 100))
        for path, value in ((('updated_unix',), 99), (('state',), 'OBSERVATION_UNAVAILABLE'),
                            (('restarts',), 1), (('observation', 'operations_port'), 0),
                            (('observation', 'metrics', 'q4t_public_connections'), 135)):
            row = copy.deepcopy(state)
            target = row
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            self.assertFalse(s.expected_saturation(row, 100))

    def test_cleanup_front_failure_still_stops_backend_and_releases_custody(self):
        run = object.__new__(s.Run)
        run.processes = {'controller': SimpleNamespace(poll=lambda: 0)}
        run.close_sockets = Mock()
        run.custody = Mock()
        seen = []
        def frontend(index):
            seen.append('front-' + str(index))
            if index == 2:
                raise RuntimeError('injected gateway cleanup failure')
        run.stop_front = frontend
        run.stop_backend = lambda: seen.append('backend')
        with patch.object(s.client.a, 'require_idle'), \
                patch.object(s.inspection, 'listeners', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'owned cleanup errors'):
                run.cleanup()
        self.assertEqual(seen, ['front-2', 'front-1', 'backend'])
        run.custody.finish.assert_called_once_with(cleanup_confirmed=False)

    def test_no_owned_start_does_not_touch_external_resources(self):
        run = object.__new__(s.Run)
        run.processes = {}
        run.close_sockets = Mock()
        with patch.object(s.client.a, 'require_idle') as gpu, \
                patch.object(s.service, 'stop') as stop:
            run.cleanup()
        gpu.assert_not_called()
        stop.assert_not_called()

    def test_failed_identity_capture_keeps_child_registered(self):
        s.BASE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=s.BASE) as directory:
            run = object.__new__(s.Run)
            run.logs, run.processes = [], {}
            process = SimpleNamespace(pid=999999999)
            try:
                with patch.object(s, 'OUT', Path(directory)), \
                        patch.object(s.subprocess, 'Popen', return_value=process), \
                        patch.object(s.sup, 'process_identity', side_effect=ProcessLookupError):
                    with self.assertRaises(ProcessLookupError):
                        run.spawn('controller', ['no-execution'])
                self.assertIs(run.processes['controller'], process)
            finally:
                for stream in run.logs:
                    stream.close()

    def test_client_budget_is_applied_to_wire_request(self):
        run = object.__new__(s.Run)
        run.current_client = 1
        body = {'max_tokens': 512}
        result = SimpleNamespace(usage={'completion_tokens': 58})
        with patch.object(s.client.Run, 'complete', return_value=result) as complete:
            run.complete('beta', body)
        self.assertEqual(complete.call_args.args, ('beta', {'max_tokens': 64}, 1))
        self.assertEqual(body, {'max_tokens': 512})


if __name__ == '__main__':
    unittest.main()
