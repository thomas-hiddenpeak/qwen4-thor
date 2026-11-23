"""Optional ordinary nginx proxy -> native ingress, CPU only, no TLS claim."""
import argparse
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_gateway import gateway as files
from tools.serve_recovery import test_gateway_nginx as fixture

INGRESS = NGINX = None


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.NginxTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.NginxTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.stack = fixture.Stack(self.output, native_ingress=INGRESS)
        self.stack.wait(lambda: self.stack.bound == self.stack.generation)
        self.port = fixture.free_port()
        directory = self.stack.directory/'optional-front'
        directory.mkdir(mode=0o700)
        # Exercise the public deployment recipe with fixture ports.
        readme = (ROOT/'README.md').read_text()
        front = readme.split('```nginx\nmap $http_upgrade',1)[1].split('\n```',1)[0]
        front = ('map $http_upgrade'+front).replace('127.0.0.1:8080', f'127.0.0.1:{self.port}').replace(
            '127.0.0.1:18161', f'127.0.0.1:{self.stack.value["policy"]["listen_port"]}')
        text = f'''daemon off;
master_process on;
worker_processes 1;
pid {files.quoted(directory/'nginx.pid')};
error_log /dev/null crit;
events {{ worker_connections 64; }}
http {{
  access_log off;
  client_body_temp_path {files.quoted(directory/'body')};
  proxy_temp_path {files.quoted(directory/'proxy')};
  fastcgi_temp_path {files.quoted(directory/'fastcgi')};
  uwsgi_temp_path {files.quoted(directory/'uwsgi')};
  scgi_temp_path {files.quoted(directory/'scgi')};
{front}
}}
'''
        path = directory/'nginx.conf'
        files.save(path, text.encode())
        self.log = (directory/'process.log').open('wb')
        self.process = subprocess.Popen([str(NGINX), '-p', str(directory)+'/',
            '-c', str(path), '-e', '/dev/null'],
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
        def ready():
            if self.process.poll() is not None:
                raise AssertionError('optional nginx exited')
            try:
                return self.request()[0] == 200
            except OSError:
                return False
        self.stack.wait(ready)

    def tearDown(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=3)
        self.log.close()
        fixture.NginxTests.tearDown(self)

    def request(self, body=None, key='A'*40):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=4)
        try:
            connection.request('POST' if body is not None else 'GET',
                '/v1/chat/completions' if body is not None else '/v1/models',
                json.dumps(body) if body is not None else None,
                {'Authorization': 'Bearer '+key, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_standard_proxy_needs_only_public_bearer_and_preserves_recovery(self):
        self.assertEqual(self.request(dict(prompt='via proxy'))[0], 200)
        self.assertEqual(self.request(dict(prompt='bad'), key='wrong')[0], 401)
        self.stack.ready = False
        self.assertEqual(self.request(dict(prompt='recovering'))[0], 503)
        self.assertEqual(len(self.stack.requests), 1)

    def test_optional_front_passes_realtime_credentials_and_duplex_frames(self):
        from tools.serve_realtime.test_upgrade_ingress import (
            NativeUpgrade, fixture_ticket, frame, read_frame)
        self.rows, self.bad, self.eof = [], '', threading.Event()
        NativeUpgrade.install_peer(self)
        key = fixture_ticket()
        wire, status, headers = NativeUpgrade.connect(self, key=None, port=self.port,
            path='/v1/realtime?model=qwen3.8-flash-next',
            fields='Sec-WebSocket-Protocol: realtime, openai-insecure-api-key.'+key+'\r\n')
        self.assertEqual(status,101)
        self.assertEqual(headers['sec-websocket-protocol'],'realtime')
        self.assertNotIn(key,str(headers))
        self.assertEqual(read_frame(wire.read),(1,b'cpu-ready'))
        wire.sock.sendall(frame(b'credential session',masked=True))
        self.assertEqual(read_frame(wire.read),(1,b'credential session'))
        wire.sock.sendall(frame(b'\x03\xe8',opcode=8,masked=True))
        self.assertEqual(read_frame(wire.read),(8,b'\x03\xe8'))
        wire.sock.close()
        self.assertEqual(self.rows[-1]['headers']['X-Q4T-Client-Id'],'alpha')
        self.assertEqual(self.rows[-1]['headers']['Sec-WebSocket-Protocol'],'realtime')
        bad, status, _ = NativeUpgrade.connect(self,key=fixture_ticket(ttl=-5),port=self.port)
        self.assertEqual(status,401)
        bad.sock.close()
        self.assertEqual(len(self.rows),1)

    def test_sse_first_frame_is_delivered_before_backend_finishes(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            connection.request('POST', '/v1/chat/completions', json.dumps(dict(stream=True)),
                {'Authorization': 'Bearer '+'A'*40, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'"generation":1', response.readline())
            self.assertFalse(self.stack.release_stream.is_set())
            self.stack.release_stream.set()
            self.assertNotIn(b'[DONE]', response.read())
        finally:
            connection.close()

    def test_optional_proxy_exit_does_not_stop_native_entrypoint_or_backend(self):
        owner = fixture.supervisor.process_identity(self.stack.process.pid)
        self.process.terminate()
        self.process.wait(timeout=3)
        self.assertEqual(fixture.supervisor.process_identity(self.stack.process.pid), owner)
        self.assertEqual(self.stack.request(dict(prompt='direct after proxy exit'))[0], 200)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--ingress', type=Path, required=True)
    parser.add_argument('--nginx', type=Path, required=True)
    args, rest = parser.parse_known_args()
    INGRESS, NGINX = args.ingress.resolve(strict=True), args.nginx.resolve(strict=True)
    unittest.main(argv=[sys.argv[0], *rest])
