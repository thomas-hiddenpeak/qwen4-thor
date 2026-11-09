"""Actual native ingress input token count route, CPU peers; no model or GPU."""
import json
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


if __name__ == '__main__':
    unittest.main()
