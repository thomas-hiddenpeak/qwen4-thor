"""Real native entrypoint, CPU backend/Guardian peers, no GPU or nginx required."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import journal
from tools.serve_recovery import test_gateway_nginx as fixture

INGRESS = None


class NativeTests(fixture.NginxTests):
    def setUp(self):
        self.stack = fixture.Stack(self.output, native_ingress=INGRESS)

    def replace_stack(self, **policy):
        self.stack.close()
        self.stack = fixture.Stack(self.output, native_ingress=INGRESS,
                                   policy_overrides=policy)
        self.ready()

    def raw(self, fields, *, length=0, body=b'', method='POST',
            path='/v1/chat/completions', source='127.0.0.1'):
        port = self.stack.value['policy']['listen_port']
        with socket.create_connection(('127.0.0.1', port), timeout=4,
                                      source_address=(source, 0)) as sock:
            sock.sendall((f'{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                f'Content-Length: {length}\r\n'+fields+'\r\n').encode()+body)
            response = http.client.HTTPResponse(sock)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def stream(self):
        connection = http.client.HTTPConnection('127.0.0.1',
            self.stack.value['policy']['listen_port'], timeout=4)
        connection.request('POST', '/v1/chat/completions', json.dumps(dict(stream=True)),
            {'Authorization': 'Bearer '+'A'*40, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b'"generation":1', response.readline())
        return connection, response

    def test_generation_rebind_rotates_token_and_strips_forged_client_headers(self):
        self.ready()
        self.assertEqual(self.stack.request(dict(prompt='forged'), extra={
            'X-Q4T-Client-Id': 'beta', 'X-Q4T-Gateway-Token': 'f'*64})[0], 400)
        self.assertEqual(self.stack.forwarded, [])
        self.assertEqual(self.stack.request(dict(prompt='first'))[0], 200)
        self.stack.generation, self.stack.token, self.stack.bound = 2, 'f'*64, 0
        self.ready()
        self.assertEqual(self.stack.request(dict(prompt='second'))[2]['generation'], 2)
        self.assertEqual([r['token'] for r in self.stack.forwarded], ['e'*64, 'f'*64])
        self.assertEqual([r['client'] for r in self.stack.forwarded], ['alpha', 'alpha'])
        self.assertTrue(all(r['authorization'] is None for r in self.stack.forwarded))

    def test_native_process_runs_with_no_nginx_or_systemd_in_path(self):
        self.ready()
        children = fixture.children(self.stack.process.pid)
        images = [str(Path(f'/proc/{pid}/exe').resolve()) for pid in children]
        self.assertIn(str(INGRESS), images)
        self.assertFalse(any('nginx' in Path(name).name for name in images))
        self.assertEqual(self.stack.request(dict(prompt='native'))[0], 200)

    def test_completions_preserve_body_and_enforce_native_auth(self):
        self.ready()
        request = dict(model='cpu-fixture', prompt=[[7, 8], [9]], n=2,
                       logprobs=3, echo=True)
        self.assertEqual(self.stack.request(request, path='/v1/completions',
                                            key='wrong')[0], 401)
        self.assertEqual(self.stack.forwarded, [])
        self.assertEqual(self.stack.request(request, path='/v1/completions')[0], 200)
        row = self.stack.forwarded[-1]
        self.assertEqual(json.loads(row['body']), request)
        self.assertEqual((row['path'], row['client'], row['token']),
                         ('/v1/completions', 'alpha', 'e'*64))
        self.assertIsNone(row['authorization'])

    def test_model_detail_routes_preserve_encoding_and_permissions(self):
        self.ready()
        auth = 'Authorization: Bearer '+'A'*40+'\r\n'
        for method, path, expected in [
            ('GET', '/v1/models/org%2Falias', 200),
            ('GET', '/v1/models/missing', 404),
            ('DELETE', '/v1/models/cpu-fixture', 403)]:
            with self.subTest(method=method, path=path):
                status, _, body = self.raw(auth, method=method, path=path)
                self.assertEqual(status, expected)
                self.assertTrue(json.loads(body))
                row = self.stack.model_requests[-1]
                self.assertEqual((row['method'], row['path']), (method, path))
                self.assertEqual((row['client'], row['token']), ('alpha', 'e'*64))
                self.assertIsNone(row['authorization'])

    def test_completions_data_limit_preserves_model_control_route(self):
        self.replace_stack(per_client_inflight=1, global_inflight=1)
        connection, response = self.stream()
        try:
            self.assertEqual(self.stack.request(dict(prompt='full'),
                                                path='/v1/completions')[0], 429)
            self.assertEqual(self.stack.request(path='/v1/models/cpu-fixture')[0], 200)
            self.assertEqual(len(self.stack.requests), 1)
        finally:
            response.close()
            connection.close()

    def test_conversations_preserve_methods_queries_and_authenticated_scope(self):
        self.ready()
        routes = [('POST', '/v1/conversations'),
                  ('POST', '/v1/conversations/conv_1'),
                  ('GET', '/v1/conversations/conv_1'),
                  ('DELETE', '/v1/conversations/conv_1'),
                  ('POST', '/v1/conversations/conv_1/items'),
                  ('GET', '/v1/conversations/conv_1/items?limit=2&order=asc&'
                          'include%5B%5D=reasoning.encrypted_content'),
                  ('GET', '/v1/conversations/conv_1/items/item_1?'
                          'include%5B%5D=message.output_text.logprobs'),
                  ('DELETE', '/v1/conversations/conv_1/items/item_1')]
        for method, path in routes:
            with self.subTest(method=method, path=path):
                body = b'{"items":[]}' if method == 'POST' else b''
                before = len(self.stack.application_requests)
                self.assertEqual(self.raw('Authorization: Bearer wrong\r\n',
                    method=method, path=path, length=len(body), body=body)[0], 401)
                self.assertEqual(len(self.stack.application_requests), before)
                for key, client in [('A', 'alpha'), ('B', 'beta')]:
                    self.assertEqual(self.raw('Authorization: Bearer '+key*40+'\r\n',
                        method=method, path=path, length=len(body), body=body)[0], 200)
                    row = self.stack.application_requests[-1]
                    self.assertEqual((row['method'], row['path'], row['body']),
                                     (method, path, body.decode()))
                    self.assertEqual((row['client'], row['token']), (client, 'e'*64))
                    self.assertIsNone(row['authorization'])

    def test_conversations_share_data_capacity_and_actual_peer_cidr(self):
        self.replace_stack(per_client_inflight=1, global_inflight=1,
                           allowed_client_cidrs=['127.0.0.1/32'])
        auth = 'Authorization: Bearer '+'A'*40+'\r\n'
        path = '/v1/conversations/conv_1/items'
        self.assertEqual(self.raw(auth, method='GET', path=path,
                                  source='127.0.0.2')[0], 403)
        connection, response = self.stream()
        try:
            self.assertEqual(self.raw(auth, method='GET', path=path)[0], 429)
            self.assertEqual(self.stack.application_requests, [])
            self.assertEqual(self.stack.request(path='/livez')[0], 200)
        finally:
            response.close()
            connection.close()

    def test_responses_preserve_methods_queries_and_authenticated_scope(self):
        self.ready()
        routes = [('POST', '/v1/responses'),
                  ('GET', '/v1/responses/resp_1?stream=true&starting_after=1&'
                          'include_obfuscation=false'),
                  ('GET', '/v1/responses/resp_1/input_items?limit=2&order=asc&'
                          'include%5B%5D=message.output_text.logprobs'),
                  ('DELETE', '/v1/responses/resp_1')]
        routes.append(('POST', '/v1/responses/resp_1/cancel'))
        for method, path in routes:
            with self.subTest(method=method, path=path):
                body = b'{"input":"query"}' if path == '/v1/responses' else b''
                before = len(self.stack.application_requests)
                self.assertEqual(self.raw('Authorization: Bearer wrong\r\n',
                    method=method, path=path, length=len(body), body=body)[0], 401)
                self.assertEqual(len(self.stack.application_requests), before)
                for key, client in [('A', 'alpha'), ('B', 'beta')]:
                    self.assertEqual(self.raw('Authorization: Bearer '+key*40+'\r\n',
                        method=method, path=path, length=len(body), body=body)[0], 200)
                    row = self.stack.application_requests[-1]
                    self.assertEqual((row['method'], row['path'], row['body']),
                                     (method, path, body.decode()))
                    self.assertEqual((row['client'], row['token']), (client, 'e'*64))
                    self.assertIsNone(row['authorization'])

    def test_stored_chat_routes_keep_methods_query_identity_and_control_lane(self):
        self.replace_stack(per_client_inflight=1, global_inflight=1,
                           allowed_client_cidrs=['127.0.0.1/32'])
        routes = [('GET', '/v1/chat/completions?limit=1&metadata%5Btopic%5D=%E8%B5%84%E6%96%99'),
                  ('GET', '/v1/chat/completions/chat_1'),
                  ('POST', '/v1/chat/completions/chat_1'),
                  ('DELETE', '/v1/chat/completions/chat_1'),
                  ('GET', '/v1/chat/completions/chat_1/messages?after=chatmsg_1')]
        auth = 'Authorization: Bearer '+'A'*40+'\r\n'
        self.assertEqual(self.raw(auth, method='GET', path=routes[0][1],
                                  source='127.0.0.2')[0], 403)
        connection, response = self.stream()
        try:
            for method, path in routes:
                body = b'{"metadata":{"topic":"records"}}' if method == 'POST' else b''
                before = len(self.stack.application_requests)
                self.assertEqual(self.raw('Authorization: Bearer wrong\r\n',
                    method=method, path=path, length=len(body), body=body)[0], 401)
                self.assertEqual(len(self.stack.application_requests), before)
                self.assertEqual(self.raw(auth, method=method, path=path,
                    length=len(body), body=body)[0], 200)
                row = self.stack.application_requests[-1]
                self.assertEqual((row['method'], row['path'], row['body']),
                                 (method, path, body.decode()))
                self.assertEqual((row['client'], row['token']), ('alpha', 'e'*64))
                self.assertIsNone(row['authorization'])
            self.assertEqual(self.stack.request(dict(prompt='full'))[0], 429)
        finally:
            response.close()
            connection.close()

    def test_responses_keep_control_capacity_and_actual_peer_cidr(self):
        self.replace_stack(per_client_inflight=1, global_inflight=1,
                           allowed_client_cidrs=['127.0.0.1/32'])
        auth = 'Authorization: Bearer '+'A'*40+'\r\n'
        path = '/v1/responses/resp_1/input_items'
        self.assertEqual(self.raw(auth, method='GET', path=path,
                                  source='127.0.0.2')[0], 403)
        connection, response = self.stream()
        try:
            self.assertEqual(self.raw(auth, method='GET', path=path)[0], 200)
            self.assertEqual(self.raw(auth, method='POST',
                path='/v1/responses/resp_1/cancel')[0], 200)
            self.assertEqual(len(self.stack.application_requests), 2)
            self.assertEqual(self.raw(auth, method='POST', path='/v1/responses',
                length=2, body=b'{}')[0], 429)
            self.assertEqual(self.stack.request(path='/livez')[0], 200)
        finally:
            response.close()
            connection.close()

    def test_response_cancel_has_reserved_capacity_beside_full_stream_controls(self):
        self.replace_stack(per_client_inflight=1, global_inflight=1)
        auth = 'Authorization: Bearer '+'A'*40+'\r\n'
        paths = [f'/v1/responses/resp_{i}?stream=true' for i in range(5)]
        held_cancel = '/v1/responses/resp_hold/cancel'
        self.stack.held_application_paths.update(paths + [held_cancel])
        connections = []
        def open_stream(method, path):
            c = http.client.HTTPConnection('127.0.0.1',
                self.stack.value['policy']['listen_port'], timeout=3)
            connections.append(c)
            c.request(method, path, headers={'Authorization':'Bearer '+'A'*40})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertIn(b'cpu-held-application', r.readline())
            return r
        try:
            streams = [open_stream('GET', path) for path in paths[:4]]
            self.assertEqual(self.raw(auth, method='GET', path=paths[4])[0], 429)
            self.assertEqual(self.raw(auth, method='POST',
                path='/v1/responses/resp_free/cancel')[0], 200)
            streams.append(open_stream('POST', held_cancel))
            self.assertEqual(self.raw(auth, method='POST',
                path='/v1/responses/resp_free/cancel')[0], 429)
            self.assertEqual(self.raw('Authorization: Bearer '+'B'*40+'\r\n',
                method='POST', path='/v1/responses/resp_free/cancel')[0], 200)
        finally:
            self.stack.release_applications.set()
            for c in connections: c.close()

    def test_duplicate_bearer_and_ambiguous_framing_never_reach_backend(self):
        self.ready()
        auth = 'Authorization: Bearer '+'A'*40+'\r\n'
        for field in ('Authorization: Bearer '+'B'*40+'\r\n',
                      'Content-Length: 0\r\n', 'Transfer-Encoding: chunked\r\n',
                      'Host: forbidden\r\n'):
            with self.subTest(field=field.split(':')[0]):
                self.assertEqual(self.raw(auth+field)[0], 400)
        self.assertEqual(self.raw(auth+'Expect: unknown\r\n')[0], 417)
        self.assertEqual(self.stack.forwarded, [])

    def test_authentication_precedes_body_reads_and_recovery_error(self):
        self.ready()
        self.stack.ready = False
        start = time.monotonic()
        status, headers, _ = self.raw('Authorization: Bearer '+'C'*40+'\r\n',
                                      length=1000)
        self.assertEqual(status, 401)
        self.assertIn('WWW-Authenticate', headers)
        self.assertLess(time.monotonic()-start, 2)
        self.assertEqual(self.stack.forwarded, [])

    def test_header_body_limit_rejects_without_consuming_large_body(self):
        self.replace_stack(body_bytes=1024)
        status, _, _ = self.raw('Authorization: Bearer '+'A'*40+'\r\n', length=1025)
        self.assertEqual(status, 413)
        self.assertEqual(self.stack.forwarded, [])

    def test_large_inline_request_and_response_preserve_exact_bytes(self):
        self.replace_stack(body_bytes=96*1024**2)
        # Transport oracle only: the peer echoes actual original bytes, never
        # claims that this binary source was decoded or interpreted by a model.
        encoded = 'A'*(4*((50000000+2)//3)-1)+'='
        request = dict(messages=[dict(role='user', content=[dict(type='file',
            file=dict(filename='capacity.pdf', file_data=encoded))])])
        raw = json.dumps(request, separators=(',', ':')).encode()
        def echo(handler, body, generation):
            handler.response(200, body)
            return True
        self.stack.response_hook = echo
        connection = http.client.HTTPConnection('127.0.0.1',
            self.stack.value['policy']['listen_port'], timeout=20)
        try:
            connection.request('POST', '/v1/chat/completions', raw,
                {'Authorization': 'Bearer '+'A'*40,
                 'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            result = json.loads(response.read())
            self.assertEqual(result, request)
        finally:
            connection.close()
        self.assertEqual(len(self.stack.forwarded), 1)
        forwarded = self.stack.forwarded[0]
        self.assertEqual(hashlib.sha256(forwarded['body'].encode()).digest(),
                         hashlib.sha256(raw).digest())
        self.assertEqual((forwarded['client'], forwarded['token']),
                         ('alpha', 'e'*64))
        self.assertIsNone(forwarded['authorization'])
        self.assertEqual(self.stack.request(path='/livez')[0], 200)

    def test_cidr_checks_actual_peer_not_loopback_listener(self):
        self.replace_stack(allowed_client_cidrs=['127.0.0.1/32'])
        auth = 'Authorization: Bearer '+'A'*40+'\r\n'
        self.assertEqual(self.raw(auth, method='GET', path='/v1/models',
                                  source='127.0.0.2')[0], 403)
        self.assertEqual(self.stack.forwarded, [])
        self.assertEqual(self.raw(auth, method='GET', path='/v1/models')[0], 200)
        self.replace_stack(allowed_client_cidrs=['127.0.0.0/8'])
        self.assertEqual(self.raw(auth, method='GET', path='/v1/models',
                                  source='127.0.0.2')[0], 200)

    def test_data_inflight_limit_preserves_control_capacity(self):
        self.replace_stack(per_client_inflight=1, global_inflight=1)
        connection, response = self.stream()
        try:
            self.assertEqual(self.stack.request(dict(prompt='full'))[0], 429)
            self.assertEqual(self.stack.request()[0], 200)
            self.assertEqual(self.stack.request(path='/livez')[0], 200)
            self.assertEqual(len(self.stack.requests), 1)
        finally:
            response.close()
            connection.close()

    def test_downstream_disconnect_closes_upstream_without_replay(self):
        self.ready()
        connection, response = self.stream()
        response.close()
        connection.close()
        self.assertTrue(self.stack.stream_disconnected.wait(2))
        self.assertEqual(len(self.stack.requests), 1)
        self.assertEqual(self.stack.request(dict(prompt='after cancel'))[0], 200)

    def test_malformed_response_is_rejected_without_replaying_post(self):
        self.ready()
        self.assertEqual(self.stack.request(dict(malformed_response=True))[0], 503)
        self.assertEqual(len(self.stack.requests), 1)

    def test_health_distinguishes_entrypoint_liveness_from_business_readiness(self):
        self.ready()
        self.stack.ready = False
        self.assertEqual(self.stack.request(path='/livez')[0], 200)
        self.assertEqual(self.stack.request(path='/readyz')[0], 503)
        self.assertEqual(self.stack.request(path='/livez', key='wrong')[0], 401)
        self.assertEqual(self.stack.forwarded, [])

    def test_untrusted_forwarded_headers_do_not_change_identity(self):
        self.ready()
        result = self.stack.request(dict(prompt='untrusted'), extra={
            'Forwarded': 'for=192.0.2.1;host=forged',
            'X-Forwarded-For': '192.0.2.2', 'X-Q4T-Admission-Secret': 'forged'})
        self.assertEqual(result[0], 200)
        self.assertEqual(self.stack.forwarded[-1]['client'], 'alpha')
        self.assertIsNone(self.stack.forwarded[-1]['authorization'])

    def test_native_child_exit_stops_owned_worker_without_stopping_backend(self):
        self.ready()
        owner = fixture.supervisor.process_identity(os.getpid())
        children = fixture.children(self.stack.process.pid)
        native = next(pid for pid in children
                      if Path(f'/proc/{pid}/exe').resolve() == INGRESS)
        fixture.signal_identity(native, journal.process_start(native), signal.SIGKILL)
        self.stack.process.wait(timeout=4)
        self.assertEqual(fixture.supervisor.process_identity(os.getpid()), owner)
        connection = http.client.HTTPConnection('127.0.0.1',
            self.stack.servers[0].server_port, timeout=2)
        try:
            connection.request('GET', '/readyz')
            self.assertEqual(connection.getresponse().status, 200)
        finally:
            connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--ingress', type=Path, required=True)
    args, rest = parser.parse_known_args()
    INGRESS = args.ingress.resolve(strict=True)
    unittest.main(argv=[sys.argv[0], *rest])
