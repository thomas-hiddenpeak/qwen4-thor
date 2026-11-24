"""Real nginx and gateway worker, CPU HTTP backend and private Guardian peer.

No native model, GPU, NVML or shared GPU lock. Requires an explicit nginx ELF;
this local integration suite is separate from portable host contracts.
"""
import argparse
import ctypes
import hashlib
import http.client
import base64
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
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
        self.response_hook = None
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
                for name, value in getattr(stack, 'response_headers', ()):
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True
            def application_path(self, method):
                path = self.path.split('?', 1)[0]
                roots = ('/v1/chatkit', '/v1/webhook_endpoints', '/v1/webhook_event_types', '/v1/skills', '/v1/containers', '/v1/threads', '/v1/assistants', '/v1/vaults', '/v1/agents', '/v1/prompts', '/v1/conversations', '/v1/responses',
                         '/v1/files', '/v1/uploads', '/v1/batches',
                         '/v1/vector_stores', '/v1/tunnels', '/v1/audio/voice_consents',
                         '/v1/audio/voices', '/v1/realtime/traces', '/v1/realtime/client_secrets', '/v1/realtime/translations/client_secrets', '/v1/realtime/transcription_sessions', '/v1/realtime/sessions', '/v1/realtime/calls', '/v1/realtime/translations/calls', '/v1/live/sessions')
                return (any(path == root or path.startswith(root+'/')
                            for root in roots) or
                        (method == 'POST' and
                         path in ('/v1/audio/transcriptions', '/v1/audio/translations', '/v1/audio/speech', '/v1/decisions')) or
                        path.startswith('/v1/chat/completions/') or
                        (method != 'POST' and path == '/v1/chat/completions'))
            def do_GET(self):
                if self.path in ('/livez', '/readyz'):
                    self.response(200, dict(status='ready', live=True, ready=True))
                elif self.path == '/v1/config':
                    self.response(200, native_config(stack.value, stack.token))
                elif self.path == '/v1/models':
                    self.response(200, dict(object='list', data=[dict(id='cpu-fixture')]))
                elif self.path.startswith('/v1/models/'):
                    self.model_detail('GET')
                elif self.application_path('GET'):
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
                if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
                    # Native multipart forwarding streams bounded chunks. This
                    # CPU HTTP peer must consume them before returning a reply.
                    parts, size = [], 0
                    while True:
                        length = int(self.rfile.readline(128).strip(), 16)
                        if length == 0:
                            if self.rfile.read(2) != b'\r\n':
                                raise ValueError('invalid fixture chunk end')
                            break
                        size += length
                        if size > 16384:
                            raise ValueError('fixture body limit')
                        part = self.rfile.read(length)
                        if len(part) != length or self.rfile.read(2) != b'\r\n':
                            raise ValueError('invalid fixture chunk')
                        parts.append(part)
                    raw = b''.join(parts)
                else:
                    raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                row = dict(method=method, path=self.path, body=raw.decode(),
                    upstream_port=self.server.server_port,
                    token=self.headers.get('X-Q4T-Gateway-Token'),
                    client=self.headers.get('X-Q4T-Client-Id'),
                    authorization=self.headers.get('Authorization'))
                if self.headers.get('OpenAI-Beta') is not None:
                    row['openai_beta'] = self.headers['OpenAI-Beta']
                if self.path.startswith('/v1/tunnels/'):
                    row.update(tunnel_instance=self.headers.get('X-Tunnel-Client-Instance-Id'),
                               tunnel_shard_token=self.headers.get('X-Tunnel-Shard-Token'))
                stack.application_requests.append(row)
                if row['token'] != stack.token:
                    self.response(403, dict(error=dict(code='invalid_gateway_credentials')))
                    return
                if '/subagents/subagent_owned' in self.path and row['client'] != 'alpha':
                    self.response(404, dict(error=dict(code='object_not_found')))
                    return
                if method == 'POST' and self.path in ('/v1/realtime/calls', '/v1/realtime/translations/calls'):
                    if self.headers.get('Content-Type') != 'application/sdp':
                        self.response(415, dict(error='invalid content type'))
                        return
                    self.send_response(201)
                    self.send_header('Content-Type', 'application/sdp')
                    self.send_header('Location', '/v1/realtime/calls/rtc_fixture')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
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
                if self.application_path('DELETE'):
                    self.application('DELETE')
                else:
                    self.model_detail('DELETE')
            def do_POST(self):
                self.close_connection = True
                if self.application_path('POST'):
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
                if stack.response_hook and stack.response_hook(self, body, generation):
                    return
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
    def test_static_response_operations_are_not_response_ids(self):
        self.ready()
        for operation in ('compact', 'input_tokens'):
            for method in ('GET', 'DELETE'):
                with self.subTest(operation=operation, method=method):
                    connection = http.client.HTTPConnection('127.0.0.1',
                        self.stack.value['policy']['listen_port'], timeout=4)
                    try:
                        connection.request(method, '/v1/responses/'+operation+
                            '?fixture=reserved', headers={
                                'Authorization': 'Bearer '+'A'*40})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 404, response.read())
                        self.assertEqual(self.stack.application_requests, [])
                    finally:
                        connection.close()

    def test_application_routes_preserve_methods_queries_and_identity(self):
        from tools.serve_gateway.gateway import APPLICATION_ROUTES
        self.ready()
        for method, template, lane in APPLICATION_ROUTES:
            if lane in ('realtime','sideband'):
                # RFC 6455 switching and duplex frames have their own wire test.
                continue
            if lane == 'control':
                # Both real entrypoints enforce the control rate independently.
                time.sleep(0.11)
            identities = dict(webhook_endpoint_id='whe_1', automation_id='automation_1', invocation_id='invocation_1',
                              run_id='run_1', step_id='step_1', thread_id='thread_1', message_id='msg_1', assistant_id='asst_1', subagent_id='subagent_1', artifact_id='artifact_1', environment_id='environment_1', environment_template_id='environment_template_1', vault_id='vault_1', credential_id='cred_1', agent_id='agent_1', turn_id='turn_1', response_id='resp_1', conversation_id='conv_1',
                              item_id='item_1', completion_id='chat_1',
                              upload_id='upload_1', file_id='file_1', container_id='cntr_1', skill_id='skill_1',
                              prompt_id='pmpt_1', version='1', batch_id='batch_1',
                              vector_store_id='vs_1', tunnel_id='tunnel_local', consent_id='cons_1',
                              voice_id='voice_1', trace_id='trace_1', session_id='live_1', call_id='rtc_1')
            self.assertLessEqual(set(re.findall(r'\{([^}]+)\}', template)),
                                 identities.keys())
            if template.startswith('/v1/chatkit/'):
                identities.update(session_id='cksess_1', thread_id='cthr_1')
            if template.startswith('/v1/agents/sessions/'):
                identities['session_id'] = 'agent_session_1'
            if template.startswith('/v1/containers/'):
                identities['file_id'] = 'cfile_1'
            path = template.format(**identities)
            if method == 'GET' and template != '/v1/live/sessions/{session_id}/content':
                path += '?limit=1&metadata%5Btopic%5D=%E8%B5%84%E6%96%99'
            body = '{"cpu_fixture":true}' if method == 'POST' else ''
            content_type = 'application/json'
            if method == 'POST' and (template in (
                                      '/v1/files', '/v1/chatkit/attachments', '/v1/audio/transcriptions', '/v1/audio/translations') or
                                      template.endswith('/parts')):
                content_type = 'multipart/form-data; boundary=cpu-route'
                body = ('--cpu-route\r\nContent-Disposition: form-data; '
                        'name="file"; filename="fixture.txt"\r\n'
                        'Content-Type: text/plain\r\n\r\nactual CPU bytes'
                        '\r\n--cpu-route--\r\n')
            create_call = method == 'POST' and template in ('/v1/realtime/calls', '/v1/realtime/translations/calls')
            if create_call:
                content_type, body = 'application/sdp', 'v=0\r\ns=fixture\r\n'
            browser = path in ('/v1/chatkit', '/v1/chatkit/attachments')
            issuer = hashlib.sha256(b'A'*40).hexdigest()
            token = 'A'*40
            if browser:
                payload = 'cksec_q4t1.alpha.'+str(int(time.time())+120)+'.'+'a'*64
                signature = hmac.digest(b'e'*64, ('q4t.realtime.client_secret.v1/'+issuer+'/'+payload).encode(), 'sha256')
                token = payload+'.'+base64.urlsafe_b64encode(signature).decode().rstrip('=')
            with self.subTest(method=method, path=path):
                connection = http.client.HTTPConnection('127.0.0.1',
                    self.stack.value['policy']['listen_port'], timeout=4)
                try:
                    connection.request(method, path, body, {
                        'Authorization': 'Bearer '+token,
                        'Content-Type': content_type})
                    response = connection.getresponse()
                    wire = response.read()
                    self.assertEqual(response.status, 201 if create_call else 200, wire)
                    if create_call:
                        self.assertEqual(response.getheader('Content-Type'), 'application/sdp')
                        self.assertEqual(response.getheader('Location'), '/v1/realtime/calls/rtc_fixture')
                        self.assertEqual(wire, body.encode())
                    row = self.stack.application_requests[-1]
                    self.assertEqual((row['method'], row['path'], row['body']),
                                     (method, path, body))
                    self.assertEqual((row['client'], row['token']),
                                     ('alpha', 'e'*64))
                    self.assertEqual(row['upstream_port'],
                        self.stack.value['policy']['upstream_operations_port'
                            if lane == 'control' else 'upstream_port'])
                    if browser:
                        expected = 'Bearer '+token if self.stack.native_ingress else 'Q4T-ChatKit '+issuer+' Bearer '+token
                        self.assertEqual(row['authorization'], expected)
                    elif path in ('/v1/chatkit/sessions', '/v1/realtime/client_secrets', '/v1/realtime/translations/client_secrets', '/v1/realtime/transcription_sessions', '/v1/realtime/sessions'):
                        self.assertEqual(row['authorization'], 'Q4T-Issuer ' +
                                         hashlib.sha256(b'A'*40).hexdigest())
                    else:
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

    def test_skill_versions_and_content_preserve_owner_and_query(self):
        self.ready()
        root = '/v1/skills/skill_1'
        for body, path in [(None, '/v1/skills?limit=0'),
                           ({'default_version':'2'}, root),
                           (None, root+'/versions?order=asc'),
                           (None, root+'/content'), (None, root+'/versions/2/content')]:
            self.assertEqual(self.stack.request(body, path=path)[0], 200)
            row = self.stack.application_requests[-1]
            self.assertEqual((row['client'],row['path']),('alpha',path))
        for path in (root+'/versions/1/unknown',root+'/versions/1/content/extra','/v1/skills/cntr_1'):
            self.assertEqual(self.stack.request(path=path)[0],404)
        self.assertEqual(len(self.stack.application_requests),5)

    def test_container_file_paths_keep_owner_and_distinguish_file_ids(self):
        self.ready()
        root = '/v1/containers/cntr_1/files'
        for body, path in [({'name':'q4t'}, '/v1/containers'),
                           ({'file_id':'file_1'}, root),
                           (None, root+'?limit=1'), (None, root+'/cfile_1/content')]:
            self.assertEqual(self.stack.request(body, path=path)[0], 200)
            row = self.stack.application_requests[-1]
            self.assertEqual((row['client'],row['path']), ('alpha',path))
        for path in (root+'/file_1/content','/v1/containers/file_1/files'):
            self.assertEqual(self.stack.request(path=path)[0], 404)
        self.assertEqual(len(self.stack.application_requests), 4)

    def test_assistant_registry_preserves_beta_and_owned_metadata_lane(self):
        self.ready()
        for path, body in [('/v1/threads', {}), ('/v1/threads/thread_1/messages?limit=2', None),
                           ('/v1/assistants', {'model':'cpu'}),
                           ('/v1/assistants/asst_1', {'name':'updated'}),
                           ('/v1/assistants?limit=1&before=asst_1', None)]:
            status, _, _ = self.stack.request(body, path=path,
                extra={'OpenAI-Beta':'agents=v1, assistants=v2'})
            self.assertEqual(status, 200)
            row = self.stack.application_requests[-1]
            self.assertEqual((row['path'],row['client'],row['openai_beta']),
                             (path,'alpha','assistants=v2'))
        self.assertEqual(self.stack.request(path='/v1/assistants/agent_other')[0], 404)
        self.assertEqual(self.stack.request(path='/v1/threads/thread_1/messages/asst_1')[0], 404)
        self.assertEqual(len(self.stack.application_requests), 5)

    def test_assistant_run_routes_preserve_method_owner_and_beta(self):
        self.ready()
        base = '/v1/threads/thread_1/runs'
        run = base+'/run_1'
        for path, body in [('/v1/threads/runs', {}), (base, {}), (base, None),
                           (run, None), (run, {'metadata': {}}), (run+'/cancel', {}),
                           (run+'/submit_tool_outputs', {'tool_outputs': []}),
                           (run+'/steps', None), (run+'/steps/step_1', None)]:
            with self.subTest(path=path, method='GET' if body is None else 'POST'):
                status, _, _ = self.stack.request(body, path=path,
                    extra={'OpenAI-Beta': 'assistants=v2'})
                self.assertEqual(status, 200)
                row = self.stack.application_requests[-1]
                self.assertEqual((row['method'], row['path'], row['client'], row['openai_beta']),
                                 ('GET' if body is None else 'POST', path, 'alpha', 'assistants=v2'))

    def test_webhook_management_preserves_owner_and_query(self):
        self.ready()
        base = '/v1/webhook_endpoints'
        resource = base + '/whe_1'
        for body, path in [(None, '/v1/webhook_event_types'), ({}, base),
                           (None, base + '?limit=2'), (None, resource),
                           ({}, resource), ({}, resource + '/rotate_secret'),
                           ({'event_type': 'response.completed'}, resource + '/test')]:
            with self.subTest(path=path):
                self.assertEqual(self.stack.request(body, path=path)[0], 200)
                row = self.stack.application_requests[-1]
                self.assertEqual((row['client'], row['path'], row['method']),
                                 ('alpha', path, 'GET' if body is None else 'POST'))

    def test_chatkit_routes_issue_and_forward_scoped_browser_credential(self):
        self.ready()
        issuer = hashlib.sha256(b'A'*40).hexdigest()
        selector = {'OpenAI-Beta': 'agents=v1, chatkit_beta=v1'}
        self.assertEqual(self.stack.request({}, path='/v1/chatkit/sessions', extra=selector)[0], 200)
        issued = self.stack.application_requests[-1]
        self.assertEqual((issued['client'], issued['openai_beta'], issued['authorization']),
                         ('alpha', 'chatkit_beta=v1', 'Q4T-Issuer '+issuer))
        payload = 'cksec_q4t1.alpha.' + str(int(time.time())+120) + '.' + 'a'*64
        signature = hmac.digest(b'e'*64, ('q4t.realtime.client_secret.v1/'+issuer+'/'+payload).encode(), 'sha256')
        token = payload + '.' + base64.urlsafe_b64encode(signature).decode().rstrip('=')
        self.assertEqual(self.stack.request({}, key=token, path='/v1/chatkit')[0], 200)
        received = self.stack.application_requests[-1]
        forwarded = 'Bearer '+token
        if not self.stack.native_ingress:
            forwarded = 'Q4T-ChatKit '+issuer+' '+forwarded
        self.assertEqual((received['client'], received['authorization']), ('alpha', forwarded))
        self.assertEqual(self.stack.request({}, path='/v1/chatkit')[0], 401)
        self.assertEqual(self.stack.request(key=token, path='/v1/chatkit/threads')[0], 401)

    def test_automation_paths_preserve_owner_selector_and_nested_id(self):
        self.ready()
        base = '/v1/agents/automations'
        row = base + '/automation_1'
        history = row + '/invocations'
        beta = {'OpenAI-Beta': 'agents=v1'}
        for body, path in [({}, base), (None, base + '?limit=2'), ({}, row + '/pause'),
                           ({}, row + '/resume'), ({}, history),
                           (None, history + '/invocation_1')]:
            with self.subTest(path=path):
                self.assertEqual(self.stack.request(body, path=path, extra=beta)[0], 200)
                seen = self.stack.application_requests[-1]
                self.assertEqual((seen['path'], seen['client'], seen['openai_beta']),
                                 (path, 'alpha', 'agents=v1'))
        for path in [base + '/agent_wrong', history + '/automation_wrong']:
            self.assertEqual(self.stack.request(path=path, extra=beta)[0], 404)
        self.assertEqual(len(self.stack.application_requests), 6)

    def test_environment_templates_preserve_identity_and_sdk_selector(self):
        self.ready()
        root = '/v1/agents/environments/templates'
        selector = {'OpenAI-Beta': 'agents=v1'}
        for body, path in [({}, root), (None, root+'?order=asc&limit=1'),
                           ({'name': 'updated'}, root+'/environment_template_1')]:
            self.assertEqual(self.stack.request(body, path=path, extra=selector)[0], 200)
            row = self.stack.application_requests[-1]
            self.assertEqual((row['path'], row['client'], row.get('openai_beta')),
                             (path, 'alpha', 'agents=v1'))
        self.assertEqual(self.stack.request(path=root+'/other_1', extra=selector)[0], 404)
        self.assertEqual(len(self.stack.application_requests), 3)

    def test_subagent_history_preserves_owner_pagination_and_beta_selector(self):
        self.ready()
        root = '/v1/agents/sessions/agent_session_1/subagents'
        member = root+'/subagent_owned'
        selector = {'OpenAI-Beta':'agents=v1'}
        for path in (root, member, member+'/items', member+'/turns?after=turn_1&limit=2&order=asc',
                     member+'/turns/turn_2', member+'/turns/turn_2/items?order=desc'):
            with self.subTest(path=path):
                self.assertEqual(self.stack.request(path=path,extra=selector)[0], 200)
                row = self.stack.application_requests[-1]
                self.assertEqual((row['method'],row['path'],row['client'],row.get('openai_beta'),
                                  row['upstream_port']), ('GET',path,'alpha','agents=v1',
                                  self.stack.value['policy']['upstream_operations_port']))
        self.assertEqual(self.stack.request(path=member, key='B'*40, extra=selector)[0], 404)
        self.assertEqual(self.stack.application_requests[-1]['client'], 'beta')
        before = len(self.stack.application_requests)
        status, _, _ = self.stack.request(path=member, key='B'*40,
            extra=dict(selector, **{'X-Q4T-Client-Id':'alpha'}))
        if self.stack.native_ingress:
            self.assertEqual(status, 400)
            self.assertEqual(len(self.stack.application_requests), before)
        else:
            self.assertEqual(status, 404)
            self.assertEqual(len(self.stack.application_requests), before + 1)
            self.assertEqual(self.stack.application_requests[-1]['client'], 'beta')
        before = len(self.stack.application_requests)
        for path in (root+'/untyped', member+'/turns/not_a_turn', member+'/extra'):
            self.assertEqual(self.stack.request(path=path,extra=selector)[0],404)
        self.assertEqual(self.stack.request({},path=member,extra=selector)[0],404)
        self.assertEqual(self.stack.request(path=member,key='invalid')[0],401)
        self.assertEqual(len(self.stack.application_requests), before)

    def test_agent_session_traces_preserve_cursor_selector_and_owner(self):
        self.ready()
        path = '/v1/agents/sessions/agent_session_1/traces?after=turn_1&limit=2&order=asc'
        selector = {'OpenAI-Beta': 'agents=v1'}
        self.assertEqual(self.stack.request(path=path, extra=selector)[0], 200)
        self.assertEqual(len(self.stack.application_requests), 1)
        row = self.stack.application_requests[0]
        self.assertEqual((row['method'], row['path'], row['client'], row.get('openai_beta')),
                         ('GET', path, 'alpha', 'agents=v1'))
        self.assertEqual(self.stack.request(path=path, key='invalid')[0], 401)
        self.assertEqual(self.stack.request({}, path=path, extra=selector)[0], 404)
        self.assertEqual(len(self.stack.application_requests), 1)

    def test_vault_beta_selector_reaches_the_owned_backend(self):
        self.ready()
        self.stack.request(dict(name='Project'), path='/v1/vaults',
                           extra={'OpenAI-Beta': 'other, agents=v1'})
        row = self.stack.application_requests[-1]
        self.assertEqual((row['path'], row['client'], row.get('openai_beta')),
                         ('/v1/vaults', 'alpha', 'agents=v1'))
        self.stack.request({}, path='/v1/vaults', extra={'OpenAI-Beta': 'agents=v10'})
        self.assertNotIn('openai_beta', self.stack.application_requests[-1])

    def test_responses_beta_selection_reaches_authenticated_model(self):
        self.ready()
        path = '/v1/responses/input_tokens?beta=true'
        self.stack.request(dict(input='Count this'), path=path,
                           extra={'OpenAI-Beta': 'other, responses_multi_agent=v1'})
        row = self.stack.application_requests[-1]
        self.assertEqual(row['path'], path)
        self.assertEqual(row['client'], 'alpha')
        self.assertEqual(row['openai_beta'], 'responses_multi_agent=v1')
        self.stack.request(dict(input='Count this'), path=path,
                           extra={'OpenAI-Beta': 'responses_multi_agent=v10'})
        self.assertNotIn('openai_beta', self.stack.application_requests[-1])

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
