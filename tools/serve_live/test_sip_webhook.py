"""Actual mTLS SIP -> native publisher -> TLS subscriber with SDK unwrap."""
import base64
import contextlib
import json
import os
import secrets
import select
import socket
import subprocess
import sys
import time
import unittest

from tools.serve_gateway import tls_fixture
from tools.serve_live.test_sip_trunk import BINARY, ROOT, SCOPE, TrunkFixture


WORKER = os.environ.get('Q4T_SIP_WEBHOOK_WORKER')
SDK = os.environ.get('Q4T_OPENAI_SDK_ROOT')


class SubscriberFixture:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        now = int(time.time())
        cls.hook_cert, cls.hook_key = tls_fixture.issue(
            cls.root / 'subscriber', 'subscriber', cls.ca, now-60, now+3600,
            host='localhost')

    def start_subscriber(self, responses):
        self.secret = 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()
        self.peer_server = subprocess.Popen(
            ['node', str(ROOT/'tools/serve_live/sip_webhook_peer.mjs'), SDK],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            stderr=self.log)
        self.stack.callback(self.stop_process, self.peer_server)
        self.peer_server.stdin.write(json.dumps(dict(
            secret=self.secret, cert=str(self.hook_cert), key=str(self.hook_key),
            responses=responses))+'\n')
        self.peer_server.stdin.flush()
        return self.subscriber()['port']

    def stop_process(self, proc):
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(2)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(2)
            for stream in (proc.stdin, proc.stdout):
                if stream:
                    stream.close()

    def subscriber(self):
        self.assertTrue(select.select([self.peer_server.stdout], [], [], 4)[0],
                        'subscriber timed out')
        item = json.loads(self.peer_server.stdout.readline())
        self.assertNotIn('error', item)
        return item


@unittest.skipUnless(BINARY and WORKER and SDK,
                     'set Q4T_SIP_WORKER, Q4T_SIP_WEBHOOK_WORKER and Q4T_OPENAI_SDK_ROOT')
