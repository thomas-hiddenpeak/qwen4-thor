"""Published tunnel contracts and real optional nginx ingress, CPU only."""
import http.client
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import tempfile
import time
import unittest

from jsonschema import Draft202012Validator
from tools.serve_gateway import gateway
from tools.serve_gateway import tls_fixture as strict_pki
from tools.serve_protocol import build_contract as api
from tools.serve_responses import build_contract as app
from tools.serve_responses import mcp_tunnel_contract as wire
from tools.serve_responses import test_mcp_tunnel_ingress as native
from tools.serve_recovery import test_gateway_nginx as fixture
from tools.serve_recovery import test_native_access as tls_fixture


class TunnelContract(unittest.TestCase):
    def validate(self, name, value):
        document = wire.schema()
        document['$ref'] = '#/$defs/'+name
        return list(Draft202012Validator(document).iter_errors(value))

    def test_generated_contract_and_complete_routes(self):
        doc = wire.ROOT/'docs'
        self.assertEqual(json.loads((doc/wire.DOCUMENT).read_text()), wire.schema())
        Draft202012Validator.check_schema(wire.schema())
        expected = api.openapi()
        self.assertEqual(json.loads((doc/'serve-openapi-v1.json').read_text()), expected)
        self.assertEqual(json.loads((doc/'serve-openapi-bundle-v1.json').read_text()),
                         app.standalone_openapi(expected))
        self.assertEqual(gateway.validate_routes(), gateway.sha(gateway.API_CONTRACT))
        for suffix, method in [('', 'get'), ('/poll', 'get'), ('/response', 'post')]:
            self.assertIn(method, expected['paths']['/v1/tunnels/{tunnel_id}'+suffix])

    def test_valid_commands_and_terminal_variants(self):
        command = dict(request_id='req_original', shard_token='opaque,token',
            channel='main', created_at='2026-11-16T00:00:00Z', response_timeout='12000ms',
            headers={'accept': ['application/json', 'text/event-stream']},
            command_type='jsonrpc', jsonrpc=dict(jsonrpc='2.0', id=9007199254740993,
                                               method='tools/list', params={}))
        self.assertFalse(self.validate('poll', dict(commands=[command])))
        end = dict(command, command_type='session_termination')
        end.pop('jsonrpc')
        self.assertFalse(self.validate('poll', dict(commands=[end])))
        rpc = dict(jsonrpc='2.0', id=9007199254740993, result=dict(tools=[]))
        upload = dict(request_id='req_original', resp_code=200, resp_json=rpc)
        for field in ('resp_json', 'rpc_resp'):
            row = dict(upload)
            row.pop('resp_json')
            row[field] = rpc
            self.assertFalse(self.validate('upload', row))
        for kind in ('notify_ack', 'session_termination_response'):
            self.assertFalse(self.validate('upload', dict(request_id='req_original',
                resp_code=204, resp_type=kind, arbitrary_metadata={'future': True})))
        self.assertFalse(self.validate('upload', dict(upload, resp_code=502,
            q4t_outcome_unknown=True, resp_json=dict(jsonrpc='2.0', id=9007199254740993,
                error=dict(code=-32603, message='Transport unavailable')))))
        self.assertFalse(self.validate('upload', dict(upload, resp_type='jsonrpc_notify',
            resp_json=dict(jsonrpc='2.0', method='notifications/progress', params={}))))

    def test_invalid_types_tokens_and_misleading_terminal_rejected(self):
        row = dict(request_id='req_original', resp_code=200,
            resp_json=dict(jsonrpc='2.0', id='original', result={}))
        cases = [dict(row, resp_code=True), dict(row, resp_code=600),
            dict(row, request_id='has space'), dict(row, request_id='x'*129),
            dict(row, request_id='ends-with-newline\n'),
            dict(row, shard_token='body-is-not-authority'),
            dict(row, rpc_resp=row['resp_json']), dict(row, resp_type='notify_ack'),
            dict(row, q4t_outcome_unknown=True), dict(row, q4t_outcome_unknown='true'),
            dict(row, resp_json=dict(row['resp_json'], error=dict(code=-1, message='x'))),
            dict(row, resp_json=dict(row['resp_json'], id=1.25)),
            dict(row, resp_headers={'mcp-session-id': 'not-an-array'}),
            dict(row, resp_headers={'mcp-session-id': ['bad\r\ninjection']}),
            dict(row, resp_headers={'mcp-session-id\n': ['value']}),
            dict(row, resp_type='jsonrpc_notify'), dict(row, channel='*')]
        for value in cases:
            with self.subTest(value=value):
                self.assertTrue(self.validate('upload', value))

    def test_stored_chat_file_parts_preserve_public_contract(self):
        from tools.serve_chat_storage.build_contract import definitions
        document = json.loads((wire.ROOT/'docs/serve-application-v1.schema.json').read_text())
        for key, value in definitions().items():
            self.assertEqual(document['$defs'][key], value)
        validator = Draft202012Validator(dict(document, **{'$ref': '#/$defs/stored_chat_message'}))
        part = dict(type='file', file=dict(file_id='file_project'))
        row = dict(id='msg_original', role='user', content=None, name=None,
                   content_parts=[part])
        self.assertEqual(list(validator.iter_errors(row)), [])
        row['content_parts'] = [dict(type='file', file={})]
        self.assertTrue(list(validator.iter_errors(row)))


