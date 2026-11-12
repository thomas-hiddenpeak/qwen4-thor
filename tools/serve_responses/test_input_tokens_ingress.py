"""Actual native ingress input token count route, CPU peers; no model or GPU."""
import json
import http.client
import os
from pathlib import Path
import unittest

from tools.serve_recovery import test_gateway_nginx as fixture
from tools.serve_recovery import test_native_ingress as native


class InputTokensIngress(unittest.TestCase):
    setUpClass = classmethod(fixture.NginxTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.NginxTests.tearDownClass.__func__)
    tearDown = fixture.NginxTests.tearDown
    ready = fixture.NginxTests.ready
    raw = native.NativeTests.raw
    stream = native.NativeTests.stream

    def setUp(self):
        self.ingress = Path(os.environ['Q4T_INGRESS']).resolve(strict=True)
        self.stack = fixture.Stack(self.output, native_ingress=self.ingress)

    def test_count_auth_preserves_body_and_client_scope(self):
        self.ready()
        body = json.dumps(dict(model='cpu', input='count this',
                               instructions='Keep exact facts.')).encode()
        path = '/v1/responses/input_tokens'
        self.assertEqual(self.raw('Authorization: Bearer wrong\r\n', path=path,
                                  length=len(body), body=body)[0], 401)
        self.assertFalse(self.stack.application_requests)
        for key, client in [('A', 'alpha'), ('B', 'beta')]:
            self.assertEqual(self.raw('Authorization: Bearer '+key*40+'\r\n',
                path=path, length=len(body), body=body)[0], 200)
            row = self.stack.application_requests[-1]
            self.assertEqual((row['method'], row['path'], row['body']),
                             ('POST', path, body.decode()))
            self.assertEqual((row['client'], row['token']), (client, 'e'*64))
            self.assertIsNone(row['authorization'])

    def test_count_uses_data_capacity_and_keeps_observation_available(self):
        self.stack.close()
        self.stack = fixture.Stack(self.output, native_ingress=self.ingress,
            policy_overrides=dict(per_client_inflight=1, global_inflight=1))
        self.ready()
        connection, response = self.stream()
        try:
            auth = 'Authorization: Bearer '+'A'*40+'\r\n'
            body = b'{"model":"cpu","input":"count"}'
            self.assertEqual(self.raw(auth, path='/v1/responses/input_tokens',
                length=len(body), body=body)[0], 429)
            self.assertFalse(self.stack.application_requests)
            self.assertEqual(self.raw(auth, method='GET',
                path='/v1/responses/resp_existing')[0], 200)
        finally:
            response.close()
            connection.close()

    def preparation_request(self, fields):
        self.stack.response_headers = fields
        connection = http.client.HTTPConnection(
            '127.0.0.1', self.stack.value['policy']['listen_port'], timeout=4)
        try:
            connection.request('POST', '/v1/responses/input_tokens', '{}',
                headers={'Authorization': 'Bearer '+'A'*40,
                         'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_preparation_usage_crosses_native_ingress_without_private_headers(self):
        self.ready()
        fields = [('X-Q4T-Preparation-Input-Tokens', '111'),
                  ('X-Q4T-Preparation-Output-Tokens', '19'),
                  ('X-Q4T-Preparation-Model-Steps', '1')]
        status, headers, body = self.preparation_request(fields + [
            ('X-Q4T-Private-Fixture', 'secret'), ('Set-Cookie', 'private=secret')])
        self.assertEqual(status, 200)
        for name, value in fields:
            self.assertEqual(headers[name], value)
        self.assertNotIn('X-Q4T-Private-Fixture', headers)
        self.assertNotIn('Set-Cookie', headers)
        self.assertTrue(json.loads(body)['cpu_fixture'])

    def test_invalid_preparation_headers_fail_only_the_current_response(self):
        self.ready()
        fields = [('X-Q4T-Preparation-Input-Tokens', '111'),
                  ('X-Q4T-Preparation-Output-Tokens', '19'),
                  ('X-Q4T-Preparation-Model-Steps', '1')]
        invalid = [fields[:1], fields + [fields[0]],
                   fields + [('Connection', 'x-q4t-preparation-input-tokens')]]
        invalid += [[(fields[0][0], value), *fields[1:]]
                    for value in ('-1', '1x', '18446744073709551616')]
        for row in invalid:
            status, _, body = self.preparation_request(row)
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)['error']['code'],
                             'service_binding_unavailable')
        self.assertEqual(self.preparation_request(fields)[0], 200)


if __name__ == '__main__':
    unittest.main()
