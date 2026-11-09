"""Real Uploads C++ HTTP/TLS and pinned SDK; only Guardian admission is adapted."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import socket
import time
import unittest

from tools.serve_recovery.test_native_access import certificates
from tools.serve_responses.test_files_transport import Stack, ROOT


def part_body(bytes=b'abc'):
    return b'--p\r\nContent-Disposition: form-data; name="data"\r\n\r\n'+bytes+b'\r\n--p--\r\n'


class UploadsTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = Path(os.environ['Q4T_TEST_OUTPUT_DIR']).resolve()
        cls.output.mkdir(parents=True, exist_ok=True)
        cls.certs = cls.output/'uploads-certs'
        certificates(cls.certs)

    def stack(self, **options):
        server = Stack(self.output/self._testMethodName, self.certs, **options)
        self.addCleanup(server.close)
        return server

    def create(self, server, count=3):
        status, raw = server.request('POST', '/v1/uploads', json.dumps(dict(bytes=count,
            filename='real.txt', purpose='user_data', mime_type='text/plain')),
            {'Content-Type': 'application/json'})
        self.assertEqual(status, 200, raw)
        return json.loads(raw)['id']

    def head(self, server, identity):
        return (f'POST {server.prefix}/v1/uploads/{identity}/parts HTTP/1.1\r\n'
                'Host: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n'
                f'Authorization: Bearer {server.key}\r\n'
                'Content-Type: multipart/form-data; boundary=p\r\n\r\n').encode()

    def records(self, server, format):
        rows = []
        for path in (server.directory/'objects').glob('*.json'):
            if path.name.startswith('.'):continue
            try:record = json.loads(path.read_text())['record']
            except FileNotFoundError:continue
            if record['data'].get('format') == format:rows.append(record)
        return rows

    def sdk(self, native):
        server = self.stack(native=native, tls=native, prefix='/uploads-test' if native else '', json_limit=65536)
        url = ('https' if native else 'http')+'://127.0.0.1:'+str(server.port)+server.prefix+'/v1'
        env = dict(os.environ, NODE_EXTRA_CA_CERTS=str(self.certs/'ca.pem'))
        result = subprocess.run(['node', 'tools/serve_responses/uploads_sdk.mjs', url,
            server.key, server.other_key, str(server.directory/'sdk.json')], cwd=ROOT,
            env=env, capture_output=True, timeout=90)
        (server.directory/'sdk.stdout').write_bytes(result.stdout)
        (server.directory/'sdk.stderr').write_bytes(result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        if native:self.assertEqual({row['lane'] for row in server.admissions}, {'data', 'control'})
        self.assertIsNone(server.error)

    def test_official_sdk_direct_core_order_md5_and_large_part(self):self.sdk(False)
    def test_official_sdk_native_tls_prefix_order_md5_and_large_part(self):self.sdk(True)

    def test_cancelled_part_error_survives_body_sent_after_response(self):
        server = self.stack(native=False)
        identity = self.create(server)
        status, _ = server.request('POST', '/v1/uploads/'+identity+'/cancel', b'')
        self.assertEqual(status, 200)
        with server.socket() as sock:
            sock.sendall(self.head(server, identity))
            response = http.client.HTTPResponse(sock);response.begin()
            self.assertEqual(response.status, 409)
            self.assertEqual(json.loads(response.read())['error']['code'], 'upload_not_pending')
            body = part_body()
            sock.sendall(f'{len(body):x}\r\n'.encode()+body+b'\r\n0\r\n\r\n')
            sock.shutdown(socket.SHUT_WR)
            self.assertEqual(sock.recv(1), b'')
        self.assertEqual(self.records(server, 'q4t.upload_part.v1'), [])

    def test_malformed_http_trailer_does_not_publish_valid_multipart_part(self):
        server = self.stack()
        identity = self.create(server)
        body = part_body()
        with server.socket() as sock:
            sock.sendall(self.head(server, identity)+f'{len(body):x}\r\n'.encode()+body+
                         b'\r\n0\r\nContent-Length: 0\r\n\r\n')
            response = http.client.HTTPResponse(sock);response.begin()
            self.assertEqual(response.status, 400);response.read();response.close()
        rows = self.records(server, 'q4t.upload.v1')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['data']['parts'], [])
        self.assertEqual(self.records(server, 'q4t.upload_part.v1'), [])

    def test_early_core_limit_interrupts_paused_tls_part(self):
        server = self.stack(core_limit=1024)
        identity = self.create(server)
        with server.socket() as sock:
            sock.sendall(self.head(server, identity)+b'1000\r\n'+b'x'*4096+b'\r\n')
            start = time.monotonic()
            response = http.client.HTTPResponse(sock);response.begin()
            self.assertEqual(response.status, 413)
            value = json.loads(response.read());response.close()
            self.assertIn(value['error']['code'], ('body_too_large', 'body_limit_exceeded'))
            self.assertLess(time.monotonic()-start, 2)
        self.assertEqual(self.records(server, 'q4t.upload.v1')[0]['data']['parts'], [])

    def test_cancel_stays_admissible_while_two_parts_fill_client_data_slots(self):
        server = self.stack()
        identity = self.create(server)
        sockets = [server.socket(), server.socket()]
        try:
            partial = b'--p\r\nContent-Disposition: form-data; name="data"\r\n\r\nabc'
            for sock in sockets:
                sock.sendall(self.head(server, identity)+f'{len(partial):x}\r\n'.encode()+partial+b'\r\n')
            end = time.monotonic()+3
            while len(self.records(server, 'q4t.binary_stage.v1')) < 2:
                if time.monotonic() > end:self.fail('Both production part handlers did not enter staging')
                time.sleep(.01)
            status, raw = server.request('POST', '/v1/uploads/'+identity+'/cancel', '{}', {'Content-Type': 'application/json'})
            self.assertEqual(status, 200, raw)
            self.assertEqual(json.loads(raw)['status'], 'cancelled')
            tail = b'\r\n--p--\r\n'
            for sock in sockets:
                sock.sendall(f'{len(tail):x}\r\n'.encode()+tail+b'\r\n0\r\n\r\n')
                response = http.client.HTTPResponse(sock);response.begin()
                self.assertEqual(response.status, 409)
                response.read();response.close()
            self.assertIn('control', [row['lane'] for row in server.admissions])
            self.assertEqual(self.records(server, 'q4t.upload_part.v1'), [])
        finally:
            for sock in sockets:sock.close()

    def test_json_requests_do_not_inherit_file_capacity_and_legacy_part_limit(self):
        server = self.stack(json_limit=1024, legacy_core=True, legacy_edge=True)
        identity = self.create(server)
        status, _ = server.request('POST', '/v1/uploads', ' '*2048+'{}', {'Content-Type': 'application/json'})
        self.assertEqual(status, 413)
        status, _ = server.request('POST', '/v1/uploads/'+identity+'/parts', part_body(b'x'*2048),
                                   {'Content-Type': 'multipart/form-data; boundary=p'})
        self.assertEqual(status, 413)
        self.assertEqual(self.records(server, 'q4t.upload.v1')[0]['data']['parts'], [])

    def test_native_complete_json_larger_than_four_k_uses_ordinary_json_limit(self):
        server = self.stack(json_limit=16384)
        identity = self.create(server, 0)
        body = ' '*6000+json.dumps(dict(part_ids=[]))
        status, raw = server.request('POST', '/v1/uploads/'+identity+'/complete', body,
                                     {'Content-Type': 'application/json'})
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)['file']['bytes'], 0)
