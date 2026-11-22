"""Real native HTTP/TLS upgrades with explicit CPU WebSocket backend peers."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools.serve_recovery import test_gateway_nginx as fixture


KEY = 'dGhlIHNhbXBsZSBub25jZQ=='
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def frame(data, opcode=1, masked=False):
    assert len(data) < 126
    if not masked:
        return bytes([0x80 | opcode, len(data)]) + data
    mask = bytes([1, 2, 3, 4])
    return bytes([0x80 | opcode, 0x80 | len(data)]) + mask + bytes(
        value ^ mask[index % 4] for index, value in enumerate(data))


def read_frame(read):
    head = read(2)
    if not head:
        return None
    if len(head) != 2 or head[1] & 127 >= 126:
        raise ValueError('CPU peer only accepts short complete frames')
    size = head[1] & 127
    mask = read(4) if head[1] & 128 else None
    payload = read(size)
    if len(payload) != size:
        raise EOFError('frame payload')
    if mask:
        payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
    return head[0] & 15, payload


class Wire:
    def __init__(self, sock):
        self.sock, self.pending = sock, b''

    def read(self, count):
        while len(self.pending) < count:
            part = self.sock.recv(65536)
            if not part:
                break
            self.pending += part
        result, self.pending = self.pending[:count], self.pending[count:]
        return result

    def head(self):
        while b'\r\n\r\n' not in self.pending:
            part = self.sock.recv(65536)
            if not part or len(self.pending) > 65536:
                raise EOFError('HTTP upgrade')
            self.pending += part
        head, self.pending = self.pending.split(b'\r\n\r\n', 1)
        lines = head.decode('ascii').split('\r\n')
        return int(lines[0].split()[1]), dict(
            (name.lower(), value.strip())
            for name, value in (line.split(':', 1) for line in lines[1:]))


class NativeUpgrade(unittest.TestCase):
    setUpClass = classmethod(fixture.NginxTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.NginxTests.tearDownClass.__func__)
    tearDown = fixture.NginxTests.tearDown
    ready = fixture.NginxTests.ready

    def setUp(self):
        self.binary = Path(os.environ['Q4T_INGRESS']).resolve(strict=True)
        self.rows = []
        self.bad = ''
        self.eof = threading.Event()
        self.stack = self.make_stack()
        self.install_peer()

    def make_stack(self, **kwargs):
        return fixture.Stack(self.output, native_ingress=self.binary, **kwargs)

    def install_peer(self):
        owner = self
        handler = self.stack.servers[0].RequestHandlerClass
        original = handler.do_GET
        def get(peer):
            if peer.path.split('?', 1)[0] not in ('/v1/realtime', '/v1/live/sessions',
                                                  '/v1/live/sessions/live_cpu/attach'):
                return original(peer)
            owner.rows.append(dict(path=peer.path, headers=dict(peer.headers)))
            peer.close_connection = True
            if owner.bad == 'finite':
                peer.response(429, dict(error=dict(code='cpu_quota')))
                return
            if owner.bad == 'status':
                peer.response(200, dict(cpu_fixture=True))
                return
            accept = base64.b64encode(hashlib.sha1(
                (peer.headers['Sec-WebSocket-Key'] + GUID).encode()).digest()).decode()
            if owner.bad == 'challenge':
                accept = 'wrong'
            headers = ['HTTP/1.1 101 Switching Protocols', 'Connection: Upgrade',
                       'Upgrade: websocket', 'Sec-WebSocket-Accept: ' + accept]
            protocol = peer.headers.get('Sec-WebSocket-Protocol')
            if protocol:
                headers.append('Sec-WebSocket-Protocol: ' + protocol)
            additions = {
                'duplicate': 'Sec-WebSocket-Accept: '+accept,
                'length': 'Content-Length: 0',
                'extension': 'Sec-WebSocket-Extensions: permessage-deflate',
                'protocol': 'Sec-WebSocket-Protocol: credential-leak',
                'hop': 'Connection: Sec-WebSocket-Accept',
            }
            if owner.bad in additions:
                headers.append(additions[owner.bad])
            # The first server frame deliberately shares the 101 write.
            peer.wfile.write(('\r\n'.join(headers)+'\r\n\r\n').encode() + frame(b'cpu-ready'))
            peer.wfile.flush()
            if owner.bad:
                return
            peer.connection.settimeout(4)
            try:
                while True:
                    event = read_frame(peer.rfile.read)
                    if event is None:
                        break
                    opcode, payload = event
                    peer.wfile.write(frame(payload, opcode=opcode))
                    peer.wfile.flush()
                    if opcode == 8:
                        break
            except (OSError, EOFError, ValueError):
                pass
            finally:
                owner.eof.set()
        handler.do_GET = get

    def connect(self, *, key='A'*40, path='/v1/realtime?intent=transcription',
                fields='', version='13', ws_key=KEY, framing='', prefix=b'',
                context=None):
        raw = socket.create_connection(('127.0.0.1',
            self.stack.value['policy']['listen_port']), timeout=3)
        if context:
            raw = context.wrap_socket(raw, server_hostname='127.0.0.1')
        self.addCleanup(raw.close)
        auth = '' if key is None else 'Authorization: Bearer '+key+'\r\n'
        wire = ('GET '+path+' HTTP/1.1\r\nHost: 127.0.0.1\r\n'
            'Connection: Upgrade\r\nUpgrade: websocket\r\n'
            'Sec-WebSocket-Key: '+ws_key+'\r\nSec-WebSocket-Version: '+version+
            '\r\n'+auth+fields+framing+'\r\n').encode()+prefix
        raw.sendall(wire)
        client = Wire(raw)
        status, headers = client.head()
        return client, status, headers

    def close_session(self, client):
        self.assertEqual(read_frame(client.read), (1, b'cpu-ready'))
        client.sock.sendall(frame(b'\x03\xe8', opcode=8, masked=True))
        self.assertEqual(read_frame(client.read), (8, b'\x03\xe8'))
        client.sock.close()

    def test_upgrade_preserves_both_coalesced_first_frames_and_internal_owner(self):
        self.ready()
        client, status, headers = self.connect(prefix=frame(b'first', masked=True))
        self.assertEqual(status, 101)
        self.assertEqual(headers['sec-websocket-accept'], 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=')
        self.assertEqual(read_frame(client.read), (1, b'cpu-ready'))
        self.assertEqual(read_frame(client.read), (1, b'first'))
        row = self.rows[0]
        self.assertEqual(row['path'], '/v1/realtime?intent=transcription')
        self.assertEqual(row['headers']['X-Q4T-Client-Id'], 'alpha')
        self.assertEqual(row['headers']['X-Q4T-Gateway-Token'], 'e'*64)
        self.assertNotIn('Authorization', row['headers'])
        client.sock.sendall(frame(b'\x03\xe8', opcode=8, masked=True))
        self.assertEqual(read_frame(client.read), (8, b'\x03\xe8'))

    def test_authentication_origin_body_and_version_fail_before_backend(self):
        self.ready()
        cases = [dict(key='wrong'), dict(ws_key='bad'), dict(version='12'),
            dict(fields='Sec-WebSocket-Key: '+KEY+'\r\n'),
            dict(framing='Content-Length: 1\r\nExpect: 100-continue\r\n'),
            dict(framing='Transfer-Encoding: chunked\r\n'),
            dict(fields='Origin: https://untrusted.example\r\n')]
        for options, expected in zip(cases, (401, 400, 426, 400, 400, 400, 403)):
            client, status, headers = self.connect(**options)
            self.assertEqual(status, expected)
            if expected == 426:
                self.assertEqual(headers['sec-websocket-version'], '13')
            client.sock.close()
        self.assertEqual(self.rows, [])

    def test_browser_subprotocol_auth_keeps_case_and_never_echoes_key(self):
        self.stack.close()
        cors = dict(allowed_origins=['https://client.example'],
            allowed_methods=['GET'], allowed_headers=[], exposed_headers=[],
            allow_credentials=True, max_age=60)
        self.stack = self.make_stack(policy_overrides=dict(version=3, cors=cors))
        self.install_peer()
        self.ready()
        key = 'A'*40
        fields = ('Origin: https://client.example\r\n'
                  'Sec-WebSocket-Protocol: realtime, openai-insecure-api-key.'+key+'\r\n')
        client, status, headers = self.connect(key=None, fields=fields)
        self.assertEqual(status, 101)
        self.assertEqual(headers['sec-websocket-protocol'], 'realtime')
        self.assertEqual(headers['access-control-allow-origin'],
                         'https://client.example')
        self.assertNotIn(key, json.dumps(headers))
        self.assertNotIn(key, json.dumps(self.rows))
        self.assertEqual(self.rows[0]['headers']['Sec-WebSocket-Protocol'], 'realtime')
        self.close_session(client)
        _, status, _ = self.connect(key=key, fields=fields)
        self.assertEqual(status, 401)
        _, status, _ = self.connect(key=None, fields=fields.replace(key, key.lower()))
        self.assertEqual(status, 401)

    def test_invalid_upstream_101_never_publishes_upgrade(self):
        self.ready()
        for mode in ('challenge', 'duplicate', 'length', 'extension', 'protocol', 'hop', 'status'):
            self.bad = mode
            client, status, headers = self.connect()
            self.assertEqual(status, 503, mode)
            self.assertNotIn('sec-websocket-accept', headers)
            body = client.read(int(headers['content-length']))
            self.assertNotIn(b'cpu-ready', body)
            client.sock.close()

    def test_finite_upstream_error_retains_http_body_without_replay(self):
        self.ready()
        self.bad = 'finite'
        client, status, headers = self.connect()
        self.assertEqual(status, 429)
        body = client.read(int(headers['content-length']))
        self.assertEqual(json.loads(body)['error']['code'], 'cpu_quota')
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(self.stack.request(dict(prompt='ordinary HTTP'))[0], 200)

    def test_session_capacity_is_separate_from_generation_and_other_client(self):
        self.stack.close()
        self.stack = self.make_stack(policy_overrides=dict(
            per_client_inflight=1, global_inflight=1))
        self.install_peer()
        self.ready()
        first, status, _ = self.connect()
        self.assertEqual(status, 101)
        _, status, _ = self.connect()
        self.assertEqual(status, 429)
        second, status, _ = self.connect(key='B'*40)
        self.assertEqual(status, 101)
        self.assertEqual(self.stack.request(dict(prompt='ordinary HTTP'))[0], 200)
        self.close_session(first)
        self.close_session(second)

    def test_live_attachment_has_independent_capacity_and_selected_protocol(self):
        self.ready()
        primary, status, _ = self.connect(path='/v1/live/sessions')
        self.assertEqual(status, 101)
        peers = []
        for expected in (101, 101, 429):
            observer, status, headers = self.connect(path='/v1/live/sessions/live_cpu/attach',
                fields='Sec-WebSocket-Protocol: live\r\n')
            self.assertEqual(status, expected)
            if status == 101:
                self.assertEqual(headers['sec-websocket-protocol'], 'live')
                peers.append(observer)
        for observer in peers:
            self.close_session(observer)
        self.close_session(primary)

    def test_binding_rotation_closes_existing_session_without_reconnect(self):
        self.ready()
        client, status, _ = self.connect()
        self.assertEqual(status, 101)
        self.assertEqual(read_frame(client.read), (1, b'cpu-ready'))
        self.stack.token = 'f'*64
        self.assertEqual(client.read(1), b'')
        self.assertTrue(self.eof.wait(2))
        self.assertEqual(len(self.rows), 1)

    def test_real_tls_live_upgrade_and_binary_frame(self):
        self.stack.close()
        directory = Path(tempfile.mkdtemp(prefix='upgrade-tls-', dir=self.output))
        certificate, key = directory/'cert.pem', directory/'key.pem'
        with (directory/'openssl.log').open('wb') as log:
            subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt',
                'ec_paramgen_curve:P-256','-nodes','-days','2','-subj','/CN=127.0.0.1',
                '-addext','subjectAltName=IP:127.0.0.1','-addext','basicConstraints=critical,CA:FALSE',
                '-addext','extendedKeyUsage=serverAuth','-keyout',str(key),'-out',str(certificate)],
                check=True,timeout=15,stdout=log,stderr=log)
        key.chmod(0o600)
        certificate.chmod(0o644)
        context = ssl.create_default_context(cafile=str(certificate))
        context.set_alpn_protocols(['http/1.1'])
        self.stack = self.make_stack(tls=dict(certificate=str(certificate),key=str(key),
            ca='', require_client_certificate=False), ssl_context=context)
        self.install_peer()
        self.ready()
        client, status, headers = self.connect(path='/v1/live/sessions',context=context,
            fields='Sec-WebSocket-Protocol: live\r\n')
        self.assertEqual(status,101)
        self.assertEqual(headers['sec-websocket-protocol'],'live')
        self.assertEqual(read_frame(client.read),(1,b'cpu-ready'))
        client.sock.sendall(frame(b'\x00\xff\x01',opcode=2,masked=True))
        self.assertEqual(read_frame(client.read),(2,b'\x00\xff\x01'))
        client.sock.sendall(frame(b'\x03\xe8',opcode=8,masked=True))
        self.assertEqual(read_frame(client.read),(8,b'\x03\xe8'))


class OptionalNginxUpgrade(unittest.TestCase):
    setUpClass = classmethod(fixture.NginxTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.NginxTests.tearDownClass.__func__)
    ready = fixture.NginxTests.ready
    tearDown = fixture.NginxTests.tearDown
    connect = NativeUpgrade.connect
    install_peer = NativeUpgrade.install_peer

    def setUp(self):
        self.rows, self.bad, self.eof = [], '', threading.Event()
        from tools.serve_gateway.test_gateway import VENDOR
        self.patch = mock.patch.object(fixture, 'NGINX', Path(json.loads(VENDOR.read_text())['binary']))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.stack = fixture.Stack(self.output)
        self.install_peer()

    def test_scoped_primary_upgrades_relay_commands_events_and_normal_close(self):
        self.ready()
        for path in ('/v1/realtime?intent=transcription','/v1/realtime','/v1/live/sessions',
                     '/v1/live/sessions/live_cpu/attach?graceful_close=true'):
            protocol = 'live' if path.startswith('/v1/live/') else 'realtime'
            fields = ('Sec-WebSocket-Protocol: live, realtime, '
                      'openai-insecure-api-key.test-only\r\n')
            client, status, head = self.connect(path=path, fields=fields)
            self.assertEqual(status,101)
            self.assertEqual(head['sec-websocket-accept'],'s3pPLMBiTxaQ9kYGzzhZRbK+xOo=')
            self.assertEqual(head['sec-websocket-protocol'], protocol)
            self.assertEqual(read_frame(client.read),(1,b'cpu-ready'))
            client.sock.sendall(frame(b'session control',masked=True))
            self.assertEqual(read_frame(client.read),(1,b'session control'))
            client.sock.sendall(frame(bytes([3,232]),opcode=8,masked=True))
            self.assertEqual(read_frame(client.read),(8,bytes([3,232])))
            client.sock.close()
            headers = {k.lower():v for k,v in self.rows[-1]['headers'].items()}
            self.assertEqual(headers['x-q4t-client-id'],'alpha')
            self.assertEqual(headers['x-q4t-gateway-token'],self.stack.token)
            self.assertEqual(headers['sec-websocket-protocol'], protocol)
            self.assertTrue(all('test-only' not in value for value in headers.values()))
            self.assertNotIn('authorization', headers)


if __name__ == '__main__':
    unittest.main()
