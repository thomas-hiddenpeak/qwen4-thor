"""Real nginx and gateway worker, CPU HTTP backend and private Guardian peer.

No native model, GPU, NVML or shared GPU lock. Requires an explicit nginx ELF;
this local integration suite is separate from portable host contracts.
"""
import argparse
import ctypes
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import binding, control, journal
from tools.serve_recovery.test_gateway import config, snapshot
from tools.serve_recovery.test_runtime import children, signal_identity
from tools.serve_service import supervisor

NGINX = None


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def native_config(value, token):
    expected = supervisor.expected_configuration()
    result = dict(profile='q4t-config-v1', protocol_source_sha256='c'*64,
        observability_version=5,
        memory_admission=dict(enabled=True, hard_limit=False,
            min_available_mib=dict(effective=2048), recovery_threshold_mib=2560),
        runtime=dict(mtp_enabled=True, structured_backend=True),
        limits=dict(business_connections=8, observation_connections=4,
            control_connections=4, connection_handlers=24,
            public_connection_handlers=16, operations_connection_handlers=8,
            unclassified_shares_total=False),
        listeners=dict(public=dict(host='127.0.0.1', port=value['policy']['upstream_port']),
            operations=dict(host='127.0.0.1', port=value['policy']['upstream_operations_port'],
                enabled=True, unclassified_shares_operations_total=True)),
        client_policy=dict(profile='q4t-gateway-clients-v1', mutable=False,
            credential_values_redacted=True,
            **binding.cp.binding(binding.policy_bytes(value['backend_policy'], token))))
    for name in ('max_context_tokens', 'max_sequences', 'prefill_chunk_tokens',
                 'max_inflight_requests', 'max_queued_requests'):
        result[name] = dict(effective=expected[name])
    result['request_deadline_ms'] = dict(effective=120000)
    return result


