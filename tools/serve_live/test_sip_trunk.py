"""Exercise the isolated SIP worker with an independent TLS/SIP peer."""
import contextlib
import os
from pathlib import Path
import secrets
import select
import socket
import ssl
import subprocess
import tempfile
import time
import unittest

from tools.serve_gateway import tls_fixture

ROOT = Path(__file__).resolve().parents[2]
BINARY = os.environ.get('Q4T_SIP_WORKER')
PEER = 'carrier.test'
SCOPE = 'customer-a'


class Peer:
    def __init__(self, sock, target):
        self.sock = sock
        self.reader = sock.makefile('rb')
        self.target = target

    def send(self, method, token, *, to_tag='', extra=b'', body=b'',
             fragmented=False, branch=None, seq=1, target=None):
        to = f'<sips:other-customer@{self.target}>'
        if to_tag:
            to += ';tag=' + to_tag
        fields = [
            f'{method} {target or ("sips:untrusted@" + self.target)} SIP/2.0',
            f'Via: SIP/2.0/TLS 127.0.0.1:9;branch=z9hG4bK{branch or token};rport',
            f'From: <sip:other-customer@forged.test>;tag={token}',
            f'To: {to}', f'Call-ID: {token}@peer.test', f'CSeq: {seq} {method}',
            'Max-Forwards: 70', 'Contact: <sips:phone@127.0.0.1:9>',
        ]
        wire_headers = (('\r\n'.join(fields) + '\r\n').encode() + extra +
                        f'Content-Length: {len(body)}\r\n'.encode())
        if fragmented:
            split = len(wire_headers) // 2
            self.sock.sendall(wire_headers[:split])
            self.sock.sendall(wire_headers[split:] + b'\r\n')
            self.sock.sendall(body)
        else:
            self.sock.sendall(wire_headers + b'\r\n' + body)
        return wire_headers

    def dialog_request(self, method, token, tag):
        start = self.reader.readline(4096).split()
        if start != [method.encode(), b'sips:phone@127.0.0.1:9', b'SIP/2.0']:
            raise AssertionError(('unexpected in-dialog request', start))
        fields = {}
        while True:
            line = self.reader.readline(16384)
            if line == b'\r\n':
                break
            if not line.endswith(b'\r\n') or len(fields) >= 64:
                raise AssertionError('incomplete or oversized in-dialog request')
            name, value = line.rstrip().split(b':', 1)
            fields[name.lower()] = value.strip()
        number, verb = fields[b'cseq'].split()
        if (fields[b'call-id'] != (token + '@peer.test').encode() or
                (';tag=' + tag).encode() not in fields[b'from'] or
                (';tag=' + token).encode() not in fields[b'to'] or
                verb != method.encode() or int(number) < 1 or
                fields.get(b'content-length', b'0') != b'0'):
            raise AssertionError('unexpected dialog identifiers or body')
        return fields

    def reply_dialog(self, fields, status):
        lines = [f'SIP/2.0 {status} Carrier response'.encode()] + [
            name + b': ' + fields[name] for name in
            (b'via', b'from', b'to', b'call-id', b'cseq')]
        self.sock.sendall(b'\r\n'.join(lines + [b'Content-Length: 0', b'', b'']))

    def transfer_notify(self, token, tag, target, seq, event, state, fragment,
                        expected=200, content_type='message/sipfrag'):
        branch = secrets.token_hex(12)
        headers = (f'Event: {event}\r\nSubscription-State: {state}\r\n'
                   f'Content-Type: {content_type}\r\n').encode()
        self.send('NOTIFY', token, to_tag=tag, target=target, branch=branch, seq=seq,
                  extra=headers, body=fragment.encode())
        self.response(expected, 'NOTIFY', token, branch=branch, seq=seq)

    def response(self, status, method, token, *, branch=None, seq=1):
        while True:
            line = self.reader.readline(4096)
            if not line:
                raise EOFError('SIP peer closed before reply')
            fields = {}
            while True:
                value = self.reader.readline(8192)
                if value == b'\r\n':
                    break
                if not value:
                    raise EOFError('incomplete reply')
                key, value = value.decode().strip().split(':', 1)
                fields[key.lower()] = value.strip()
            size = int(fields.get('content-length', '0'))
            self.body = self.reader.read(size) if size else b''
            self.headers = fields
            received = int(line.split()[1])
            if received == 100:
                continue
            if (received not in (status if isinstance(status, tuple) else (status,)) or
                    fields['cseq'] != f'{seq} {method}' or fields['call-id'] != token + '@peer.test'):
                raise AssertionError((line, fields, status, method, token))
            if ('branch=z9hG4bK' + (branch or token)) not in fields['via']:
                raise AssertionError('transaction branch changed')
            self.status = received
            return fields['to'].split(';tag=', 1)[-1]


