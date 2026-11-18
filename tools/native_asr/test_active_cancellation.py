"""CPU sockets prove that the cancellation driver requires live execution."""
import json
import os
from pathlib import Path
import socket
import tempfile
import types
import unittest
from unittest import mock

from tools.native_asr import service_acceptance as service


class ActiveCancellation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_OUTPUT_DIR'])
        self.addCleanup(self.temporary.cleanup)
        self.stage = object.__new__(service.Stage)
        self.stage.output = Path(self.temporary.name)
        self.stage.policy = dict(upstream_operations_port=12345)
        self.stage.budget = mock.Mock()
        self.before = dict(q4t_active_chats=0, q4t_seq_slots_free=1,
            q4t_seq_slots_total=1, q4t_requests_total=10, q4t_execution_steps_total=20)
        listener = socket.socket();listener.bind(('127.0.0.1', 0));listener.listen()
        self.addCleanup(listener.close)
        client = socket.create_connection(listener.getsockname())
        self.peer, _ = listener.accept();self.peer.setblocking(False)
        self.addCleanup(client.close);self.addCleanup(self.peer.close)
        self.connection = types.SimpleNamespace(sock=client)

    def test_real_socket_stays_open_until_admitted_and_progressing(self):
        rows = [self.before,
            dict(self.before, q4t_active_chats=1, q4t_seq_slots_free=0, q4t_requests_total=11),
            dict(self.before, q4t_requests_total=11, q4t_execution_steps_total=21),
            dict(self.before, q4t_active_chats=1, q4t_seq_slots_free=0,
                 q4t_requests_total=11, q4t_execution_steps_total=22)]
        def value(_):
            with self.assertRaises(BlockingIOError):self.peer.recv(1)
            return rows.pop(0)
        with mock.patch('tools.serve_ops.transport.get', return_value=dict(http_status=200, body='metrics')), \
             mock.patch.object(service, 'metric_values', side_effect=value), \
             mock.patch.object(service.time, 'sleep'):
            observed = self.stage.cancel_running(self.connection, 'cancel', self.before)
        self.assertFalse(rows)
        self.assertEqual(observed['q4t_execution_steps_total'], 22)
        self.peer.settimeout(1);self.assertEqual(self.peer.recv(1), b'')
        record = json.loads((self.stage.output/'cancel-active.json').read_text())
        self.assertTrue(record['active_execution_observed'])
        self.assertEqual(len(record['observations']), 4)

    def test_finished_request_is_not_falsely_counted_as_active_cancellation(self):
        finished = dict(self.before, q4t_requests_total=11, q4t_execution_steps_total=30)
        with mock.patch('tools.serve_ops.transport.get', return_value=dict(http_status=200, body='metrics')), \
             mock.patch.object(service, 'metric_values', return_value=finished), \
             mock.patch.object(service.time, 'monotonic', side_effect=[0, 0, 31]), \
             mock.patch.object(service.time, 'sleep'):
            with self.assertRaises(TimeoutError):
                self.stage.cancel_running(self.connection, 'cancel', self.before)
        with self.assertRaises(BlockingIOError):self.peer.recv(1)
        record = json.loads((self.stage.output/'cancel-active.json').read_text())
        self.assertFalse(record['active_execution_observed'])


if __name__ == '__main__': unittest.main()