class Stack:
    def __init__(self, output, *, native_ingress=None, policy_overrides=None,
                 tls=None, ssl_context=None, access=None):
        self.directory = Path(tempfile.mkdtemp(
            prefix='native-' if native_ingress else 'nginx-', dir=output))
        self.service_id = access['service_id'] if access is not None else str(uuid.uuid4())
        self.gateway_generation = 1
        self.native_ingress = native_ingress
        self.generation, self.token = 1, 'e'*64
        self.ready, self.peer_available = True, True
        self.bound = 0
        self.stopped = threading.Event()
        self.release_stream = threading.Event()
        self.stream_started = threading.Event()
        self.stream_disconnected = threading.Event()
        self.requests = []
        self.forwarded = []
        self.model_requests = []
        self.application_requests = []
        self.held_application_paths = set()
        self.release_applications = threading.Event()
        self.events = []
        self.errors = []
        self.servers, self.threads = [], []
        self.value = None
        self.ssl_context = ssl_context
        stack = self
        class Backend(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_args):
                pass
            def response(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True
            def do_GET(self):
                if self.path in ('/livez', '/readyz'):
                    self.response(200, dict(status='ready', live=True, ready=True))
                elif self.path == '/v1/config':
                    self.response(200, native_config(stack.value, stack.token))
                elif self.path == '/v1/models':
                    self.response(200, dict(object='list', data=[dict(id='cpu-fixture')]))
                elif self.path.startswith('/v1/models/'):
                    self.model_detail('GET')
                elif (self.path.split('?', 1)[0] == '/v1/chat/completions' or
                      self.path.startswith(('/v1/conversations/', '/v1/responses/',
                                            '/v1/chat/completions/'))):
                    self.application('GET')
                else:
                    self.response(404, dict(error='absent'))
            def model_detail(self, method):
                stack.model_requests.append(dict(method=method, path=self.path,
                    token=self.headers.get('X-Q4T-Gateway-Token'),
                    client=self.headers.get('X-Q4T-Client-Id'),
                    authorization=self.headers.get('Authorization')))
                if self.path not in ('/v1/models/cpu-fixture',
                                     '/v1/models/org%2Falias'):
                    self.response(404, dict(error=dict(code='model_not_found')))
                elif method == 'DELETE':
                    self.response(403, dict(error=dict(code='model_not_deletable')))
                else:
                    self.response(200, dict(id='cpu-fixture', object='model',
                                            created=42, owned_by='q4t'))
            def application(self, method):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                row = dict(method=method, path=self.path, body=raw.decode(),
                    token=self.headers.get('X-Q4T-Gateway-Token'),
                    client=self.headers.get('X-Q4T-Client-Id'),
                    authorization=self.headers.get('Authorization'))
                stack.application_requests.append(row)
                if row['token'] != stack.token:
                    self.response(403, dict(error=dict(code='invalid_gateway_credentials')))
                    return
                # Transport oracle only. Native endpoint/SDK semantics are
                # exercised separately by the Conversations/Responses SDK fixtures.
                if self.path in stack.held_application_paths:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(b'data: cpu-held-application\n\n')
                    self.wfile.flush()
                    stack.release_applications.wait(5)
                    return
                self.response(200, dict(cpu_fixture=True, method=method,
                                        path=self.path, client=row['client']))
            def do_DELETE(self):
                if self.path.startswith(('/v1/conversations/', '/v1/responses/',
                                         '/v1/chat/completions/')):
                    self.application('DELETE')
                else:
                    self.model_detail('DELETE')
            def do_POST(self):
                self.close_connection = True
                if self.path.split('?', 1)[0] in ('/v1/conversations', '/v1/responses') or self.path.startswith(('/v1/conversations/', '/v1/responses/', '/v1/chat/completions/')):
                    self.application('POST')
                    return
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                stack.forwarded.append(dict(token=self.headers.get('X-Q4T-Gateway-Token'),
                    client=self.headers.get('X-Q4T-Client-Id'),
                    authorization=self.headers.get('Authorization'), body=raw.decode(),
                    path=self.path))
                if self.headers.get('X-Q4T-Gateway-Token') != stack.token:
                    self.response(403, dict(error=dict(code='invalid_gateway_credentials')))
                    return
                body = json.loads(raw)
                generation = stack.generation
                stack.requests.append(dict(body=body, generation=generation))
                if body.get('drop'):
                    return  # Simulated backend transport failure after accepting a POST.
                if body.get('quota'):
                    self.response(429, dict(error=dict(code='client_rate_limited')))
                    return
                if body.get('malformed_response'):
                    self.connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n'
                        b'Content-Length: 3\r\nConnection: close\r\n\r\n{}')
                    return
                if body.get('stream'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(f'data: {{"generation":{generation}}}\n\n'.encode())
                    self.wfile.flush()
                    stack.stream_started.set()
                    end = time.monotonic()+5
                    while not stack.release_stream.is_set() and time.monotonic() < end:
                        if select.select([self.connection], [], [], 0.02)[0]:
                            if not self.connection.recv(1, socket.MSG_PEEK):
                                stack.stream_disconnected.set()
                                break
                    return  # Explicit truncated SSE, without fabricated [DONE].
                self.response(200, dict(generation=generation, cpu_fixture=True))
        for _ in range(2):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
            server.daemon_threads = True
            self.servers.append(server)
            self.thread(server.serve_forever)
        ports = (free_port(), *(s.server_port for s in self.servers))
        binary = Path(native_ingress) if native_ingress else NGINX
        self.value = config(self.directory, ports=ports, nginx=str(binary))
        self.value['nginx_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
        if native_ingress:
            self.value.update(version=2, native_ingress=self.value.pop('nginx'),
                native_ingress_sha256=self.value.pop('nginx_sha256'))
            if access is not None:self.value.update(version=3, access=access)
        self.value['binary_sha256'] = hashlib.sha256(Path('/proc/self/exe').read_bytes()).hexdigest()
        self.value['policy'].update(per_client_rps=100, burst=100)
        self.value['policy'].update(policy_overrides or {})
        self.value['tls'] = tls
        self.path = self.directory/'gateway.json'
        self.path.write_text(json.dumps(self.value))
        self.path.chmod(0o600)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(control.address(self.service_id))
        self.listener.listen(8)
        self.listener.settimeout(0.1)
        self.thread(self.peer)
        self.log = (self.directory/'worker.log').open('wb')
        self.start_gateway()

    def start_gateway(self):
        self.channel, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.channel.settimeout(0.1)
        self.thread(self.receive_events)
        self.process = subprocess.Popen([sys.executable, '-I', '-B',
            str(ROOT/'tools/serve_recovery'/('native_ingress.py' if self.native_ingress
                                           else 'gateway.py')), '--config', str(self.path)],
            env=dict(PATH='/no-systemd-or-dbus', LANG='C.UTF-8',
                Q4T_SERVICE_ID=self.service_id, Q4T_GATEWAY_GENERATION=str(self.gateway_generation),
                Q4T_CONTROL_FD=str(endpoint.fileno()), Q4T_GUARDIAN_PID=str(os.getpid()),
                Q4T_GUARDIAN_START=str(journal.process_start(os.getpid()))),
            pass_fds=(endpoint.fileno(),), stdout=self.log, stderr=self.log)
        endpoint.close()

    def restart_gateway(self):
        self.process.terminate();self.process.wait(timeout=5)
        self.channel.close()
        self.gateway_generation += 1;self.bound = 0
        self.start_gateway()

    def thread(self, function):
        worker = threading.Thread(target=function, daemon=True)
        self.threads.append(worker)
        worker.start()

    def peer(self):
        while not self.stopped.is_set():
            try:
                sock, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with sock:
                try:
                    sock.settimeout(1)
                    message = journal.receive(sock)
                    if message['op'] == 'status':
                        journal.send(sock, dict(version=1, service_id=self.service_id,
                            guardian_pid=os.getpid(), gateway_pid=self.process.pid,
                            gateway_start_ticks=journal.process_start(self.process.pid),
                            gateway_generation=self.gateway_generation))
                        continue
                    if message['op'] != 'binding' or not self.peer_available:
                        continue
                    value = snapshot(self.value, generation=self.generation, token=self.token)
                    value.update(version=1, service_id=self.service_id,
                        guardian_pid=os.getpid(), model_ready=self.ready,
                        business_ready=self.ready and self.bound == self.generation,
                        gateway_bound_model=self.bound)
                    if not self.ready:
                        value['binding'] = None
                    journal.send(sock, value)
                except (ValueError, OSError, TypeError):
                    continue

    def receive_events(self):
        channel = self.channel
        while not self.stopped.is_set():
            try:
                value = journal.receive(channel)
            except TimeoutError:
                continue
            except OSError:
                return
            if value is None:
                return
            self.events.append(value)
            if value['event'] == 'bound' and value['model_generation'] == self.generation:
                self.bound = self.generation
            elif value['event'] == 'unbound':
                self.bound = 0

    def wait(self, predicate, timeout=12):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            if self.process.poll() is not None:
                raise AssertionError('gateway worker exited: '+
                                     (self.directory/'worker.log').read_text())
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError('CPU gateway fixture deadline: '+
            (self.directory/'worker.log').read_text())

    def request(self, body=None, *, key='A'*40, path=None, extra=None):
        method = 'POST' if body is not None else 'GET'
        path = path or ('/v1/chat/completions' if body is not None else '/v1/models')
        host = self.value['policy']['listen_host']
        connection = (http.client.HTTPSConnection(host, self.value['policy']['listen_port'],
            timeout=4, context=self.ssl_context) if self.ssl_context else
            http.client.HTTPConnection(host, self.value['policy']['listen_port'], timeout=4))
        try:
            headers = {'Authorization': 'Bearer '+key, 'Content-Type': 'application/json'}
            headers.update(extra or {})
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def close(self):
        if getattr(self, '_closed', False):
            return
        self._closed = True
        self.release_stream.set()
        self.release_applications.set()
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)
        self.stopped.set()
        self.listener.close()
        self.channel.close()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for worker in self.threads:
            worker.join(timeout=2)
        self.log.close()
        (self.directory/'observations.json').write_text(json.dumps(dict(
            events=self.events, requests=self.requests,
            forwarded_count=len(self.forwarded), cpu_fixture=True), indent=2)+'\n')


class NginxTests(unittest.TestCase):
    def test_application_routes_preserve_methods_queries_and_identity(self):
        from tools.serve_gateway.gateway import APPLICATION_ROUTES
        self.ready()
        for method, template, _ in APPLICATION_ROUTES:
            path = template.replace('{response_id}', 'resp_1').replace(
                '{conversation_id}', 'conv_1').replace('{item_id}', 'item_1').replace(
                '{completion_id}', 'chat_1')
            if method == 'GET':
                path += '?limit=1&metadata%5Btopic%5D=%E8%B5%84%E6%96%99'
            body = '{"cpu_fixture":true}' if method == 'POST' else ''
            with self.subTest(method=method, path=path):
                connection = http.client.HTTPConnection('127.0.0.1',
                    self.stack.value['policy']['listen_port'], timeout=4)
                try:
                    connection.request(method, path, body, {
                        'Authorization': 'Bearer '+'A'*40,
                        'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200, response.read())
                    row = self.stack.application_requests[-1]
                    self.assertEqual((row['method'], row['path'], row['body']),
                                     (method, path, body))
                    self.assertEqual((row['client'], row['token']),
                                     ('alpha', 'e'*64))
                    self.assertIsNone(row['authorization'])
                finally:
                    connection.close()

    @classmethod
    def setUpClass(cls):
        cls.libc = ctypes.CDLL(None, use_errno=True)
        cls.old = ctypes.c_int()
        if cls.libc.prctl(37, ctypes.byref(cls.old), 0, 0, 0) or cls.libc.prctl(36, 1, 0, 0, 0):
            raise RuntimeError('CPU fixture subreaper unavailable')
        cls.output = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', ROOT/'build/recovery-nginx'))
        cls.output.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.libc.prctl(36, cls.old.value, 0, 0, 0)

    def setUp(self):
        self.stack = Stack(self.output)

    def tearDown(self):
        self.stack.close()
        end = time.monotonic()+3
        while time.monotonic() < end:
            owned = children(os.getpid())
            if not owned:
                return
            for pid in owned:
                try:
                    signal_identity(pid, journal.process_start(pid), signal.SIGKILL)
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, ProcessLookupError, FileNotFoundError):
                    pass
            time.sleep(0.01)
        self.fail('CPU nginx descendants not reaped')

    def ready(self):
        self.stack.wait(lambda: self.stack.bound == self.stack.generation)

    def test_recovery_keeps_auth_and_explicit_503_without_model_forward(self):
        self.ready()
        self.stack.ready = False
        status, headers, body = self.stack.request(dict(prompt='blocked'))
        self.assertEqual(status, 503)
        self.assertEqual(body['error']['code'], 'service_recovering')
        self.assertEqual(headers.get('Retry-After'), '1')
        self.assertEqual(self.stack.request(dict(prompt='bad key'), key='a'*40)[0], 401)
        self.assertEqual(self.stack.request(path='/metrics')[0], 404)
        self.assertEqual(self.stack.requests, [])

    def test_generation_rebind_rotates_token_and_strips_forged_client_headers(self):
        self.ready()
        status, _, body = self.stack.request(dict(prompt='first'), extra={
            'X-Q4T-Client-Id': 'beta', 'X-Q4T-Gateway-Token': 'f'*64})
        self.assertEqual((status, body['generation']), (200, 1))
        self.assertEqual(self.stack.forwarded[-1]['client'], 'alpha')
        self.assertIsNone(self.stack.forwarded[-1]['authorization'])
        self.stack.generation, self.stack.token, self.stack.bound = 2, 'f'*64, 0
        self.ready()
        status, _, body = self.stack.request(dict(prompt='second'))
        self.assertEqual((status, body['generation']), (200, 2))
        self.assertEqual([r['token'] for r in self.stack.forwarded], ['e'*64, 'f'*64])

    def test_accepted_post_transport_failure_is_not_replayed(self):
        self.ready()
        result = self.stack.request(dict(drop=True, tools=[dict(name='read_project')]))
        self.assertEqual(result[0], 503)
        self.assertEqual(len(self.stack.requests), 1)
        self.assertEqual(len(self.stack.forwarded), 1)

    def test_backend_quota_response_remains_429(self):
        self.ready()
        result = self.stack.request(dict(quota=True))
        self.assertEqual(result[0], 429)
        self.assertEqual(result[2]['error']['code'], 'client_rate_limited')

    def test_control_peer_outage_cannot_reuse_successful_binding(self):
        self.ready()
        self.stack.peer_available = False
        self.assertEqual(self.stack.request(dict(prompt='not forwarded'))[0], 503)
        self.assertEqual(self.stack.requests, [])
        self.stack.peer_available = True
        self.stack.wait(lambda: self.stack.bound == 1)
        self.assertEqual(self.stack.request(dict(prompt='resumed'))[0], 200)
        self.assertIsNone(self.stack.process.poll())

    def test_truncated_sse_is_not_continued_on_new_generation(self):
        self.ready()
        connection = http.client.HTTPConnection('127.0.0.1', self.stack.value['policy']['listen_port'], timeout=5)
        try:
            connection.request('POST', '/v1/chat/completions', json.dumps(dict(stream=True)),
                {'Authorization': 'Bearer '+'A'*40, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            first = response.readline()
            self.assertIn(b'"generation":1', first)
            self.stack.generation, self.stack.token, self.stack.bound = 2, 'f'*64, 0
            self.ready()
            self.stack.release_stream.set()
            remaining = response.read()
            self.assertNotIn(b'[DONE]', remaining)
            self.assertNotIn(b'"generation":2', remaining)
            self.assertEqual(self.stack.request(dict(prompt='new'))[2]['generation'], 2)
            self.assertEqual(len(self.stack.requests), 2)
        finally:
            connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--nginx', type=Path, required=True)
    args, rest = parser.parse_known_args()
    NGINX = args.nginx.resolve(strict=True)
    unittest.main(argv=[sys.argv[0], *rest])