class TrunkFixture:
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.ca = tls_fixture.ca(cls.root / 'ca')
        other_ca = tls_fixture.ca(cls.root / 'other-ca')
        now = int(time.time())
        def issue(name, issuer, purpose, host):
            return tls_fixture.issue(cls.root / name, name, issuer, now-120,
                                     now+86400, purpose=purpose, host=host)
        cert, key = issue('server', cls.ca, 'serverAuth', 'server.test')
        cls.pki = cls.root / 'listener'
        cls.pki.mkdir(mode=0o700)
        (cls.pki / 'agent.pem').write_bytes(cert.read_bytes()+key.read_bytes())
        (cls.pki / 'agent.pem').chmod(0o600)
        (cls.pki / 'cafile.pem').write_bytes(cls.ca[0].read_bytes())
        cls.good = issue('allowed', cls.ca, 'clientAuth', PEER)
        cls.wrong = issue('wrong', cls.ca, 'clientAuth', 'someone-else.test')
        cls.alien = issue('alien', other_ca, 'clientAuth', PEER)

    @contextlib.contextmanager
    def publisher(self):
        yield None

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        control, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.control = self.stack.enter_context(control)
        self.control.settimeout(3)
        self.log = self.stack.enter_context(tempfile.TemporaryFile())
        with child, self.publisher() as publisher:
            optional = [] if publisher is None else [publisher.fileno()]
            self.proc = subprocess.Popen(
                [BINARY, str(child.fileno()), '127.0.0.1', '0',
                 str(self.pki), PEER, SCOPE, *map(str, optional)],
                pass_fds=(child.fileno(), *optional),
                stderr=self.log, stdout=subprocess.DEVNULL)
        self.stack.callback(self.stop)
        ready = self.event()
        self.assertEqual(ready[0], 'READY')
        self.port = int(ready[1])

    def stop(self):
        if self.proc.poll() is None:
            self.control.send(b'STOP')
            try:
                self.proc.wait(3)
            finally:
                if self.proc.poll() is None:
                    self.proc.kill()
                    self.proc.wait(3)
            self.assertEqual(self.proc.returncode, 0)

    def event(self):
        packet = self.control.recv(70000)
        if not packet:
            self.log.seek(0)
            self.fail('worker stopped: ' + self.log.read(2048).decode())
        line, _, self.payload = packet.partition(b'\n')
        return line.decode().split()

    def quiet(self):
        self.assertFalse(select.select([self.control], [], [], .12)[0])

    def peer(self, cert):
        ctx = ssl.create_default_context(cafile=self.ca[0])
        if cert:
            ctx.load_cert_chain(*cert)
        raw = self.stack.enter_context(socket.create_connection(
            ('127.0.0.1', self.port), timeout=3))
        tls = self.stack.enter_context(ctx.wrap_socket(raw, server_hostname='server.test'))
        peer = Peer(tls, f'127.0.0.1:{self.port}')
        self.stack.callback(peer.reader.close)
        return peer

    def invite(self, peer):
        token = secrets.token_hex(8)
        peer.send('INVITE', token)
        tag = peer.response(180, 'INVITE', token)
        self.assertTrue(tag and '<' not in tag)
        event = self.event()
        self.assertEqual(event[:2], ['INCOMING', SCOPE])
        self.assertRegex(event[2], r'^live_[0-9a-f]{32}$')
        return token, tag, event[2]

    def reject(self, scope, live, code, expected):
        self.control.send(f'REJECT op {scope} {live} {code}'.encode())
        if expected == 'submitted':
            self.assertEqual(self.event(), ['FINAL', SCOPE, live, str(code)])
        self.assertEqual(self.event(), ['RESULT', 'op', expected])

