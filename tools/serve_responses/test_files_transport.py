"""Real Files CPU network chain: SDK, native TLS, C++ handlers and disk.

Only the parent admission decision is a synthetic Guardian peer. No mock file
results, model outputs, installed credentials, GPU or shared default state.
"""
import copy
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import threading
import time
import unittest
import uuid

from tools.serve_gateway import gateway
from tools.serve_recovery import journal, native_ingress
from tools.serve_recovery.test_gateway import config
from tools.serve_recovery.test_gateway_nginx import free_port
from tools.serve_recovery.test_native_access import certificates

ROOT = Path(__file__).resolve().parents[2]
HOST = Path(os.environ.get('Q4T_FILES_HOST', ROOT / '.q4t-work/serve-access-20261109/host/q4t_host_tests'))
INGRESS = Path(os.environ.get('Q4T_FILES_INGRESS',
    ROOT / '.q4t-work/serve-custom-tools-20261108/engine-build/q4t-ingress'))
LIMIT = 512*1024**2+65536


class Stack:
    def __init__(self, directory, certs, *, native=True, tls=True,
                 core_limit=LIMIT, edge_limit=LIMIT, legacy_core=False,
                 legacy_edge=False, prefix=''):
        self.directory = directory
        directory.mkdir(mode=0o700)
        self.logs = []
        self.backend = self.entry = self.peer = self.thread = None
        self.stop = threading.Event()
        self.token = 'e'*64
        self.admissions = []
        self.error = None
        self.context = ssl.create_default_context(cafile=str(certs/'ca.pem'))
        self.tls, self.prefix = tls and native, prefix
        base = config(directory)
        private = dict(base['backend_policy'], version=3 if legacy_core else 4,
                       revision=0, gateway_token=self.token)
        for client in private['clients']:
            client['max_body_bytes'] = 1024
            if not legacy_core:client['max_file_body_bytes'] = core_limit
        (directory/'policy.json').write_text(json.dumps(private))
        (directory/'policy.json').chmod(0o600)
        try:
            log = (directory/'backend.log').open('xb');self.logs.append(log)
            self.backend = subprocess.Popen([str(HOST), '--files-api-fixture', str(directory)],
                stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True)
            end = time.monotonic()+5
            while not (directory/'ready.json').exists():
                if self.backend.poll() is not None or time.monotonic() >= end:
                    raise RuntimeError('Files production fixture failed to start')
                time.sleep(.01)
            ready = json.loads((directory/'ready.json').read_text())
            self.port = ready['port']
            self.key, self.other_key = 'cpu-client-a', 'cpu-client-b'
            if not native:return
            self.key, self.other_key = 'A'*40, 'B'*40
            self.port = free_port()
            raw = dict(base['policy'], version=4 if legacy_edge else 5,
                listen_port=self.port, upstream_port=ready['port'],
                upstream_operations_port=ready['operations'], body_bytes=1024,
                per_client_rps=100, burst=100, http=dict(path_prefix=prefix))
            if not legacy_edge:raw['file_body_bytes'] = edge_limit
            policy = gateway.policy(raw)
            address = 'q4t-files-cpu-'+str(uuid.uuid4())
            settings = dict(version=2, parent_pid=os.getpid(),
                parent_start_ticks=journal.process_start(os.getpid()),
                address=address, policy=policy,
                tls=dict(certificate=str(certs/'server.pem'), key=str(certs/'server.key'),
                         ca='', require_client_certificate=False) if tls else None,
                tls_lease='q4t-files-tls-'+str(uuid.uuid4()) if tls else '')
            (directory/'ingress.json').write_text(json.dumps(settings))
            (directory/'ingress.json').chmod(0o600)
            def decide(lane):
                self.admissions.append(dict(lane=lane, token=self.token))
                return dict(port=ready['operations' if lane == 'control' else 'port'],
                            token=self.token)
            self.peer = native_ingress.AdmissionSocket(address, ['alpha', 'beta'],
                decide, lambda: self.entry.pid if self.entry is not None else None)
            log = (directory/'ingress.log').open('xb');self.logs.append(log)
            self.entry = subprocess.Popen([str(INGRESS), '--config', str(directory/'ingress.json')],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            def serve():
                try:
                    while not self.stop.is_set():self.peer.step(.02)
                except BaseException as error:self.error = error
            self.thread = threading.Thread(target=serve);self.thread.start()
            end = time.monotonic()+5
            while True:
                try:
                    if self.request('GET', '/v1/files')[0] == 200:break
                except (OSError, http.client.HTTPException):pass
                if self.entry.poll() is not None or time.monotonic() >= end:
                    raise RuntimeError('Native Files ingress failed to start')
                time.sleep(.02)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.entry is not None and self.entry.poll() is None:
            self.entry.terminate()
            try:self.entry.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.entry.pid, signal.SIGKILL);self.entry.wait(timeout=3)
        self.stop.set()
        if self.thread:self.thread.join(timeout=3)
        if self.peer:self.peer.close()
        if self.backend is not None:
            if self.backend.stdin and not self.backend.stdin.closed:self.backend.stdin.close()
            try:self.backend.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(self.backend.pid, signal.SIGKILL);self.backend.wait(timeout=3)
        for log in self.logs:log.close()
        self.logs = []

    def socket(self):
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        return self.context.wrap_socket(sock, server_hostname='127.0.0.1') if self.tls else sock

    def request(self, method, path, body=None, headers=None):
        connection = (http.client.HTTPSConnection('127.0.0.1', self.port,
            context=self.context, timeout=8) if self.tls else
            http.client.HTTPConnection('127.0.0.1', self.port, timeout=8))
        try:
            connection.request(method, self.prefix+path, body,
                {'Authorization': 'Bearer '+self.key, **(headers or {})})
            response = connection.getresponse()
            return response.status, response.read()
        finally:connection.close()

    def head(self, *, framing='Transfer-Encoding: chunked', content_type=None):
        return (f'POST {self.prefix}/v1/files HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                f'Authorization: Bearer {self.key}\r\n{framing}\r\n'
                'Content-Type: '+(content_type or 'multipart/form-data; boundary=f')+'\r\n\r\n').encode()


