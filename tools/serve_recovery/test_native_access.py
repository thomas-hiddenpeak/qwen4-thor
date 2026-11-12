"""Real C++ TLS/CORS/address/rotation contracts, CPU backend, no GPU."""
import argparse
import copy
import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_gateway import gateway as policy
from tools.serve_recovery import journal, tls_control
from tools.serve_recovery import test_gateway_nginx as fixture

INGRESS = None
NO_TLS = None
HOST_TESTS = None
OPENSSL = shutil.which('openssl')
CORS = dict(allowed_origins=['https://client.example'], allowed_methods=['GET', 'POST'],
    allowed_headers=['authorization', 'content-type', 'x-request-id'],
    exposed_headers=['x-request-id', 'retry-after'], allow_credentials=True, max_age=120)


def certificates(directory):
    """Only explicitly synthetic local certificates. No installed trust changes."""
    directory.mkdir(mode=0o700)
    def run(*args):
        with (directory/'openssl.log').open('ab') as log:
            subprocess.run([OPENSSL, *args], cwd=directory, check=True,
                           stdout=log, stderr=log, timeout=15)
    run('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '30',
        '-subj', '/CN=q4t CPU fixture CA', '-keyout', 'ca.key', '-out', 'ca.pem')
    run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=localhost',
        '-keyout', 'server.key', '-out', 'server.csr')
    (directory/'server.ext').write_text('basicConstraints=critical,CA:FALSE\n'
        'keyUsage=critical,digitalSignature,keyEncipherment\n'
        'extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1\n')
    for number, name, days in ((1, 'server.pem', '30'), (2, 'next.pem', '30'),
                              (3, 'expired.pem', '0')):
        run('x509', '-req', '-in', 'server.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key',
            '-set_serial', str(number), '-days', days, '-extfile', 'server.ext', '-out', name)
    run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=fixture client',
        '-keyout', 'client.key', '-out', 'client.csr')
    (directory/'client.ext').write_text('basicConstraints=critical,CA:FALSE\n'
        'keyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n')
    run('x509', '-req', '-in', 'client.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key',
        '-set_serial', '4', '-days', '30', '-extfile', 'client.ext', '-out', 'client.pem')
    run('x509', '-req', '-in', 'client.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key',
        '-set_serial', '5', '-days', '30', '-extfile', 'client.ext', '-out', 'other-client.pem')
    (directory/'issued').mkdir()
    (directory/'index.txt').write_text('')
    (directory/'serial').write_text('1000\n')
    (directory/'crlnumber').write_text('1000\n')
    (directory/'ca.cnf').write_text('[ca]\ndefault_ca=q4t\n[q4t]\n'
        'database=index.txt\nnew_certs_dir=issued\ncertificate=ca.pem\n'
        'private_key=ca.key\nserial=serial\ncrlnumber=crlnumber\n'
        'default_md=sha256\ndefault_days=30\ndefault_crl_days=30\npolicy=policy\n'
        '[policy]\ncommonName=supplied\n')
    run('ca', '-batch', '-config', 'ca.cnf', '-gencrl', '-out', 'clean.crl')
    run('ca', '-batch', '-config', 'ca.cnf', '-revoke', 'client.pem')
    run('ca', '-batch', '-config', 'ca.cnf', '-gencrl', '-out', 'revoked.crl')
    for path in directory.glob('*.key'):
        path.chmod(0o600)
    for pattern in ('*.pem', '*.crl'):
        for path in directory.glob(pattern):
            path.chmod(0o644)


class AccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.NginxTests.setUpClass.__func__(cls)
        if HOST_TESTS is not None:
            required = cls.output/'native-access-wire-required.txt'
            required.write_text('embeddings_fp32_wire_roundtrip_and_base64_byte_order\n'
                'moderations_public_response_requires_complete_real_reports\n')
            with (cls.output/'native-access-wire.log').open('wb') as log:
                subprocess.run([str(HOST_TESTS), '--required-list', str(required)],
                    env=dict(os.environ, Q4T_TEST_OUTPUT_DIR=str(cls.output)),
                    stdout=log, stderr=subprocess.STDOUT, check=True, timeout=30)
        cls.certificates = Path(tempfile.mkdtemp(prefix='keys-', dir=cls.output))/'pem'
        certificates(cls.certificates)

    @classmethod
    def tearDownClass(cls):
        fixture.NginxTests.tearDownClass.__func__(cls)

    ready = fixture.NginxTests.ready
    tearDown = fixture.NginxTests.tearDown

    def setUp(self):
        self.assets = Path(tempfile.mkdtemp(prefix='credentials-', dir=self.output))
        for name in ('server.pem', 'server.key', 'ca.pem'):
            shutil.copyfile(self.certificates/name, self.assets/name)
            (self.assets/name).chmod(0o600 if name.endswith('.key') else 0o644)
        (self.assets/'server.key').chmod(0o600)
        self.tls = dict(certificate=str(self.assets/'server.pem'),
            key=str(self.assets/'server.key'), ca='', require_client_certificate=False)
        self.context = ssl.create_default_context(cafile=str(self.certificates/'ca.pem'))
        self.replace()

    def replace(self, *, tls=True, context=None, binary=None, **settings):
        if hasattr(self, 'stack'):
            self.stack.close()
        self.stack = fixture.Stack(self.output, native_ingress=binary or INGRESS,
            policy_overrides={**dict(version=3, cors=copy.deepcopy(CORS)), **settings},
            tls=self.tls if tls else None, ssl_context=(context or self.context) if tls else None)
        self.addCleanup(self.stack.close)
        self.ready()

    def admin(self, op='status', **override):
        return tls_control.request(self.stack.service_id, 1, op,
            **dict(dict(binary_sha256=self.stack.value['native_ingress_sha256'],
                parent_pid=self.stack.process.pid,
                parent_start=journal.process_start(self.stack.process.pid)), **override))

    def reload(self):
        first = self.admin('reload')
        self.assertTrue(first['reload_accepted'])
        end = time.monotonic()+3
        result = first
        while result['reload_pending'] and time.monotonic() < end:
            time.sleep(0.02)
            result = self.admin()
        self.assertFalse(result['reload_pending'])
        self.assertEqual(result['load_attempt'], first['load_attempt'])
        self.assertEqual(result['entrypoint_pid'], first['entrypoint_pid'])
        return result

    def connect(self, *, context=None, sni=None):
        value = self.stack.value['policy']
        raw = socket.create_connection((value['listen_host'], value['listen_port']), timeout=4)
        if self.stack.value['tls']:
            try:
                return (context or self.context).wrap_socket(raw,
                    server_hostname=sni or value['server_name'])
            except BaseException:
                raw.close()
                raise
        return raw

    def request(self, method='GET', path='/v1/models', *, headers=None, body=b'', auth=True):
        fields = dict(Host=self.stack.value['policy']['server_name'],
                      **{'Content-Length': str(len(body))})
        if ':' in fields['Host']:
            fields['Host'] = '['+fields['Host']+']'
        if auth:
            fields['Authorization'] = 'Bearer '+'A'*40
        fields.update(headers or {})
        with self.connect() as sock:
            sock.sendall((f'{method} {path} HTTP/1.1\r\n'+
                ''.join(f'{k}: {v}\r\n' for k, v in fields.items())+'\r\n').encode()+body)
            response = http.client.HTTPResponse(sock)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def stream(self):
        sock = self.connect()
        body = b'{"stream":true}'
        sock.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n'
            b'Content-Type: application/json\r\nAuthorization: Bearer '+b'A'*40+
            b'\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        response = http.client.HTTPResponse(sock)
        response.begin()
        self.assertEqual(response.status, 200)
        self.assertIn(b'"generation":1', response.readline())
        return sock, response

    def test_tls_12_and_13_preserve_json_authentication_and_private_binding(self):
        for version in (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
            self.context.minimum_version = self.context.maximum_version = version
            self.assertEqual(self.stack.request(dict(prompt='CPU TLS fixture'))[0], 200)
            with self.connect() as sock:
                self.assertEqual(sock.version(), 'TLSv1.2' if version == ssl.TLSVersion.TLSv1_2
                                               else 'TLSv1.3')
        self.assertEqual(self.stack.request(dict(prompt='denied'), key='wrong')[0], 401)
        self.assertEqual(len(self.stack.forwarded), 2)
        self.assertTrue(all(item['client'] == 'alpha' and item['authorization'] is None
                            for item in self.stack.forwarded))

    def test_ipv6_actual_peer_cidr_plaintext_and_tls(self):
        for tls in (False, True):
            self.replace(tls=tls, listen_host='::1', server_name='::1',
                         allowed_client_cidrs=['::1/128'])
            self.assertEqual(self.stack.request(dict(prompt='IPv6'))[0], 200)
        self.replace(listen_host='::1', server_name='::1',
                     allowed_client_cidrs=['fd00::/8'])
        self.assertEqual(self.request()[0], 403)
        self.assertEqual(self.stack.requests, [])

    def test_cors_preflight_needs_no_api_key_or_business_admission(self):
        self.stack.ready = False
        fields = {'Origin': 'https://client.example',
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'Authorization, Content-Type, X-Request-Id'}
        for _ in range(3):
            status, headers, body = self.request('OPTIONS', '/v1/chat/completions',
                                                headers=fields, auth=False)
            self.assertEqual((status, body), (204, b''))
            self.assertEqual(headers['Access-Control-Allow-Origin'], fields['Origin'])
            self.assertEqual(headers['Access-Control-Allow-Credentials'], 'true')
            self.assertEqual(headers['Access-Control-Allow-Headers'],
                             'authorization, content-type, x-request-id')
        self.assertEqual(self.stack.requests, [])
        self.assertEqual(self.request(headers={'Origin': fields['Origin']})[0], 503)

    def test_completions_and_model_details_use_tls_cors_and_api_keys(self):
        self.replace(cors=dict(CORS, allowed_methods=['GET', 'POST', 'DELETE']))
        origin = 'https://client.example'
        for method, path in [('POST', '/v1/completions'),
                             ('GET', '/v1/models/org%2Falias'),
                             ('DELETE', '/v1/models/cpu-fixture')]:
            status, headers, body = self.request('OPTIONS', path, auth=False,
                headers={'Origin': origin, 'Access-Control-Request-Method': method,
                         'Access-Control-Request-Headers': 'Authorization, Content-Type'})
            self.assertEqual((status, body), (204, b''))
            self.assertEqual(headers['Access-Control-Allow-Origin'], origin)
            self.assertEqual(self.request(method, path, auth=False,
                headers={'Origin': origin})[0], 401)
        raw = json.dumps(dict(model='cpu-fixture', prompt=[4, 5])).encode()
        status, headers, body = self.request('POST', '/v1/completions', body=raw,
            headers={'Origin': origin, 'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertEqual(headers['Access-Control-Allow-Origin'], origin)
        self.assertEqual(json.loads(self.stack.forwarded[-1]['body']), json.loads(raw))
        self.assertEqual(self.request('GET', '/v1/models/org%2Falias',
            headers={'Origin': origin})[0], 200)
        status, headers, body = self.request('DELETE', '/v1/models/cpu-fixture',
            headers={'Origin': origin})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)['error']['code'], 'model_not_deletable')
        self.assertEqual(headers['Access-Control-Allow-Origin'], origin)

    def test_moderations_sdk_tls_auth_cors_and_exact_body(self):
        # Actual C++ protocol fixture, not a claim of model classification.
        expected = json.loads((Path(os.environ['Q4T_TEST_OUTPUT_DIR']) /
                               'moderations-response.json').read_text())
        request = dict(input=['benign '+('x' * 8192), 'second input'])
        self.stack.response_hook = lambda peer, *_: (
            peer.response(200, expected) or True)
        origin = 'https://client.example'
        status, headers, raw = self.request('OPTIONS', '/v1/moderations',
            auth=False, headers={'Origin': origin,
                'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': 'Authorization, Content-Type'})
        self.assertEqual((status, raw), (204, b''))
        self.assertEqual(headers['Access-Control-Allow-Origin'], origin)
        self.assertEqual(self.request('POST', '/v1/moderations', auth=False)[0], 401)
        self.assertEqual(self.stack.forwarded, [])
        command = dict(action='moderations_create', request=request,
            base_url='https://127.0.0.1:' +
                str(self.stack.value['policy']['listen_port']) + '/v1',
            api_key='A' * 40, headers={'Origin': origin}, timeout_ms=5000,
            sdk_directory=os.environ.get('Q4T_NODE_SDK_DIR',
                str(ROOT/'.q4t-work/serve-protocol-20261103/sdk')))
        env = {k:v for k,v in os.environ.items()
               if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['NODE_EXTRA_CA_CERTS'] = str(self.certificates/'ca.pem')
        run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_sdk.mjs')],
            input=json.dumps(command), text=True, capture_output=True,
            env=env, timeout=10)
        (self.stack.directory/'moderations-sdk.stdout').write_text(run.stdout)
        (self.stack.directory/'moderations-sdk.stderr').write_text(run.stderr)
        self.assertEqual((run.returncode, run.stderr), (0, ''))
        result = json.loads(run.stdout)
        self.assertTrue(result['ok'])
        self.assertEqual(result['result'], expected)
        self.assertEqual(len(result['exchanges']), 1)
        self.assertEqual(len(self.stack.forwarded), 1)
        row = self.stack.forwarded[0]
        self.assertEqual(json.loads(row['body']), request)
        self.assertEqual((row['path'], row['client'], row['token']),
                         ('/v1/moderations', 'alpha', 'e' * 64))
        self.assertIsNone(row['authorization'])
        # Finite backend errors also survive native TLS without a POST replay.
        self.stack.response_hook = lambda peer, *_: (
            peer.response(429, dict(error=dict(type='rate_limit_error',
                code='client_tpm_exhausted', message='CPU quota fixture'))) or True)
        command['request'] = dict(input='retry must remain off')
        run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_sdk.mjs')],
            input=json.dumps(command), text=True, capture_output=True,
            env=env, timeout=10)
        (self.stack.directory/'moderations-error-sdk.stdout').write_text(run.stdout)
        (self.stack.directory/'moderations-error-sdk.stderr').write_text(run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual((run.returncode, run.stderr), (1, ''))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['status'], 429)
        self.assertEqual(result['error']['code'], 'client_tpm_exhausted')
        self.assertEqual(len(result['exchanges']), 1)
        self.assertEqual(len(self.stack.forwarded), 2)

    def test_embeddings_sdk_default_base64_float_tls_and_exact_body(self):
        # Native ingress and official SDK; actual C++ wire fixtures, no model.
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        floats = json.loads((root/'embeddings-float.json').read_text())
        encoded = json.loads((root/'embeddings-base64.json').read_text())
        origin = 'https://client.example'
        status, headers, raw = self.request('OPTIONS', '/v1/embeddings', auth=False,
            headers={'Origin':origin, 'Access-Control-Request-Method':'POST',
                'Access-Control-Request-Headers':'Authorization, Content-Type'})
        self.assertEqual((status, raw), (204, b''))
        self.assertEqual(headers['Access-Control-Allow-Origin'], origin)
        self.assertEqual(self.request('POST', '/v1/embeddings', auth=False)[0], 401)
        self.assertEqual(self.stack.forwarded, [])
        env = {k:v for k,v in os.environ.items()
               if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['NODE_EXTRA_CA_CERTS'] = str(self.certificates/'ca.pem')
        command = dict(action='embeddings_create',
            base_url='https://127.0.0.1:'+str(self.stack.value['policy']['listen_port'])+'/v1',
            api_key='A'*40, headers={'Origin':origin}, timeout_ms=5000,
            sdk_directory=os.environ.get('Q4T_NODE_SDK_DIR',
                str(ROOT/'.q4t-work/serve-protocol-20261103/sdk')))
        for index, encoding in enumerate([None, 'float', 'base64']):
            request = dict(model='actual-qwen', input=['x'*8192, 'second'])
            if encoding: request['encoding_format'] = encoding
            command['request'] = request
            expected = floats if encoding == 'float' else encoded
            self.stack.response_hook = lambda peer, *_: (peer.response(200, expected) or True)
            run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_sdk.mjs')],
                input=json.dumps(command), text=True, capture_output=True, env=env, timeout=10)
            (self.stack.directory/f'embeddings-sdk-{index}.stdout').write_text(run.stdout)
            (self.stack.directory/f'embeddings-sdk-{index}.stderr').write_text(run.stderr)
            self.assertEqual((run.returncode, run.stderr), (0, ''))
            result = json.loads(run.stdout)
            self.assertTrue(result['ok'])
            self.assertEqual(len(result['exchanges']), 1)
            if encoding == 'base64': self.assertEqual(result['result'], encoded)
            else:
                self.assertEqual(result['embedding_float32_base64'],
                                 [row['embedding'] for row in encoded['data']])
                for actual, reference in zip(result['result']['data'], floats['data']):
                    self.assertEqual({k:v for k,v in actual.items() if k != 'embedding'},
                                     {k:v for k,v in reference.items() if k != 'embedding'})
                    self.assertEqual(len(actual['embedding']), len(reference['embedding']))
            forwarded = self.stack.forwarded[-1]
            self.assertEqual(json.loads(forwarded['body']),
                dict(request, encoding_format=encoding or 'base64'))
            self.assertEqual((forwarded['path'], forwarded['client'], forwarded['token']),
                             ('/v1/embeddings', 'alpha', 'e'*64))
            self.assertIsNone(forwarded['authorization'])
        self.assertEqual(len(self.stack.forwarded), 3)

    def test_conversations_use_native_tls_cors_and_identity(self):
        self.replace(cors=dict(CORS, allowed_methods=['GET', 'POST', 'DELETE']))
        origin = 'https://client.example'
        for method, path in [
            ('POST', '/v1/conversations'),
            ('POST', '/v1/conversations/conv_1/items'),
            ('GET', '/v1/conversations/conv_1/items?limit=1&include%5B%5D='
                    'reasoning.encrypted_content'),
            ('DELETE', '/v1/conversations/conv_1/items/item_1')]:
            with self.subTest(method=method, path=path):
                status, headers, body = self.request('OPTIONS', path, auth=False,
                    headers={'Origin': origin, 'Access-Control-Request-Method': method,
                             'Access-Control-Request-Headers': 'Authorization, Content-Type'})
                self.assertEqual((status, body), (204, b''))
                self.assertEqual(headers['Access-Control-Allow-Origin'], origin)
                self.assertEqual(self.request(method, path, auth=False,
                    headers={'Origin': origin})[0], 401)
                raw = b'{"items":[]}' if method == 'POST' else b''
                status, headers, body = self.request(method, path, body=raw,
                    headers={'Origin': origin, 'Content-Type': 'application/json'})
                self.assertEqual(status, 200)
                self.assertEqual(headers['Access-Control-Allow-Origin'], origin)
                row = self.stack.application_requests[-1]
                self.assertEqual((row['method'], row['path'], row['body']),
                                 (method, path, raw.decode()))
                self.assertEqual((row['client'], row['token']), ('alpha', 'e'*64))
                self.assertIsNone(row['authorization'])

    def test_responses_use_native_tls_cors_and_identity(self):
        self.replace(cors=dict(CORS, allowed_methods=['GET', 'POST', 'DELETE']))
        origin = 'https://client.example'
        for method, path in [
            ('POST', '/v1/responses'),
            ('GET', '/v1/responses/resp_1?stream=true&starting_after=2'),
            ('GET', '/v1/responses/resp_1/input_items?limit=1&include%5B%5D='
                    'message.output_text.logprobs'),
            ('POST', '/v1/responses/resp_1/cancel'),
            ('DELETE', '/v1/responses/resp_1')]:
            with self.subTest(method=method, path=path):
                status, headers, body = self.request('OPTIONS', path, auth=False,
                    headers={'Origin': origin, 'Access-Control-Request-Method': method,
                             'Access-Control-Request-Headers': 'Authorization, Content-Type'})
                self.assertEqual((status, body), (204, b''))
                self.assertEqual(headers['Access-Control-Allow-Origin'], origin)
                self.assertEqual(self.request(method, path, auth=False,
                    headers={'Origin': origin})[0], 401)
                raw = b'{"input":"query"}' if path == '/v1/responses' else b''
                status, headers, body = self.request(method, path, body=raw,
                    headers={'Origin': origin, 'Content-Type': 'application/json'})
                self.assertEqual(status, 200)
                self.assertEqual(headers['Access-Control-Allow-Origin'], origin)
                row = self.stack.application_requests[-1]
                self.assertEqual((row['method'], row['path'], row['body']),
                                 (method, path, raw.decode()))
                self.assertEqual((row['client'], row['token']), ('alpha', 'e'*64))
                self.assertIsNone(row['authorization'])

    def test_stored_chat_uses_native_tls_cors_and_identity(self):
        self.replace(cors=dict(CORS, allowed_methods=['GET', 'POST', 'DELETE']))
        origin = 'https://client.example'
        for method, path in [
            ('GET', '/v1/chat/completions?metadata%5Btopic%5D=records'),
            ('GET', '/v1/chat/completions/chat_1'),
            ('POST', '/v1/chat/completions/chat_1'),
            ('DELETE', '/v1/chat/completions/chat_1'),
            ('GET', '/v1/chat/completions/chat_1/messages?limit=1')]:
            with self.subTest(method=method, path=path):
                status, headers, body = self.request('OPTIONS', path, auth=False,
                    headers={'Origin': origin, 'Access-Control-Request-Method': method,
                             'Access-Control-Request-Headers': 'Authorization, Content-Type'})
                self.assertEqual((status, body), (204, b''))
                self.assertEqual(headers['Access-Control-Allow-Origin'], origin)
                self.assertEqual(self.request(method, path, auth=False,
                    headers={'Origin': origin})[0], 401)
                raw = b'{"metadata":null}' if method == 'POST' else b''
                status, headers, body = self.request(method, path, body=raw,
                    headers={'Origin': origin, 'Content-Type': 'application/json'})
                self.assertEqual(status, 200)
                self.assertEqual(headers['Access-Control-Allow-Origin'], origin)
                row = self.stack.application_requests[-1]
                self.assertEqual((row['method'], row['path'], row['body']),
                                 (method, path, raw.decode()))
                self.assertEqual((row['client'], row['token']), ('alpha', 'e'*64))
                self.assertIsNone(row['authorization'])

    def test_cors_errors_preserve_origin_and_never_bypass_authentication(self):
        status, headers, _ = self.request(headers={'Origin': 'https://client.example'}, auth=False)
        self.assertEqual(status, 401)
        self.assertEqual(headers['Access-Control-Allow-Origin'], 'https://client.example')
        self.assertIn('Origin', headers['Vary'])
        self.assertEqual(self.request(headers={'Origin': 'https://untrusted.example'})[0], 403)
        fields = {'Origin': 'https://client.example', 'Access-Control-Request-Method': 'POST',
                  'Access-Control-Request-Headers': 'x-q4t-gateway-token'}
        self.assertEqual(self.request('OPTIONS', '/v1/chat/completions', headers=fields)[0], 403)
        fields['Access-Control-Request-Headers'] = 'authorization'
        fields['Access-Control-Request-Method'] = 'DELETE'
        self.assertEqual(self.request('OPTIONS', '/v1/chat/completions', headers=fields)[0], 403)
        self.assertEqual(self.stack.requests, [])

    def test_certificate_rotation_keeps_same_process_and_existing_sse(self):
        before = self.admin()
        sock, response = self.stream()
        try:
            shutil.copyfile(self.certificates/'next.pem', self.assets/'staged.pem')
            (self.assets/'staged.pem').chmod(0o644)
            os.replace(self.assets/'staged.pem', self.assets/'server.pem')
            after = self.reload()
            self.assertEqual(after['entrypoint_pid'], before['entrypoint_pid'])
            self.assertEqual(after['generation'], before['generation']+1)
            self.assertNotEqual(after['fingerprint_sha256'], before['fingerprint_sha256'])
            self.assertFalse(self.stack.stream_disconnected.is_set())
            with self.connect() as fresh:
                self.assertEqual(hashlib.sha256(fresh.getpeercert(True)).hexdigest(),
                                 after['fingerprint_sha256'])
            self.assertEqual(self.stack.request(dict(prompt='after rotation'))[0], 200)
            self.stack.release_stream.set()
            response.read()
        finally:
            response.close()
            sock.close()

    def test_invalid_reload_retains_valid_certificate_then_recovers(self):
        before = self.admin()
        shutil.copyfile(self.certificates/'client.key', self.assets/'server.key')
        failed = self.reload()
        self.assertEqual(failed['error'], 'tls_private_key_mismatch_or_invalid')
        self.assertEqual(failed['generation'], before['generation'])
        self.assertTrue(failed['ready'])
        self.assertEqual(self.stack.request(dict(prompt='retained certificate'))[0], 200)
        shutil.copyfile(self.certificates/'server.key', self.assets/'server.key')
        recovered = self.reload()
        self.assertEqual(recovered['error'], '')
        self.assertEqual(recovered['entrypoint_pid'], before['entrypoint_pid'])

    def test_mtls_requires_trusted_client_certificate_without_changing_api_auth(self):
        self.tls.update(ca=str(self.certificates/'ca.pem'), require_client_certificate=True)
        self.replace()
        with self.assertRaises((ssl.SSLError, ConnectionError)):
            self.request()
        self.context.load_cert_chain(str(self.certificates/'client.pem'),
                                     str(self.certificates/'client.key'))
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request(auth=False)[0], 401)

    def test_expired_name_and_key_permission_failures_are_bounded_and_isolated(self):
        before = self.admin()
        shutil.copyfile(self.certificates/'expired.pem', self.assets/'server.pem')
        self.assertEqual(self.reload()['error'], 'tls_certificate_expired_or_not_yet_valid')
        self.assertEqual(self.stack.request()[0], 200)
        shutil.copyfile(self.certificates/'server.pem', self.assets/'server.pem')
        (self.assets/'server.key').chmod(0o644)
        self.assertEqual(self.reload()['error'], 'tls_material_unreadable')
        (self.assets/'server.key').chmod(0o600)
        self.assertEqual(self.reload()['error'], '')
        bad_policy = dict(self.stack.value['policy'], server_name='wrong.example')
        with self.assertRaisesRegex(ValueError, 'credential validation'):
            tls_control.check(INGRESS, bad_policy, self.tls, self.assets)
        self.assertEqual(self.admin()['entrypoint_pid'], before['entrypoint_pid'])

    def test_client_crl_rotation_rejects_revoked_client_and_preserves_others(self):
        shutil.copyfile(self.certificates/'clean.crl', self.assets/'clients.crl')
        (self.assets/'clients.crl').chmod(0o644)
        self.tls.update(ca=str(self.certificates/'ca.pem'), require_client_certificate=True,
                        crl=str(self.assets/'clients.crl'))
        self.context.load_cert_chain(str(self.certificates/'client.pem'),
                                     str(self.certificates/'client.key'))
        self.replace()
        self.assertEqual(self.request()[0], 200)
        before = self.admin()
        self.assertTrue(before['client_crl_enforced'])
        self.assertGreater(before['valid_until_unix'], time.time())
        shutil.copyfile(self.certificates/'revoked.crl', self.assets/'clients.crl')
        self.assertEqual(self.reload()['error'], '')
        with self.assertRaises((ssl.SSLError, ConnectionError)):
            self.request()
        self.context.load_cert_chain(str(self.certificates/'other-client.pem'),
                                     str(self.certificates/'client.key'))
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.admin()['entrypoint_pid'], before['entrypoint_pid'])

    def test_certificate_expiry_unbinds_entrypoint_without_stopping_backend(self):
        until = datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=7)
        with (self.assets/'short-certificate.log').open('wb') as log:
            subprocess.run([OPENSSL, 'ca', '-batch', '-config', 'ca.cnf', '-notext',
                '-in', 'server.csr', '-extfile', 'server.ext',
                '-enddate', until.strftime('%Y%m%d%H%M%SZ'), '-out',
                str(self.assets/'short.pem')], cwd=self.certificates, check=True,
                stdout=log, stderr=log, timeout=5)
        (self.assets/'short.pem').chmod(0o644)
        os.replace(self.assets/'short.pem', self.assets/'server.pem')
        initial = self.reload()
        self.assertEqual(initial['error'], '')
        self.assertTrue(initial['expires_within_seven_days'])
        self.assertEqual(self.stack.request()[0], 200)
        self.stack.wait(lambda: self.stack.bound == 0, timeout=10)
        self.assertFalse(self.admin()['ready'])
        self.assertEqual(self.admin()['entrypoint_pid'], initial['entrypoint_pid'])
        backend = http.client.HTTPConnection('127.0.0.1',
            self.stack.value['policy']['upstream_port'], timeout=2)
        try:
            backend.request('GET', '/readyz')
            self.assertEqual(backend.getresponse().status, 200)
        finally:
            backend.close()
        shutil.copyfile(self.certificates/'server.pem', self.assets/'server.pem')
        self.assertEqual(self.reload()['error'], '')
        self.ready()
        self.assertEqual(self.stack.request(dict(prompt='certificate recovered'))[0], 200)

    def test_nonregular_key_cannot_block_reload_or_core_requests(self):
        key = self.assets/'server.key'
        key.unlink()
        os.mkfifo(key, 0o600)
        start = time.monotonic()
        self.assertEqual(self.reload()['error'], 'tls_material_unreadable')
        self.assertLess(time.monotonic()-start, 2)
        self.assertEqual(self.stack.request(dict(prompt='FIFO isolated'))[0], 200)

    def test_reader_lease_prevents_duplicate_io_across_entrypoint_generations(self):
        address = f'\0q4t-tls-{os.geteuid()}-{self.stack.service_id}'
        before = self.admin()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as occupied:
            occupied.bind(address)
            for _ in range(3):
                self.assertEqual(self.reload()['error'], 'tls_reader_busy')
                self.assertEqual(self.stack.request()[0], 200)
        self.assertEqual(self.reload()['error'], '')
        self.assertEqual(self.admin()['entrypoint_pid'], before['entrypoint_pid'])

    def test_plaintext_or_slow_tls_peers_do_not_block_other_clients(self):
        value = self.stack.value['policy']
        peers = [socket.create_connection((value['listen_host'], value['listen_port']), timeout=4)
                 for _ in range(8)]
        try:
            peers[0].sendall(b'GET /v1/models HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
            try:
                self.assertFalse(peers[0].recv(4096).startswith(b'HTTP/'))
            except ConnectionResetError:
                pass  # TLS-only listeners may abort a plaintext handshake.
            self.assertEqual(self.stack.request(dict(prompt='other client'))[0], 200)
        finally:
            for peer in peers:
                peer.close()

    def test_tls_disconnect_cancels_upstream_without_replay(self):
        sock, response = self.stream()
        response.close()
        sock.close()
        self.assertTrue(self.stack.stream_disconnected.wait(2))
        self.assertEqual(len(self.stack.requests), 1)
        self.assertEqual(self.stack.request(dict(prompt='after cancellation'))[0], 200)

    def test_wrong_sni_and_invalid_tls_framing_do_not_reach_backend(self):
        with self.assertRaises(ssl.SSLError):
            self.connect(sni='wrong.example')
        with self.connect() as sock:
            sock.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                b'Authorization: Bearer '+b'A'*40+b'\r\nContent-Length: 0\r\n'
                b'Transfer-Encoding: chunked\r\n\r\n')
            response = http.client.HTTPResponse(sock)
            response.begin()
            self.assertEqual(response.status, 400)
        self.assertEqual(self.stack.requests, [])

    def test_late_encrypted_body_does_not_discard_early_authentication_error(self):
        with self.connect() as sock:
            sock.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                b'Content-Type: application/json\r\nAuthorization: Bearer wrong\r\n'
                b'Content-Length: 4096\r\n\r\n')
            # Allow the header-only admission to send 401 before the next TLS
            # record arrives. This used to turn a valid response into TCP RST.
            time.sleep(0.02)
            sock.sendall(b'x'*4096)
            response = http.client.HTTPResponse(sock)
            response.begin()
            self.assertEqual(response.status, 401)
            self.assertEqual(json.loads(response.read())['error']['code'],
                             'gateway_unauthorized')
        self.assertEqual(self.stack.requests, [])

    def test_private_admin_requires_live_parent_and_exact_native_identity(self):
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.admin(binary_sha256='0'*64)
        with self.assertRaisesRegex(ValueError, 'parent identity'):
            self.admin(parent_start=0)
        self.assertEqual(self.stack.request()[0], 200)

    def test_plaintext_does_not_load_crypto_and_tls_off_build_is_explicit(self):
        self.replace(tls=False)
        pid = self.admin()['entrypoint_pid']
        maps = Path(f'/proc/{pid}/maps').read_text()
        self.assertNotIn('libssl.so', maps)
        self.assertNotIn('libcrypto.so', maps)
        self.assertEqual(self.stack.request()[0], 200)
        if NO_TLS is None:
            self.fail('this acceptance requires the explicit TLS-OFF binary')
        self.replace(tls=False, binary=NO_TLS)
        self.assertEqual(self.stack.request()[0], 200)
        with self.assertRaisesRegex(ValueError, 'credential validation'):
            tls_control.check(NO_TLS, self.stack.value['policy'], self.tls, self.assets)

    def test_python_policy_rejects_credentialed_wildcard_and_unsafe_native_addresses(self):
        value = self.stack.value['policy']
        for cors in (dict(CORS, allowed_origins=['*']),
                     dict(CORS, allowed_origins=['https://client.example/path']),
                     dict(CORS, allowed_headers=['x-q4t-client-id'])):
            with self.assertRaises(ValueError):
                policy.policy(dict(value, cors=cors))
        for host in ('::', '0.0.0.0', 'ff02::1'):
            with self.assertRaises(ValueError):
                policy.policy(dict(value, listen_host=host))
        with self.assertRaises(ValueError):
            policy.policy(dict(value, allowed_client_cidrs=['::1/64']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--ingress', type=Path, required=True)
    parser.add_argument('--no-tls-ingress', type=Path, required=True)
    parser.add_argument('--host-tests', type=Path)
    args, rest = parser.parse_known_args()
    INGRESS, NO_TLS = args.ingress.resolve(strict=True), args.no_tls_ingress.resolve(strict=True)
    HOST_TESTS = args.host_tests.resolve(strict=True) if args.host_tests else None
    unittest.main(argv=[sys.argv[0], *rest])
