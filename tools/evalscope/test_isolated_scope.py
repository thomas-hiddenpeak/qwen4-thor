"""Scope ownership and completion contracts without model/GPU execution."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from isolated_scope import IsolatedScope, stop_owned_scope, scope_properties


class ScopeContracts(unittest.TestCase):
    def test_reject_foreign_unit_name(self):
        with patch('isolated_scope.unit_properties') as query:
            with self.assertRaises(ValueError):
                IsolatedScope([], cwd='.', env={}, log_path='log',
                              unit='ssh.scope', memory_max=None, evidence='unused')
            query.assert_not_called()

    def test_reject_bad_limit_before_query(self):
        with patch('isolated_scope.unit_properties') as query:
            with self.assertRaises(ValueError):
                IsolatedScope([], cwd='.', env={}, log_path='log',
                              unit='q4t-test.scope', memory_max=0, evidence='unused')
            query.assert_not_called()

    def test_existing_unit_does_not_launch_or_stop(self):
        with patch('isolated_scope.unit_properties', return_value={'LoadState':'loaded'}), \
                patch('isolated_scope.subprocess.Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'existing unit'):
                IsolatedScope([], cwd='.', env={}, log_path='log',
                              unit='q4t-test.scope', memory_max=None, evidence='unused')
            launch.assert_not_called()

    def test_cleanup_refuses_replaced_owner(self):
        scope = IsolatedScope.__new__(IsolatedScope)
        scope.closed, scope.pidfd = False, None
        scope.token = 'original'
        scope.log = SimpleNamespace(close=lambda: None)
        scope._properties = lambda: {'LoadState': 'loaded', 'Description': 'other'}
        with patch('isolated_scope.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'another scope owner'):
                scope.close()
            run.assert_not_called()
        self.assertFalse(scope.closed)

    def test_missing_exit_does_not_invent_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope = IsolatedScope.__new__(IsolatedScope)
            scope.evidence, scope.returncode = Path(tmp), None
            scope.launcher = SimpleNamespace(poll=lambda: 0)
            with self.assertRaisesRegex(RuntimeError, 'without payload exit'):
                scope.poll()

    def test_foreign_exit_token_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope = IsolatedScope.__new__(IsolatedScope)
            scope.evidence, scope.returncode, scope.token = Path(tmp), None, 'ours'
            (Path(tmp)/'payload-exit.json').write_text(json.dumps(dict(token='other',returncode=0)))
            with self.assertRaisesRegex(RuntimeError, 'exit token mismatch'):
                scope.poll()

    def test_signal_uses_pidfd(self):
        scope = IsolatedScope.__new__(IsolatedScope)
        scope.pidfd, scope.poll = 123, lambda: None
        with patch('isolated_scope.signal.pidfd_send_signal') as send:
            scope.kill()
        self.assertEqual(send.call_args.args[0],123)

    def test_fallback_cleanup_checks_token_before_signal(self):
        token = 'q4t-scope-' + 'a' * 32
        with patch('isolated_scope.scope_properties', return_value={
                'LoadState': 'loaded', 'Description': 'other'}), \
                patch('isolated_scope.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'another scope owner'):
                stop_owned_scope('q4t-test.scope', token)
            run.assert_not_called()

    def test_absent_scope_does_not_invent_stop(self):
        token = 'q4t-scope-' + 'a' * 32
        with patch('isolated_scope.scope_properties', return_value={'LoadState': 'not-found'}), \
                patch('isolated_scope.subprocess.run') as run:
            result = stop_owned_scope('q4t-test.scope', token)
            run.assert_not_called()
        self.assertTrue(result['unit_removed'])
        self.assertTrue(result['already_absent'])
        self.assertIsNone(result['stop_rc'])

    def test_empty_properties_fail_closed(self):
        with patch('isolated_scope.subprocess.run', return_value=SimpleNamespace(
                stdout='', stderr='query failed', returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'properties unavailable'):
                scope_properties('q4t-test.scope')


if __name__ == '__main__':
    unittest.main()