class OptionalTunnel(native.TunnelIngress):
    def setUp(self):
        receipt = json.loads((wire.ROOT/'.q4t-work/serve-gateway-20261103/vendor/receipt.json').read_text())
        fixture.NGINX = Path(receipt['binary']).resolve(strict=True)
        self.assertEqual(gateway.sha(fixture.NGINX), receipt['binary_sha256'])
        self.ingress = None
        self.stack = self.make_stack()

    def make_stack(self, **kwargs):
        if 'policy_overrides' in kwargs:
            policy = dict(kwargs['policy_overrides'])
            if policy.get('version') == 3:
                policy.update(version=2, allowed_client_cidrs=['127.0.0.0/8'])
                policy.pop('cors', None)
            kwargs['policy_overrides'] = policy
        if kwargs.get('tls'):
            kwargs['tls'] = {key: kwargs['tls'][key] for key in ('certificate', 'key')}
            kwargs.setdefault('policy_overrides', {})['server_name'] = 'localhost'
        return fixture.Stack(self.output, **kwargs)

    def test_tls_tunnel_token_and_body_forwarding(self):
        self.stack.close()
        pem = Path(tempfile.mkdtemp(prefix='optional-tunnel-tls-', dir=self.output))/'pem'
        tls_fixture.certificates(pem)
        context = ssl.create_default_context(cafile=str(pem/'ca.pem'))
        self.stack = self.make_stack(tls=dict(certificate=str(pem/'server.pem'),
            key=str(pem/'server.key')), policy_overrides=dict(version=2,
                allowed_client_cidrs=['127.0.0.0/8']))
        self.ready()
        connection = http.client.HTTPSConnection('localhost',
            self.stack.value['policy']['listen_port'], context=context, timeout=4)
        body = dict(request_id='original', resp_code=200, resp_type='jsonrpc_response',
                    resp_json=dict(jsonrpc='2.0', id=9007199254740993, result=dict(tools=[])))
        try:
            connection.request('POST', '/v1/tunnels/tunnel_local/response', json.dumps(body),
                {'Authorization': 'Bearer '+'A'*40, 'Content-Type': 'application/json',
                 'X-Tunnel-Client-Instance-Id': 'original-instance',
                 'X-Tunnel-Shard-Token': 'original-token'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200, response.read())
            row = self.stack.application_requests[-1]
            self.assertEqual(json.loads(row['body']), body)
            self.assertEqual((row['tunnel_instance'], row['tunnel_shard_token']),
                             ('original-instance', 'original-token'))
            self.assertIsNone(row['authorization'])
        finally:
            connection.close()

    def test_single_opaque_comma_is_preserved_and_invalid_headers_never_forward(self):
        self.ready()
        path = '/v1/tunnels/tunnel_local/response'
        auth = 'Authorization: Bearer '+'A'*40+'\r\n'
        for header in ('X-Tunnel-Client-Instance-Id: has space\r\n',
                       'X-Tunnel-Shard-Token: '+('x'*129)+'\r\n',
                       'Trailer: X-Tunnel-Shard-Token\r\n',
                       'Connection: keep-alive, X-Tunnel-Client-Instance-Id\r\n',
                       'X-Tunnel-Shard-Token: a\r\nX-Tunnel-Shard-Token:\r\n'):
            self.assertEqual(self.raw(auth+header, path=path)[0], 400)
        self.assertEqual(self.stack.application_requests, [])
        self.assertEqual(self.raw(auth+'X-Tunnel-Client-Instance-Id: original,instance\r\n'
            'X-Tunnel-Shard-Token: original,token\r\n', path=path)[0], 200)
        row = self.stack.application_requests[-1]
        self.assertEqual((row['tunnel_instance'], row['tunnel_shard_token']),
                         ('original,instance', 'original,token'))

    def test_standalone_private_package_tls_wire(self):
        self.ready()
        directory = Path(tempfile.mkdtemp(prefix='standalone-tunnel-', dir=self.output))
        ca = strict_pki.ca(directory/'ca')
        now = time.time()
        certificate, key = strict_pki.issue(directory/'leaf', 'localhost', ca,
            now-30, now+2*86400, host='localhost')
        policy = dict(self.stack.value['policy'], listen_port=fixture.free_port(),
                      version=2, allowed_client_cidrs=['127.0.0.0/8'], server_name='localhost')
        gateway.save(directory/'policy.json', json.dumps(policy).encode())
        backend = dict(self.stack.value['backend_policy'], gateway_token=self.stack.token)
        gateway.save(directory/'clients.json', json.dumps(backend).encode())
        prepared = gateway.prepare(directory/'policy.json', fixture.NGINX,
            certificate, key, directory/'package', directory/'clients.json',
            trusted_ca=ca[0])
        package = Path(prepared['package'])
        gateway.check(package, prepared['manifest_sha256'])
        context = ssl.create_default_context(cafile=str(ca[0]))
        with (directory/'nginx-private.log').open('wb') as log:
            process = subprocess.Popen([str(fixture.NGINX), '-p', str(package)+'/',
                '-c', str(package/'nginx.conf')], stdout=log, stderr=log,
                start_new_session=True, umask=0o077)
            try:
                deadline = time.monotonic()+5
                while True:
                    connection = http.client.HTTPSConnection('localhost', policy['listen_port'],
                                                              context=context, timeout=3)
                    try:
                        connection.request('POST', '/v1/tunnels/tunnel_local/response',
                            '{"request_id":"req_original","resp_code":204,"resp_type":"notify_ack"}',
                            {'Authorization': 'Bearer '+'A'*40,
                             'Content-Type': 'application/json',
                             'X-Tunnel-Client-Instance-Id': 'instance',
                             'X-Tunnel-Shard-Token': 'shard'})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200, response.read())
                        break
                    except ConnectionRefusedError:
                        if time.monotonic() >= deadline or process.poll() is not None:
                            raise
                        time.sleep(0.02)
                    finally:
                        connection.close()
                row = self.stack.application_requests[-1]
                self.assertEqual((row['client'], row['token']),
                    ('alpha', self.stack.token))
                self.assertEqual((row['tunnel_instance'], row['tunnel_shard_token']),
                                 ('instance', 'shard'))
                self.assertIsNone(row['authorization'])
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGQUIT)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
