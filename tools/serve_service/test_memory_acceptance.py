"""No GPU: reject false-positive protective-stop acceptance records."""
import copy
import unittest

from tools.serve_service.memory_acceptance import protected_terminal


class TerminalTests(unittest.TestCase):
    def fixture(self):
        return dict(state='FAILED', cleanup_confirmed=True, manual_stop=False,
            automatic_reactivation=False,
            worker=dict(state='FAILED', cleanup_confirmed=True, attempts=1,
                restarts=0, error='critical_memory_pressure',
                memory=dict(state='TRIPPED', reason='critical_memory_pressure',
                    signal_sent=True, available_bytes=99, floor_bytes=100)),
            guard=dict(status='COMMAND_FAILED', child_returncode=1))

    def test_expected_protective_failure_is_not_rewritten_as_clean_exit(self):
        value = self.fixture()
        protected_terminal(value)
        self.assertEqual(value['guard']['status'], 'COMMAND_FAILED')
        self.assertEqual(value['state'], 'FAILED')

    def test_environment_abort_and_manual_stop_cannot_pass_as_memory_trip(self):
        for parent, key, changed in (
                ('guard', 'status', 'ENV_ABORT'), ('guard', 'cleanup_error', 'x'),
                ('guard', 'child_returncode', 0), ('', 'manual_stop', True),
                ('', 'cleanup_confirmed', False),
                ('worker', 'restarts', 1), ('worker', 'attempts', 2),
                ('worker', 'error', 'memory_observation_unavailable'),
                ('memory', 'signal_sent', False),
                ('memory', 'available_bytes', 100),
                ('memory', 'state', 'OPEN')):
            with self.subTest(parent=parent, key=key):
                value = copy.deepcopy(self.fixture())
                target = value['worker']['memory'] if parent == 'memory' else (
                    value[parent] if parent else value)
                target[key] = changed
                with self.assertRaises(RuntimeError):
                    protected_terminal(value)


if __name__ == '__main__':
    unittest.main()
