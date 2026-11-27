import unittest

from tools.serve_responses import chatkit_pending_profiles as suite


class PendingBrowserHandoff(unittest.TestCase):
    def setUp(self):
        actor = suite.package.read(suite.PROOF.with_name('device.json'))['actor']
        self.first = {k: actor[k] for k in ('sdk', 'workflow', 'thread', 'call', 'original_item')}
        self.first.update(phase='primary', requests=4, original_user=actor['history'][0],
                          original_sha256='a' * 64)
        self.second = dict(self.first, **{k: actor[k] for k in (
            'history', 'answer', 'unchanged_pending_call', 'late_output',
            'foreign_session', 'foreign_project')})
        self.second.update(phase='light', requests=10)

    def test_original_call_survives_the_capacity_change(self):
        suite.check(self.first, 'primary')
        suite.check(self.second, 'light', self.first)

    def test_primary_must_leave_the_call_pending(self):
        with self.assertRaisesRegex(RuntimeError, 'leave the call pending'):
            suite.check(dict(self.first, answer='42'), 'primary')

    def test_distinct_thread_call_or_rewritten_original_items_are_rejected(self):
        for key in (*suite.IDENTITY, 'original_sha256'):
            value = 'b' * 64 if key == 'original_sha256' else self.second[key] + 'b'
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'reopen'):
                suite.check(dict(self.second, **{key: value}), 'light', self.first)

    def test_replayed_input_stale_output_and_foreign_access_are_rejected(self):
        for key, value in [('history', self.second['history'][:2] * 2),
                           ('late_output', 200), ('foreign_project', 200),
                           ('answer', '41'), ('unchanged_pending_call', False)]:
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'incomplete'):
                suite.check(dict(self.second, **{key: value}), 'light', self.first)


if __name__ == '__main__':
    unittest.main()
