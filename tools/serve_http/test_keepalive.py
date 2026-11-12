"""Actual native ingress with CPU HTTP peers; never loads a model or GPU."""
import http.client
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import time

from tools.serve_recovery import test_gateway_nginx as fixture
from tools.serve_recovery import test_native_access as tls_fixture

INGRESS = Path(os.environ.get('Q4T_HTTP_INGRESS', 'build/q4t-ingress')).resolve()


def request(body=None, *, key='A', version='1.1', close=False, extra=''):
    raw = json.dumps(body or {}).encode()
    return (f'POST /v1/chat/completions HTTP/{version}\r\nHost: 127.0.0.1\r\n'
            f'Authorization: Bearer {key * 40}\r\nContent-Type: application/json\r\n'
            f'Content-Length: {len(raw)}\r\n' +
            ('Connection: close\r\n' if close else '') + extra + '\r\n').encode() + raw


def response(reader):
    status = int(reader.readline().split()[1])
    fields = {}
    while (line := reader.readline()) not in (b'\r\n', b''):
        name, value = line.decode().rstrip('\r\n').split(':', 1)
        fields[name.lower()] = value.strip()
    if status < 200 or status == 204:
        return status, fields, b''
    if fields.get('transfer-encoding') == 'chunked':
        payload = bytearray()
        while True:
            size = int(reader.readline().split(b';')[0].strip(), 16)
            if not size:
                assert reader.readline() == b'\r\n'
                return status, fields, bytes(payload)
            part = reader.read(size)
            assert len(part) == size and reader.read(2) == b'\r\n'
            payload.extend(part)
    return status, fields, reader.read(int(fields['content-length']))