def form(data=b'actual bytes'):
    return (b'--f\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nuser_data\r\n'
            b'--f\r\nContent-Disposition: form-data; name="file"; filename="x.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\n'+data+b'\r\n--f--\r\n')


class FilesTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = Path(os.environ['Q4T_TEST_OUTPUT_DIR']).resolve()
        cls.output.mkdir(parents=True, exist_ok=True)
        cls.certs = cls.output/'certs'
        certificates(cls.certs)

    def stack(self, **settings):
        self.server = Stack(self.output/self._testMethodName, self.certs, **settings)
        self.addCleanup(self.server.close)
        return self.server

    def sdk(self, native):
        server = self.stack(native=native, tls=native, prefix='/q4t' if native else '')
        url = ('https' if native else 'http')+'://127.0.0.1:'+str(server.port)+server.prefix+'/v1'
        env = dict(os.environ, NODE_EXTRA_CA_CERTS=str(self.certs/'ca.pem'))
        result = subprocess.run(['node', 'tools/serve_responses/files_sdk.mjs', url,
            server.key, server.other_key, str(server.directory/'sdk.json')],
            cwd=ROOT, env=env, capture_output=True, timeout=90)
        (server.directory/'sdk.stdout').write_bytes(result.stdout)
        (server.directory/'sdk.stderr').write_bytes(result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        if native:self.assertEqual({r['lane'] for r in server.admissions}, {'data', 'control'})
        self.assertIsNone(server.error)

    def test_official_sdk_real_core_files_and_large_bytes(self):self.sdk(False)
    def test_official_sdk_native_tls_prefix_files_and_large_bytes(self):self.sdk(True)

    def test_malformed_chunk_trailer_never_publishes_complete_multipart(self):
        server = self.stack()
        wire = form()
        with server.socket() as sock:
            sock.sendall(server.head()+f'{len(wire):x}\r\n'.encode()+wire+
                         b'\r\n0\r\nContent-Length: 0\r\n\r\n')
            response = http.client.HTTPResponse(sock);response.begin()
            self.assertEqual(response.status, 400);response.read()
        status, raw = server.request('GET', '/v1/files')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['data'], [])

    def test_early_core_error_interrupts_paused_tls_upload(self):
        server = self.stack(core_limit=1024)
        with server.socket() as sock:
            sock.sendall(server.head()+b'1000\r\n'+b'x'*4096+b'\r\n')
            begin = time.monotonic()
            response = http.client.HTTPResponse(sock);response.begin()
            try:
                status, raw = response.status, response.read()
            finally:response.close()
            self.assertEqual(status, 413, raw)
            self.assertEqual(json.loads(raw)['error']['code'], 'body_too_large')
            self.assertLess(time.monotonic()-begin, 2)
        self.assertEqual(json.loads(server.request('GET', '/v1/files')[1])['data'], [])

    def test_changed_generation_before_frame_completion_aborts_publication(self):
        server = self.stack()
        wire = form()
        with server.socket() as sock:
            sock.sendall(server.head()+f'{len(wire):x}\r\n'.encode()+wire+b'\r\n')
            end = time.monotonic()+2
            # Initial ready list + upload admission + preconnect authorization.
            while len(server.admissions) < 4 and time.monotonic() < end:time.sleep(.01)
            self.assertGreaterEqual(len(server.admissions), 4)
            server.token = 'f'*64
            sock.sendall(b'0\r\n\r\n')
            response = http.client.HTTPResponse(sock);response.begin()
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.read())['error']['code'], 'service_recovering')
        server.token = 'e'*64
        self.assertEqual(json.loads(server.request('GET', '/v1/files')[1])['data'], [])

    def test_legacy_gateway_cap_and_json_cap_remain_independent(self):
        server = self.stack(legacy_edge=True, tls=False)
        with server.socket() as sock:
            sock.sendall(server.head(framing='Content-Length: 2048'))
            response = http.client.HTTPResponse(sock);response.begin()
            self.assertEqual(response.status, 413)
            self.assertEqual(json.loads(response.read())['error']['code'], 'gateway_body_too_large')
        self.assertEqual(server.request('POST', '/v1/responses', b'x'*2048,
                                        {'Content-Type': 'application/json'})[0], 413)
        wire = form()
        status, raw = server.request('POST', '/v1/files', wire,
            {'Content-Type': 'multipart/form-data; boundary=f'})
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)['bytes'], len(b'actual bytes'))

    def test_expect_continue_and_incremental_client_framing(self):
        server = self.stack(tls=False)
        wire = form(bytes(range(256)))
        with server.socket() as sock:
            sock.sendall(server.head(framing='Expect: 100-continue\r\nTransfer-Encoding: chunked'))
            interim = b''
            while b'\r\n\r\n' not in interim:interim += sock.recv(1)
            self.assertEqual(interim, b'HTTP/1.1 100 Continue\r\n\r\n')
            for index in range(0, len(wire), 3):
                part = wire[index:index+3]
                sock.sendall(f'{len(part):x}\r\n'.encode()+part+b'\r\n')
            sock.sendall(b'0\r\n\r\n')
            response = http.client.HTTPResponse(sock);response.begin()
            self.assertEqual(response.status, 200)
            file = json.loads(response.read())
        self.assertEqual(server.request('GET', '/v1/files/'+file['id']+'/content'),
                         (200, bytes(range(256))))


if __name__ == '__main__':unittest.main()