@unittest.skipUnless(BINARY, 'set Q4T_SIP_WORKER to the built telephone worker')
class TrunkTest(TrunkFixture, unittest.TestCase):
    def test_authenticated_reject_duplicate_ack_and_scope(self):
        peer = self.peer(self.good)
        token, tag, live = self.invite(peer)
        peer.send('INVITE', token)
        self.assertEqual(peer.response(180, 'INVITE', token), tag)
        self.quiet()
        self.reject('other-customer', live, 603, 'not_found')
        self.reject(SCOPE, live, 200, 'invalid_status')
        self.reject(SCOPE, live, 486, 'submitted')
        self.assertEqual(peer.response(486, 'INVITE', token), tag)
        self.reject(SCOPE, live, 603, 'conflict')
        peer.send('INVITE', token)
        self.assertEqual(peer.response(486, 'INVITE', token), tag)
        peer.send('ACK', token, to_tag=tag)
        self.assertEqual(self.event(), ['ACK', SCOPE, live, '486'])
        self.reject(SCOPE, live, 486, 'not_found')

    def test_cancel_one_of_two_calls(self):
        peer = self.peer(self.good)
        a, tag_a, first = self.invite(peer)
        b, tag_b, second = self.invite(peer)
        self.assertNotEqual(first, second)
        bad = secrets.token_hex(8)
        peer.send('CANCEL', bad)
        peer.response(481, 'CANCEL', bad)
        self.quiet()
        peer.send('CANCEL', a)
        self.assertEqual(peer.response(200, 'CANCEL', a), tag_a)
        self.assertEqual(peer.response(487, 'INVITE', a), tag_a)
        self.assertEqual(self.event(), ['FINAL', SCOPE, first, '487'])
        peer.send('ACK', a, to_tag=tag_a)
        self.assertEqual(self.event(), ['ACK', SCOPE, first, '487'])
        self.reject(SCOPE, second, 603, 'submitted')
        peer.response(603, 'INVITE', b)
        peer.send('ACK', b, to_tag=tag_b)
        self.assertEqual(self.event(), ['ACK', SCOPE, second, '603'])

    def test_original_invite_headers_and_private_offer(self):
        peer = self.peer(self.good)
        token = secrets.token_hex(8)
        body = b'v=0\r\ns=sip-media-private\r\nt=0 0\r\n'
        auth = b'aUtHoRiZaTiOn: Digest username="first",\r\n realm="private"\r\n'
        proxy = b'pRoXy-AuThOrIzAtIoN : Digest username="second"\r\n'
        extra = (b'X-Route: one\r\n' + auth + b'x-route: two\r\n' + proxy +
                 b'X-Long: keep\r\n folded\r\nContent-Type: application/sdp\r\n')
        original = peer.send('INVITE', token, extra=extra, body=body, fragmented=True)
        peer.response(180, 'INVITE', token)
        event = self.event()
        self.assertEqual(event[:2], ['INCOMING', SCOPE])
        live = event[2]
        expected = original.split(b'\r\n', 1)[1].replace(auth, b'').replace(proxy, b'')
        self.assertEqual(self.payload, expected)
        self.control.send(f'OFFER a other-customer {live}'.encode())
        self.assertEqual(self.event(), ['RESULT', 'a', 'not_found'])
        self.control.send(f'OFFER b {SCOPE} {live}'.encode())
        self.assertEqual(self.event(), ['OFFER', 'b'])
        self.assertEqual(self.payload, body)
        self.reject(SCOPE, live, 486, 'submitted')
        self.control.send(f'OFFER c {SCOPE} {live}'.encode())
        self.assertEqual(self.event(), ['RESULT', 'c', 'conflict'])

    def test_slow_control_reader_preserves_previous_calls(self):
        peer = self.peer(self.good)
        padding = (b'X-Padding: ' + b'x'*1024 + b'\r\n') * 32
        calls = []
        for _ in range(64):
            token = secrets.token_hex(8)
            peer.send('INVITE', token, extra=padding)
            tag = peer.response((180, 503), 'INVITE', token)
            if peer.status == 503:
                break
            calls.append((token, tag))
        self.assertGreater(len(calls), 1)
        self.assertLess(len(calls), 64)
        identities = []
        for token, _ in calls:
            event = self.event()
            self.assertEqual(event[:2], ['INCOMING', SCOPE])
            self.assertIn(f'Call-ID: {token}@peer.test'.encode(), self.payload)
            identities.append(event[2])
        self.reject(SCOPE, identities[0], 603, 'submitted')
        token, tag = calls[0]
        self.assertEqual(peer.response(603, 'INVITE', token), tag)
        peer.send('ACK', token, to_tag=tag)
        self.assertEqual(self.event(), ['ACK', SCOPE, identities[0], '603'])
        self.invite(peer)

    def test_offerless_and_wrong_content_type(self):
        peer = self.peer(self.good)
        _, _, live = self.invite(peer)
        self.control.send(f'OFFER a {SCOPE} {live}'.encode())
        self.assertEqual(self.event(), ['OFFER', 'a'])
        self.assertEqual(self.payload, b'')
        token = secrets.token_hex(8)
        peer.send('INVITE', token, extra=b'Content-Type: text/plain\r\n', body=b'other')
        peer.response(415, 'INVITE', token)
        self.quiet()

    def test_answer_deadline_and_slot_release(self):
        peer = self.peer(self.good)
        token, tag, live = self.invite(peer)
        peer.sock.settimeout(35)
        self.assertEqual(peer.response(480, 'INVITE', token), tag)
        self.assertEqual(self.event(), ['FINAL', SCOPE, live, '480'])
        peer.send('ACK', token, to_tag=tag)
        self.assertEqual(self.event(), ['ACK', SCOPE, live, '480'])
        self.reject(SCOPE, live, 603, 'not_found')

    def test_owner_loss_ends_pending_invitation(self):
        peer = self.peer(self.good)
        token, tag, _ = self.invite(peer)
        self.control.close()
        self.assertEqual(peer.response(503, 'INVITE', token), tag)
        self.assertEqual(self.proc.wait(3), 1)

    def test_bad_credentials_fail_before_ready(self):
        with tempfile.TemporaryDirectory(dir=self.root) as bad:
            pki = Path(bad)
            (pki / 'cafile.pem').write_bytes(self.ca[0].read_bytes())
            (pki / 'agent.pem').write_bytes(
                (self.root / 'server/server.pem').read_bytes() +
                self.wrong[1].read_bytes())
            control, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            with control, child:
                result = subprocess.run(
                    [BINARY, str(child.fileno()), '127.0.0.1', '0',
                     bad, PEER, SCOPE], pass_fds=(child.fileno(),),
                    capture_output=True, timeout=3)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(select.select([control], [], [], 0)[0])

    def test_untrusted_trunks_cannot_publish(self):
        for cert in (None, self.wrong, self.alien):
            with self.subTest(certificate=cert and cert[0].stem):
                with self.assertRaises((ssl.SSLError, ConnectionError, EOFError)):
                    peer = self.peer(cert)
                    token = secrets.token_hex(8)
                    peer.send('INVITE', token)
                    peer.response(180, 'INVITE', token)
                self.quiet()
        self.invite(self.peer(self.good))


if __name__ == '__main__':
    unittest.main()