class WebhookTest(SubscriberFixture, TrunkFixture, unittest.TestCase):
    @contextlib.contextmanager
    def publisher(self):
        responses = {
            'test_publish_after_admission': [204],
            'test_explicit_retry_preserves_event': [307, 503, 204],
            'test_callback_independent_of_final_ack': ['hold'],
            'test_worker_exit_leaves_trunk_running': ['hold'],
        }.get(self._testMethodName, [204])
        port = self.start_subscriber(responses)
        local, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with local, child:
            # EnterGrammarWorker validates that fd 3 is owned by the real parent
            # and sets the production limits before reading private settings.
            setup = ('import os,sys;os.dup2(int(sys.argv[1]),3,inheritable=True);'
                     'os.set_inheritable(3,True);os.execv(sys.argv[2],sys.argv[2:])')
            self.sender = subprocess.Popen(
                [sys.executable, '-c', setup, str(child.fileno()), WORKER,
                 '--sip-webhook-worker', str(os.getpid()), str(1 << 30)],
                pass_fds=(child.fileno(),), stdout=subprocess.DEVNULL, stderr=self.log)
            self.stack.callback(self.stop_process, self.sender)
            network = dict(allow_http=False, public_networks=False,
                           allowed_networks=['127.0.0.1/32','::1/128'],
                           allowed_hosts=['localhost'], ports=[port],
                           ca_file=str(self.ca[0]), timeout_ms=3000, max_redirects=0)
            if self._testMethodName == 'test_untrusted_subscriber':
                network['ca_file'] = str(self.root/'other-ca/root.pem')
            local.settimeout(4)
            local.send(json.dumps(dict(scope=SCOPE, secret=self.secret, network=network,
                                       url=f'https://localhost:{port}/calls')).encode())
            self.assertEqual(local.recv(256).decode(), 'READY ' + SCOPE)
            yield local

    def publish(self, live, state='submitted', scope=SCOPE):
        self.control.send(f'PUBLISH req {scope} {live}'.encode())
        self.assertEqual(self.event(), ['RESULT', 'req', state])

    def receipt(self, live, *state):
        self.assertEqual(self.event(), ['WEBHOOK', SCOPE, live, *state])

    def test_publish_after_admission(self):
        peer = self.peer(self.good)
        token = secrets.token_hex(8)
        body = b'v=0\r\ns=private-media-offer\r\nt=0 0\r\n'
        extra = (b'X-Route: first\r\nAuthorization: Digest username="private"\r\n'
                 b'x-route: second\r\nX-Long: once\r\n twice\r\n'
                 b'Proxy-Authorization: Digest username="private"\r\n'
                 b'Content-Type: application/sdp\r\n')
        peer.send('INVITE', token, extra=extra, body=body, fragmented=True)
        tag = peer.response(180, 'INVITE', token)
        notice = self.event()
        self.assertEqual(notice[:2], ['INCOMING', SCOPE])
        live = notice[2]
        self.publish(live, 'not_found', 'other')
        self.assertFalse(select.select([self.peer_server.stdout], [], [], .15)[0])
        self.publish(live)
        event = self.subscriber()['event']
        self.assertEqual(set(event), {'type','id','object','created_at','data'})
        self.assertEqual(event['type'], 'live.transport.incoming')
        self.assertEqual(event['object'], 'event')
        self.assertLess(abs(time.time() - event['created_at']), 5)
        data = event['data']
        self.assertEqual(set(data), {'type','session_id','sip_headers'})
        self.assertEqual((data['type'], data['session_id']), ('sip', live))
        fields = [(p['name'], p['value']) for p in data['sip_headers']]
        self.assertEqual(fields[7:], [('X-Route','first'),('x-route','second'),
                         ('X-Long','once twice'),('Content-Type','application/sdp'),
                         ('Content-Length',str(len(body)))])
        self.assertEqual(fields[0], ('Via', f'SIP/2.0/TLS 127.0.0.1:9;branch=z9hG4bK{token};rport'))
        self.assertNotIn('private', json.dumps(event))
        self.receipt(live, 'delivered', '204')
        self.publish(live, 'delivered')
        self.assertFalse(select.select([self.peer_server.stdout], [], [], .15)[0])
        self.reject(SCOPE, live, 603, 'submitted')
        peer.response(603, 'INVITE', token)
        peer.send('ACK', token, to_tag=tag)
        self.assertEqual(self.event(), ['ACK', SCOPE, live, '603'])

    def test_explicit_retry_preserves_event(self):
        _, _, live = self.invite(self.peer(self.good))
        receipts = []
        for code in (307, 503, 204):
            self.publish(live)
            receipts.append(self.subscriber())
            self.receipt(live, 'delivered' if code == 204 else 'rejected', str(code))
        self.assertEqual(receipts, [receipts[0]]*3)

    def test_callback_independent_of_final_ack(self):
        peer = self.peer(self.good)
        token, tag, live = self.invite(peer)
        self.publish(live)
        self.subscriber()
        self.publish(live, 'in_flight')
        self.reject(SCOPE, live, 486, 'submitted')
        peer.response(486, 'INVITE', token)
        peer.send('ACK', token, to_tag=tag)
        self.assertEqual(self.event(), ['ACK', SCOPE, live, '486'])
        self.peer_server.stdin.write('release\n')
        self.peer_server.stdin.flush()
        self.receipt(live, 'delivered', '204')

    def test_worker_exit_leaves_trunk_running(self):
        peer = self.peer(self.good)
        token, tag, live = self.invite(peer)
        self.publish(live)
        self.subscriber()
        self.sender.kill()
        self.sender.wait(3)
        self.receipt(live, 'unknown')
        self.publish(live, 'unavailable')
        self.reject(SCOPE, live, 603, 'submitted')
        peer.response(603, 'INVITE', token)
        peer.send('ACK', token, to_tag=tag)
        self.assertEqual(self.event(), ['ACK', SCOPE, live, '603'])
        self.invite(peer)

    def test_untrusted_subscriber(self):
        peer = self.peer(self.good)
        token, tag, live = self.invite(peer)
        self.publish(live)
        self.receipt(live, 'unknown')
        self.assertFalse(select.select([self.peer_server.stdout], [], [], .12)[0])
        self.reject(SCOPE, live, 486, 'submitted')
        peer.response(486, 'INVITE', token)
        peer.send('ACK', token, to_tag=tag)
        self.assertEqual(self.event(), ['ACK', SCOPE, live, '486'])
