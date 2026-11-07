"""Targeted real TCP teardown checks; no HTTP, model or GPU calls."""
from pathlib import Path
import socket
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_service import nvml_acceptance as stage


class PortTests(unittest.TestCase):
    def test_live_listener_is_rejected(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            with self.assertRaises(OSError):
                stage.require_port_available(listener.getsockname()[1])

    def test_server_time_wait_does_not_imply_live_listener(self):
        with socket.socket() as listener, socket.socket() as client:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            client.settimeout(2)
            client.connect(('127.0.0.1', port))
            accepted, _ = listener.accept()
            with accepted:
                accepted.settimeout(2)
                accepted.shutdown(socket.SHUT_WR)
                self.assertEqual(client.recv(1), b'')
                client.close()
                self.assertEqual(accepted.recv(1), b'')
        entries = [line.split() for line in Path('/proc/net/tcp').read_text().splitlines()[1:]]
        self.assertTrue(any(int(row[1].split(':')[1], 16) == port and row[3] == '06'
                            for row in entries), 'real server TIME_WAIT required')
        with socket.socket() as probe, self.assertRaises(OSError):
            probe.bind(('127.0.0.1', port))
        stage.require_port_available(port)


if __name__ == '__main__':
    unittest.main()
