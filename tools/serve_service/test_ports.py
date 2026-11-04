"""Known listener conflicts must reject before managed backend execution."""
from contextlib import contextmanager
import errno
import signal
import socket
import sys
import unittest
from unittest.mock import Mock, patch

from tools.serve_service import package as pkg, supervisor as sup
from tools.serve_service.test_service import Case


@contextmanager
def occupied(host='127.0.0.1'):
    with socket.socket() as listener:
        listener.bind((host, 0))
        listener.listen(1)
        yield listener


def unused_port():
    with socket.socket() as candidate:
        candidate.bind(('127.0.0.1', 0))
        return candidate.getsockname()[1]


class Ports(unittest.TestCase):
    setUp = Case.setUp

    def manifest(self, public, operations=0, restarts=0):
        cfg = pkg.settings(public, restarts, operations_port=operations)
        cfg.update(poll_seconds=.01, startup_seconds=1, stop_seconds=.1,
                   kill_seconds=.2, restart_delay_seconds=.01)
        return dict(settings=cfg, repository=str(pkg.ROOT),
                    protocol_sha256='protocol', files={'q4t': 'binary'})

    def refuse(self, manifest, expected):
        output = self.root / 'run'
        output.mkdir()
        with patch.object(pkg, 'verify', return_value=manifest), \
             patch.object(sup, 'backend_command', return_value=[sys.executable, '-c', 'pass']), \
             patch.object(sup.subprocess, 'Popen',
                          side_effect=AssertionError('backend launch reached')) as launch:
            code = sup.run(self.root, 'fixture', output,
                read_memory=lambda: dict(total_bytes=8*1024**3, available_bytes=4*1024**3))
        state = pkg.read(output / 'status.json')
        self.assertFalse(launch.called, 'port conflict reached backend launch')
        self.assertEqual((code, state['attempts'], state['restarts']), (1, 0, 0))
        self.assertTrue(state['cleanup_confirmed'])
        self.assertIn(expected, state['error'])
        self.assertFalse(list(output.glob('attempt-*.json')))
        return state

    def test_occupied_operations_refuses_before_launch(self):
        with occupied() as holder:
            port = holder.getsockname()[1]
            state = self.refuse(self.manifest(unused_port(), port), 'operations listener')
            self.assertIn(str(port), state['error'])
            self.assertEqual(holder.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1)

    def test_occupied_public_refuses_before_launch(self):
        with occupied() as holder:
            self.refuse(self.manifest(holder.getsockname()[1], unused_port()), 'public listener')

    def test_wildcard_listener_also_blocks_loopback_operations(self):
        with occupied('0.0.0.0') as holder:
            self.refuse(self.manifest(unused_port(), holder.getsockname()[1]), 'operations listener')

    def test_failed_second_bind_releases_check_sockets_and_preserves_owner(self):
        public = unused_port()
        with occupied() as holder:
            config = self.manifest(public, holder.getsockname()[1])['settings']
            with self.assertRaisesRegex(RuntimeError, 'operations listener'):
                sup.preflight_ports(config)
            with socket.socket() as again:
                again.bind(('127.0.0.1', public))
            self.assertEqual(holder.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1)

    def test_disabled_operations_and_successful_checks_leave_no_reservation(self):
        public, operations = unused_port(), unused_port()
        for selected in (0, operations):
            sup.preflight_ports(self.manifest(public, selected)['settings'])
            with socket.socket() as again:
                again.bind(('127.0.0.1', public))
            if selected:
                with socket.socket() as again:
                    again.bind(('127.0.0.1', selected))

    def test_socket_allocation_failure_is_actionable_and_precedes_launch(self):
        manifest = self.manifest(18998, 18997)
        with patch.object(sup.socket, 'socket', side_effect=OSError(errno.EMFILE, 'descriptor limit')):
            self.refuse(manifest, 'public listener')

    def test_recovery_rechecks_operations_before_another_backend(self):
        output = self.root / 'run'
        output.mkdir()
        public, operations = unused_port(), unused_port()
        manifest = self.manifest(public, operations, restarts=3)
        command = [sys.executable, '-c', 'import time;time.sleep(30)']
        held = []
        def observe(pid, port, ops):
            holder = socket.socket()
            held.append(holder)
            holder.bind(('127.0.0.1', ops))
            holder.listen(1)
            sup.signal_owner(sup.process_identity(pid), signal.SIGKILL)
            process = dict(pid=pid, executable_sha256='binary', listener_scope='loopback')
            return dict(process=process, protocol='protocol', ready=True,
                metrics={'q4t_gpu_healthy': 1}, operations_process=process,
                operations_port=ops, observability_version=5,
                configuration=dict(sup.expected_configuration(), operations_port=ops))
        try:
            with patch.object(pkg, 'verify', return_value=manifest), \
                 patch.object(sup, 'backend_command', return_value=command), \
                 patch.object(pkg, 'command', return_value=command):
                code = sup.run(self.root, 'fixture', output, observe=observe,
                    read_memory=lambda: dict(total_bytes=8*1024**3, available_bytes=4*1024**3))
            state = pkg.read(output / 'status.json')
            self.assertEqual((code, state['attempts'], state['restarts']), (1, 1, 1))
            self.assertIn('operations listener', state['error'])
            self.assertTrue(state['cleanup_confirmed'])
            self.assertEqual(len(held), 1)
        finally:
            for holder in held:
                holder.close()


if __name__ == '__main__':
    unittest.main()
