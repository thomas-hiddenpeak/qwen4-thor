"""Do not identify an unknown process from an empty /proc cmdline."""
import copy
import unittest
from tools.serve_port_preflight import finish as f


class Observation(unittest.TestCase):
    def fixture(self):
        model = dict(pid=42, start_ticks=100, boot_id='boot')
        policy = f.s.resources.policy(32, 64, 8)
        binding = dict(record=dict(root='/sys/fs/cgroup/fixture', policy=policy),
                       owners=dict(backend=model))
        members = [dict(owner=model, parent=1, argv=['q4t'], membership='/fixture/payload')]
        for pid in range(43, 47):
            members.append(dict(owner=dict(model, pid=pid), parent=42,
                argv=['q4t-grammar-worker', '--q4t-grammar-worker', '42', '2147483648'],
                membership='/fixture/payload'))
        row = {'effective_limits': f.s.resources.limits(policy), 'memory.current': 1,
            'memory.peak': 1, 'memory.swap.current': 0, 'pids.current': 5,
            'memory.events': dict(high=0, max=0, oom=0, oom_kill=0),
            'pids.events': dict(max=0), 'processes': members}
        later = copy.deepcopy(row)
        later['processes'][1]['argv'] = []
        return [row, later], binding

    def test_known_full_identity_can_keep_role_when_later_argv_empty(self):
        rows, binding = self.fixture()
        original = copy.deepcopy(rows)
        result = f.audit_samples(rows, binding)
        self.assertEqual(rows, original)
        self.assertEqual(len(result['empty_argv_with_earlier_role_evidence']), 1)
        self.assertEqual(result['empty_argv_with_earlier_role_evidence'][0]['earlier_role_sample'], 0)

    def test_empty_argv_without_earlier_identity_is_rejected(self):
        rows, binding = self.fixture()
        with self.assertRaisesRegex(RuntimeError, 'lacks earlier role evidence'):
            f.audit_samples(rows[1:], binding)

    def test_reused_pid_with_different_start_cannot_inherit_role(self):
        rows, binding = self.fixture()
        rows[1]['processes'][1]['owner']['start_ticks'] += 1
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            f.audit_samples(rows, binding)

    def test_changed_nonempty_argv_is_not_hidden_by_earlier_role(self):
        rows, binding = self.fixture()
        rows[1]['processes'][1]['argv'] = ['other-program']
        with self.assertRaisesRegex(RuntimeError, 'unidentified payload'):
            f.audit_samples(rows, binding)


if __name__ == '__main__':
    unittest.main()
