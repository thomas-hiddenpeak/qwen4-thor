"""No-listener driver guards, custody cleanup and explicit TCP-source binding."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tools.serve_gateway import sources_stage as stage


class Driver(unittest.TestCase):
    def setUp(self):
        parent = stage.ROOT/'build/gateway-source-driver-tests'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))

    def test_fixed_matrix_matches_attempt_bounds(self):
        rows = stage.matrix()
        self.assertEqual(len({r['name'] for r in rows}), 21)
        self.assertEqual(sum(r['path'] == '/v1/chat/completions' for r in rows)+4,
                         stage.LIMITS['generation_attempts'])
        self.assertEqual(sum(r['expected'] == 403 for r in rows), 16)
        self.assertEqual({r['source'] for r in rows}, {'127.0.0.1', '127.0.0.2', '127.0.0.3'})
        self.assertEqual(next(r for r in rows if r['name'] == 'headers-only')['body'], None)
        self.assertEqual(stage.matrix(), rows)

    def test_missing_guard_fails_before_business_directory(self):
        with patch.object(stage, 'OUT', self.root/'execution'), \
             patch.object(stage.h, 'require_guard', side_effect=RuntimeError('missing lease')):
            with self.assertRaisesRegex(RuntimeError, 'missing lease'):
                stage.Run({})
        self.assertFalse((self.root/'execution/business').exists())

    def test_source_address_reaches_tcp_and_tls_still_verifies_hostname(self):
        context = Mock()
        context.check_hostname = True
        raw = Mock()
        with patch.object(stage.socket, 'create_connection', return_value=raw) as create:
            connection = stage.Connection('q4t.internal.test', 18451, timeout=8,
                context=context, source_address=('127.0.0.2', 0))
            connection.connect()
        create.assert_called_once_with(('127.0.0.1', 18451), 8,
                                       source_address=('127.0.0.2', 0))
        context.wrap_socket.assert_called_once_with(raw, server_hostname='q4t.internal.test')
        self.assertIs(connection.sock, context.wrap_socket.return_value)

    def test_tls_failure_closes_only_its_unwrapped_socket(self):
        context, raw = Mock(), Mock()
        context.wrap_socket.side_effect = RuntimeError('TLS rejected')
        with patch.object(stage.socket, 'create_connection', return_value=raw):
            connection = stage.Connection('q4t.internal.test', 18451, timeout=8,
                context=context, source_address=('127.0.0.2', 0))
            with self.assertRaisesRegex(RuntimeError, 'TLS rejected'):
                connection.connect()
        raw.close.assert_called_once_with()

    def fake(self, failure):
        run = object.__new__(stage.Run)
        run.started = time.monotonic()
        run.counts = dict(starts=0, generation_attempts=0, controls=0)
        run.children = Mock()
        run.sockets = [Mock()]
        run.launch = Mock(side_effect=failure)
        run.source_checks = Mock()
        run.tools = Mock()
        with patch.object(stage.s, 'OUT', self.root), \
             patch.object(stage.s.a, 'require_idle'), \
             patch.object(stage.socket, 'socket'), \
             patch.object(stage.signal, 'setitimer') as timer:
            with self.assertRaisesRegex(RuntimeError, str(failure)):
                run.execute()
        run.children.cleanup.assert_called_once_with()
        run.source_checks.assert_not_called()
        run.tools.assert_not_called()
        run.sockets[0].close.assert_called_once_with()
        self.assertEqual(timer.call_args.args, (stage.signal.ITIMER_REAL, 0))
        return json.loads((self.root/'result.json').read_bytes())

    def test_startup_failure_still_cleans_owned_children_and_records_failure(self):
        value = self.fake(RuntimeError('startup failed after Popen'))
        self.assertEqual(value['status'], 'FAILED')
        self.assertTrue(value['cleanup_confirmed'])
        self.assertIn('startup failed', value['error'])

    def test_environment_abort_is_preserved_and_cannot_run_business(self):
        value = self.fake(RuntimeError('ENV_ABORT: competing process'))
        self.assertEqual(value['status'], 'ENV_ABORT')
        self.assertTrue(value['cleanup_confirmed'])


if __name__ == '__main__':
    unittest.main()
