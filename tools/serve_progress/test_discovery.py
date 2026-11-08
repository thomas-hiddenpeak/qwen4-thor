"""Actual CPU HTTP wire and continuation-budget regressions after discovery failure."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_progress import acceptance as a


class DiscoveryTests(unittest.TestCase):
    def test_actual_discovery_wire_preserves_client_and_local_planes(self):
        seen = []
        token = 'a'*64
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                identity = (self.headers.get('X-Q4T-Client-Id'),
                    self.headers.get('X-Q4T-Gateway-Token'))
                seen.append((self.path, identity))
                wanted = ('alpha', token) if self.path == '/v1/capabilities' else (None, None)
                raw = b'{}'
                self.send_response(200 if identity == wanted else 403)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                pass

        with HTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for path in ('/v1/capabilities', '/v1/config'):
                    value = a.discovery_request(server.server_port, path,
                        dict(gateway_token=token))
                    self.assertEqual(value['http_status'], 200)
                denied = a.discovery_request(server.server_port, '/v1/capabilities',
                    dict(gateway_token='b'*64))
                self.assertEqual(denied['http_status'], 403)
                self.assertEqual(len(seen), 3)  # No transparent retry.
            finally:
                server.shutdown()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_continuation_preserves_consumed_start_and_self_check(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work') as directory:
            before = dict(starts=1, chats=1, probes=0, controls=162)
            budget = a.Budget(directory, initialize=True, baseline=before,
                limits=a.COMPLETION_LIMITS)
            for name, additional in (('starts', 2), ('chats', 8), ('probes', 1)):
                for _ in range(additional):
                    budget.reserve(name, 'only-unfinished-business')
                with self.assertRaisesRegex(RuntimeError, 'exhausted'):
                    budget.reserve(name, 'no-fourth-start-or-tenth-chat')
            value = json.loads((Path(directory) / 'counts.json').read_text())
            self.assertEqual(value['counts']['starts'], 3)
            self.assertEqual(value['counts']['chats'], 9)
            self.assertEqual(a.COMPLETION_LIMITS['seconds'], 900)


if __name__ == '__main__':
    unittest.main()