class KeepAliveTests(fixture.NginxTests):
    def setUp(self):
        self.stack = fixture.Stack(self.output, native_ingress=INGRESS,
            policy_overrides=dict(per_client_inflight=1, global_inflight=2))
        self.stack.response_hook = self.hook
        self.ready()

    def connection(self):
        port = self.stack.value['policy']['listen_port']
        if self.stack.ssl_context:
            return http.client.HTTPSConnection('127.0.0.1', port, timeout=7,
                                               context=self.stack.ssl_context)
        return http.client.HTTPConnection('127.0.0.1', port, timeout=7)

    def socket(self):
        conn = self.connection()
        conn.connect()
        sock, conn.sock = conn.sock, None
        return sock

    def post(self, conn, body, key='A', **fields):
        conn.request('POST', '/v1/chat/completions', json.dumps(body),
            {'Authorization': 'Bearer '+key*40, 'Content-Type': 'application/json', **fields})
        reply = conn.getresponse()
        return reply.status, dict(reply.getheaders()), reply.read()

    def hook(self, handler, body, generation):
        mode = body.get('framing')
        if not mode:
            return False
        if mode == 'mixed':
            handler.connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n'
                b'Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n')
            return True
        handler.connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
            b'Transfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n')
        payload = f'data: {{"generation":{generation}}}\r\n\r\n'.encode()
        handler.connection.sendall(f'{len(payload):x};part="one"\r\n'.encode()+payload+b'\r\n')
        self.stack.stream_started.set()
        if mode == 'delayed':
            self.stack.release_stream.wait(5)
        if mode == 'truncated':
            handler.connection.sendall(b'9\r\nbad')
            return True
        payload = b'data: [DONE]\r\n\r\n'
        handler.connection.sendall(f'{len(payload):x}\r\n'.encode()+payload+
                                   b'\r\n0\r\nDigest: fixture\r\n\r\n')
        return True

    def test_sequential_reauth_trace_and_upstream_error(self):
        with closing(self.connection()) as conn:
            first = self.post(conn, {'prompt': 'one'}, **{'X-Request-Id': 'one'})
            original = conn.sock
            second = self.post(conn, {'quota': True}, key='B', **{'X-Request-Id': 'two'})
            self.assertIs(conn.sock, original)
            third = self.post(conn, {'prompt': 'three'})
            self.assertIs(conn.sock, original)
            self.assertEqual([r[0] for r in (first, second, third)], [200, 429, 200])
            self.assertEqual([r[1]['X-Request-Id'] for r in (first, second)], ['one', 'two'])
            self.assertNotIn(third[1]['X-Request-Id'], ('one', 'two'))
            self.assertEqual([r['client'] for r in self.stack.forwarded], ['alpha', 'beta', 'alpha'])
            self.assertEqual([r['authorization'] for r in self.stack.forwarded], [None]*3)
            self.assertEqual(self.post(conn, {}, key='Z')[0], 401)
            self.assertEqual(len(self.stack.forwarded), 3)

    def test_chunked_upload_then_length_request(self):
        with closing(self.connection()) as conn:
            parts = [b'{"prompt":', b'"chunked"', b'}']
            conn.request('POST', '/v1/chat/completions', iter(parts),
                {'Authorization': 'Bearer '+'A'*40, 'Content-Type': 'application/json'},
                encode_chunked=True)
            reply = conn.getresponse()
            self.assertEqual(reply.status, 200); reply.read()
            original = conn.sock
            self.assertEqual(self.post(conn, {'prompt': 'length'})[0], 200)
            self.assertIs(conn.sock, original)
            self.assertEqual(self.stack.forwarded[0]['body'].encode(), b''.join(parts))

    def test_continue_and_same_connection_reuse(self):
        with self.socket() as sock, sock.makefile('rb') as reader:
            head = (b'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                b'Authorization: Bearer '+b'A'*40+b'\r\nContent-Type: application/json\r\n'
                b'Expect: 100-continue\r\nTransfer-Encoding: chunked\r\n\r\n')
            sock.sendall(head)
            self.assertEqual(response(reader)[0], 100)
            self.assertEqual(self.stack.forwarded, [])
            sock.sendall(b'2\r\n{}\r\n0\r\n\r\n')
            self.assertEqual(response(reader)[0], 200)
            sock.sendall(request({'next': True}, key='B', close=True))
            self.assertEqual(response(reader)[0], 200)
            self.assertEqual(reader.read(), b'')
            self.assertEqual([r['client'] for r in self.stack.forwarded], ['alpha', 'beta'])

    def test_continue_auth_or_length_denial_is_final(self):
        for key, length, status in [('Z', 2, 401), ('A', 16777217, 413)]:
            with self.socket() as sock, sock.makefile('rb') as reader:
                sock.sendall((f'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                    f'Authorization: Bearer {key*40}\r\nContent-Length: {length}\r\n'
                    'Expect: 100-continue\r\n\r\n').encode())
                self.assertEqual(response(reader)[0], status)
                self.assertEqual(reader.read(), b'')
        self.assertEqual(self.stack.forwarded, [])

    def test_pipeline_during_stream_changes_only_next_generation(self):
        with self.socket() as sock, sock.makefile('rb') as reader:
            sock.sendall(request({'framing': 'delayed'}))
            self.assertTrue(self.stack.stream_started.wait(2))
            # These bytes arrive after relay begins, exercising cancellation
            # polling with unread pipeline data (also TLS internal buffering).
            sock.sendall(request({'next': True}, key='B', close=True))
            self.stack.generation, self.stack.token, self.stack.bound = 2, 'f'*64, 0
            self.ready()
            self.stack.release_stream.set()
            first = response(reader)
            second = response(reader)
            self.assertEqual((first[0], second[0]), (200, 200))
            self.assertIn(b'"generation":1', first[2])
            self.assertTrue(first[2].endswith(b'data: [DONE]\r\n\r\n'))
            self.assertEqual(json.loads(second[2])['generation'], 2)
            self.assertEqual([r['token'] for r in self.stack.forwarded], ['e'*64, 'f'*64])
            self.assertEqual(reader.read(), b'')

    def test_sse_complete_then_json_reuses_transport(self):
        with closing(self.connection()) as conn:
            first = self.post(conn, {'framing': 'complete'})
            self.assertEqual(first[0], 200)
            self.assertEqual(first[1]['Transfer-Encoding'], 'chunked')
            self.assertTrue(first[2].endswith(b'data: [DONE]\r\n\r\n'))
            original = conn.sock
            self.assertEqual(self.post(conn, {'prompt': 'after'})[0], 200)
            self.assertIs(conn.sock, original)
        self.assertEqual(len(self.stack.requests), 2)

    def test_truncated_sse_has_no_fabricated_end_or_replay(self):
        with closing(self.connection()) as conn:
            with self.assertRaises(http.client.IncompleteRead) as error:
                self.post(conn, {'framing': 'truncated'})
            self.assertNotIn(b'[DONE]', error.exception.partial)
            self.assertNotIn(b'HTTP/1.1', error.exception.partial)
        self.assertEqual(len(self.stack.requests), 1)

    def test_ambiguous_upstream_frame_closes_without_replay(self):
        with closing(self.connection()) as conn:
            reply = self.post(conn, {'framing': 'mixed'})
            self.assertEqual(reply[0], 503)
            self.assertEqual(reply[1]['Connection'], 'close')
        self.assertEqual(len(self.stack.requests), 1)

    def test_explicit_close_does_not_dispatch_pipeline_tail(self):
        with self.socket() as sock, sock.makefile('rb') as reader:
            sock.sendall(request({'first': True}, close=True)+request({'never': True}))
            reply = response(reader)
            self.assertEqual(reply[0], 200)
            self.assertEqual(reply[1]['connection'], 'close')
            self.assertEqual(reader.read(), b'')
        self.assertEqual(len(self.stack.requests), 1)

    def test_request_count_is_bounded(self):
        with closing(self.connection()) as conn:
            original = None
            for number in range(100):
                reply = self.post(conn, {'number': number})
                self.assertEqual(reply[0], 200)
                self.assertEqual(reply[1]['Connection'], 'close' if number == 99 else 'keep-alive')
                if number == 0: original = conn.sock
                elif number < 99: self.assertIs(conn.sock, original)
            self.assertIsNone(conn.sock)
        self.assertEqual(len(self.stack.requests), 100)

    def test_idle_releases_data_capacity_then_closes(self):
        with closing(self.connection()) as conn:
            self.assertEqual(self.post(conn, {})[0], 200)
            sock = conn.sock
            with closing(self.connection()) as other:
                self.assertEqual(self.post(other, {})[0], 200)
            start = time.monotonic()
            self.assertEqual(sock.recv(1), b'')
            self.assertLess(time.monotonic()-start, 6)
        self.assertEqual(len(self.stack.requests), 2)

    def test_http10_decodes_chunked_upstream_and_closes(self):
        with self.socket() as sock:
            sock.sendall(request({'framing': 'complete'}, version='1.0',
                                 extra='Connection: keep-alive\r\n'))
            reply = http.client.HTTPResponse(sock)
            reply.begin(); body = reply.read()
            self.assertEqual(reply.status, 200)
            self.assertIsNone(reply.getheader('Transfer-Encoding'))
            self.assertEqual(reply.getheader('Connection'), 'close')
            self.assertTrue(body.endswith(b'data: [DONE]\r\n\r\n'))


