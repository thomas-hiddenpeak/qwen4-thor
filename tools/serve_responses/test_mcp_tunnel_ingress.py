"""Native tunnel routes, TLS and independent lanes; CPU peers, no model/GPU."""
import http.client
import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest

from tools.serve_recovery import test_gateway_nginx as fixture
from tools.serve_recovery import test_native_ingress as native
from tools.serve_recovery import test_native_access as tls_fixture


class TunnelIngress(unittest.TestCase):
    setUpClass = classmethod(fixture.NginxTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.NginxTests.tearDownClass.__func__)
    tearDown = fixture.NginxTests.tearDown
    ready = fixture.NginxTests.ready
    raw = native.NativeTests.raw
    stream = native.NativeTests.stream

    def setUp(self):
        self.ingress = Path(os.environ['Q4T_INGRESS']).resolve(strict=True)
        self.stack = self.make_stack()

    def make_stack(self, **kwargs):
        return fixture.Stack(self.output, native_ingress=self.ingress, **kwargs)

    def test_tunnel_routes_auth_preserve_original_wire_headers(self):
        self.ready()
        for method, path in [('GET', '/v1/tunnels/tunnel_local'),
                ('GET', '/v1/tunnels/tunnel_local/poll?channel=main&timeout_ms=0'),
                ('POST', '/v1/tunnels/tunnel_local/response')]:
            body = b'{"request_id":"original","resp_code":200}' if method == 'POST' else b''
            headers = ('X-Tunnel-Client-Instance-Id: original-instance\r\n'
                       'Content-Type: application/json\r\n')
            if method == 'POST':
                headers += 'X-Tunnel-Shard-Token: original-token\r\n'
            self.assertEqual(self.raw('Authorization: Bearer wrong\r\n'+headers,
                method=method, path=path, length=len(body), body=body)[0], 401)
            self.assertEqual(self.raw('Authorization: Bearer '+'A'*40+'\r\n'+headers,
                method=method, path=path, length=len(body), body=body)[0], 200)
            row = self.stack.application_requests[-1]
            self.assertEqual((row['path'], row['body'], row['tunnel_instance']),
                             (path, body.decode(), 'original-instance'))
            self.assertEqual(row['tunnel_shard_token'], 'original-token' if method == 'POST' else None)
            self.assertEqual((row['client'], row['token']), ('alpha', 'e'*64))
            self.assertIsNone(row['authorization'])
            port = 'upstream_operations_port' if path.endswith('tunnel_local') else 'upstream_port'
            self.assertEqual(row['upstream_port'], self.stack.value['policy'][port])

    def held(self, method, path):
        connection = http.client.HTTPConnection('127.0.0.1',
            self.stack.value['policy']['listen_port'], timeout=4)
        connection.request(method, path, '{}' if method == 'POST' else None,
            {'Authorization': 'Bearer '+'A'*40, 'Content-Type': 'application/json',
             'X-Tunnel-Client-Instance-Id': 'original-instance',
             **({'X-Tunnel-Shard-Token': 'original-token'} if method == 'POST' else {})})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b'cpu-held-application', response.readline())
        return connection, response

    def test_poll_and_response_capacity_do_not_depend_on_generation_or_each_other(self):
        self.stack.close()
        self.stack = self.make_stack(
            policy_overrides=dict(per_client_inflight=1, global_inflight=1))
        self.ready()
        poll = '/v1/tunnels/tunnel_local/poll'
        response = '/v1/tunnels/tunnel_local/response'
        self.stack.held_application_paths.update((poll, response))
        owned = [self.stream()]
        try:
            owned.append(self.held('GET', poll))
            auth = ('Authorization: Bearer '+'A'*40+'\r\n'
                    'X-Tunnel-Client-Instance-Id: original-instance\r\n')
            self.assertEqual(self.raw(auth, method='GET', path=poll)[0], 429)
            owned.append(self.held('POST', response))
            owned.append(self.held('POST', response))
            self.assertEqual(self.raw(auth+'X-Tunnel-Shard-Token: original-token\r\n'
                'Content-Type: application/json\r\n', path=response,
                length=2, body=b'{}')[0], 429)
            self.assertEqual(self.raw(auth, method='GET', path='/v1/tunnels/tunnel_local')[0], 200)
            self.assertEqual(self.stack.request(dict(prompt='occupied'))[0], 429)
            self.assertEqual(self.raw(auth, method='GET', path='/v1/models')[0], 200)
        finally:
            self.stack.release_applications.set()
            for connection, reply in owned:
                reply.close()
                connection.close()

    def test_duplicate_and_connection_nominated_tokens_rejected_before_forwarding(self):
        self.ready()
        auth = 'Authorization: Bearer '+'A'*40+'\r\n'
        headers = 'X-Tunnel-Client-Instance-Id: instance\r\nX-Tunnel-Shard-Token: token\r\n'
        for extra in ('X-Tunnel-Shard-Token: different\r\n',
                      'X-Tunnel-Client-Instance-Id: different\r\n',
                      'Connection: X-Tunnel-Shard-Token\r\n',
                      'Connection: X-Tunnel-Client-Instance-Id\r\n'):
            self.assertEqual(self.raw(auth+headers+extra,
                path='/v1/tunnels/tunnel_local/response')[0], 400)
        self.assertEqual(self.stack.application_requests, [])

    def test_tls_tunnel_token_and_body_forwarding(self):
        self.stack.close()
        pem = Path(tempfile.mkdtemp(prefix='tunnel-tls-', dir=self.output))/'pem'
        tls_fixture.certificates(pem)
        context = ssl.create_default_context(cafile=str(pem/'ca.pem'))
        tls = dict(certificate=str(pem/'server.pem'), key=str(pem/'server.key'),
                   ca='', require_client_certificate=False)
        self.stack = self.make_stack(
            policy_overrides=dict(version=3, cors=None), tls=tls, ssl_context=context)
        self.ready()
        body = dict(request_id='original', resp_code=200, resp_type='jsonrpc_response',
                    resp_json=dict(jsonrpc='2.0', id=9007199254740993, result=dict(tools=[])))
        result = self.stack.request(body, path='/v1/tunnels/tunnel_local/response',
            extra={'X-Tunnel-Client-Instance-Id': 'original-instance',
                   'X-Tunnel-Shard-Token': 'original-token'})
        self.assertEqual(result[0], 200)
        row = self.stack.application_requests[-1]
        self.assertEqual(json.loads(row['body']), body)
        self.assertEqual((row['tunnel_instance'], row['tunnel_shard_token']),
                         ('original-instance', 'original-token'))
        self.assertIsNone(row['authorization'])


if __name__ == '__main__':
    unittest.main()
