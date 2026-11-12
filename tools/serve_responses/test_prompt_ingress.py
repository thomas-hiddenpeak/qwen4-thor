"""Actual native ingress Prompt registry routes, CPU peers; no model or GPU."""
import json
import os
from pathlib import Path
import unittest

from tools.serve_recovery import test_gateway_nginx as fixture
from tools.serve_recovery import test_native_ingress as native


class PromptIngress(unittest.TestCase):
    setUpClass = classmethod(fixture.NginxTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.NginxTests.tearDownClass.__func__)
    tearDown = fixture.NginxTests.tearDown
    ready = fixture.NginxTests.ready
    raw = native.NativeTests.raw
    stream = native.NativeTests.stream

    def setUp(self):
        self.ingress = Path(os.environ['Q4T_INGRESS']).resolve(strict=True)
        self.stack = fixture.Stack(self.output, native_ingress=self.ingress)

    def test_prompt_auth_preserves_all_routes_and_client_scope(self):
        self.ready()
        routes = [('POST', '/v1/prompts'), ('GET', '/v1/prompts?limit=1'),
            ('GET', '/v1/prompts/prompt_a'), ('POST', '/v1/prompts/prompt_a'),
            ('DELETE', '/v1/prompts/prompt_a'),
            ('POST', '/v1/prompts/prompt_a/versions'),
            ('GET', '/v1/prompts/prompt_a/versions/2')]
        for method, path in routes:
            body = b'{"native":"body-preserved"}' if method == 'POST' else b''
            self.assertEqual(self.raw('Authorization: Bearer wrong\r\n',
                method=method, path=path, length=len(body), body=body)[0], 401)
            for key, client in [('A', 'alpha'), ('B', 'beta')]:
                self.assertEqual(self.raw('Authorization: Bearer '+key*40+'\r\n',
                    method=method, path=path, length=len(body), body=body)[0], 200)
                row = self.stack.application_requests[-1]
                self.assertEqual((row['method'], row['path'], row['body']),
                                 (method, path, body.decode()))
                self.assertEqual((row['client'], row['token']), (client, 'e'*64))
                self.assertIsNone(row['authorization'])

    def test_prompt_mutations_use_data_and_reads_keep_reserved_capacity(self):
        self.stack.close()
        self.stack = fixture.Stack(self.output, native_ingress=self.ingress,
            policy_overrides=dict(per_client_inflight=1, global_inflight=1))
        self.ready()
        connection, response = self.stream()
        try:
            auth = 'Authorization: Bearer '+'A'*40+'\r\n'
            body = b'{"model":"cpu","input":"compact"}'
            self.assertEqual(self.raw(auth, path='/v1/prompts',
                length=len(body), body=body)[0], 429)
            self.assertFalse(self.stack.application_requests)
            self.assertEqual(self.raw(auth, method='GET',
                path='/v1/prompts/prompt_a')[0], 200)
        finally:
            response.close()
            connection.close()


if __name__ == '__main__':
    unittest.main()