class TlsKeepAliveTests(tls_fixture.AccessTests):
    def setUp(self):
        tls_fixture.INGRESS = INGRESS
        super().setUp()
        self.stack.response_hook = self.hook

    connection = KeepAliveTests.connection
    socket = KeepAliveTests.socket
    post = KeepAliveTests.post
    hook = KeepAliveTests.hook
    test_tls_continue_reuse = KeepAliveTests.test_continue_and_same_connection_reuse
    test_tls_buffered_pipeline = KeepAliveTests.test_pipeline_during_stream_changes_only_next_generation

    def test_tls_preflight_reuses_and_does_not_cache_origin(self):
        with closing(self.connection()) as conn:
            conn.request('OPTIONS', '/v1/chat/completions', headers={
                'Origin': 'https://client.example', 'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': 'authorization, content-type'})
            reply = conn.getresponse()
            self.assertEqual(reply.status, 204); reply.read()
            original = conn.sock
            first = self.post(conn, {})
            self.assertEqual(first[0], 200)
            self.assertIs(conn.sock, original)
            self.assertNotIn('Access-Control-Allow-Origin', first[1])
            last = self.post(conn, {}, Origin='https://denied.example')
            self.assertEqual(last[0], 403)
            self.assertEqual(len(self.stack.requests), 1)
