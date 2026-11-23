"""Exercise the production trunk registry, its worker lifetimes and carrier receipts."""
import base64
import contextlib
import json
import os
import secrets
import select
import socket
import subprocess
import time
import unittest

from tools.serve_live.test_sip_trunk import PEER, SCOPE, TrunkFixture
from tools.serve_live.test_sip_webhook import SDK, SubscriberFixture
from tools.serve_service.package import save

OWNER = os.environ.get('Q4T_SIP_OWNER_FIXTURE')


@unittest.skipUnless(OWNER and SDK, 'set Q4T_SIP_OWNER_FIXTURE and Q4T_OPENAI_SDK_ROOT')
class OwnerTest(SubscriberFixture, TrunkFixture, unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.log = self.stack.enter_context((self.root/(self._testMethodName+'.log')).open('wb'))
        events = {
            'test_two_signed_events_share_one_carrier_disposition':
                ['live.transport.incoming','realtime.call.incoming'],
            'test_realtime_only_subscription': ['realtime.call.incoming'],
        }.get(self._testMethodName)
        port = self.start_subscriber([204]*(len(events) if events else 1))
        with socket.socket() as unused:
            unused.bind(('127.0.0.1',0))
            self.port = unused.getsockname()[1]
        network = dict(allow_http=False, public_networks=False, allowed_hosts=['localhost'],
                       allowed_networks=['127.0.0.1/32','::1/128'], ports=[port],
                       timeout_ms=3000, max_redirects=0, ca_file=str(self.ca[0]))
        file = self.root/'fixture.json'
        hook = dict(url=f'https://localhost:{port}/calls', secret=self.secret, network=network)
        if events:
            hook['event_types'] = events
        save(file, dict(trunks=[dict(client_id=SCOPE, listen_ip='127.0.0.1', port=self.port,
            certificate_dir=str(self.pki), peer_subject=PEER, webhook=hook)]))
        self.owner = subprocess.Popen([OWNER, '--sip-owner-fixture', str(file), SCOPE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True)
        self.stack.callback(self.stop_owner)
        end = time.monotonic()+5
        while time.monotonic() < end:
            self.command('health')
            health = self.read_owner()
            self.assertEqual(health['activation_failures'], 0)
            if health['ready'] == 1:
                break
            time.sleep(.02)
        else:
            self.fail('trunk activation')

    def command(self, value):
        self.owner.stdin.write(value+'\n')
        self.owner.stdin.flush()

    def read_owner(self):
        self.assertTrue(select.select([self.owner.stdout], [], [], 4)[0], 'owner deadline')
        return json.loads(self.owner.stdout.readline())

    def stop_owner(self):
        try:
            if self.owner.poll() is None:
                self.command('stop')
            self.assertEqual(self.owner.wait(4), 0)
        finally:
            self.stop_process(self.owner)

    def incoming(self, offer=None):
        peer = self.peer(self.good)
        token = secrets.token_hex(8)
        peer.send('INVITE', token, body=offer or b'',
                  extra=b'Content-Type: application/sdp\r\n' if offer else b'')
        tag = peer.response(180, 'INVITE', token)
        live = self.subscriber()['event']['data']['session_id']
        return peer, token, tag, live

    def test_signed_notice_outlives_activation_and_rejection_waits_for_ack(self):
        peer, token, tag, live = self.incoming()
        self.command(f'reject foreign {live} 486 500')
        self.assertEqual(self.read_owner(), 404)
        self.command(f'reject {SCOPE} {live} 486 2000')
        peer.response(486, 'INVITE', token)
        self.assertFalse(select.select([self.owner.stdout], [], [], .15)[0])
        peer.send('ACK', token, to_tag=tag)
        self.assertEqual(self.read_owner(), 200)

    def test_missing_ack_is_unknown_and_duplicate_cannot_resubmit(self):
        peer, token, tag, live = self.incoming()
        self.command(f'reject {SCOPE} {live} 603 150')
        peer.response(603, 'INVITE', token)
        self.assertEqual(self.read_owner(), 504)
        self.command(f'reject {SCOPE} {live} 486 1000')
        self.assertEqual(self.read_owner(), 409)
        peer.send('ACK', token, to_tag=tag)
        end = time.monotonic()+2
        while time.monotonic() < end:
            self.command(f'reject {SCOPE} {live} 486 1000')
            status = self.read_owner()
            if status == 404:
                return
            self.assertEqual(status, 409)
            time.sleep(.01)
        self.fail('acknowledged call was not retired')

    def offer(self, ip='127.0.0.1'):
        udp = self.stack.enter_context(socket.socket(socket.AF_INET,socket.SOCK_DGRAM))
        udp.bind(('127.0.0.1', 0))
        key = base64.b64encode(secrets.token_bytes(30)).decode()
        return (f'v=0\r\no=- 1 1 IN IP4 {ip}\r\ns=test\r\nc=IN IP4 {ip}\r\nt=0 0\r\n'
                f'm=audio {udp.getsockname()[1]} RTP/SAVP 0\r\na=rtcp-mux\r\n'
                f'a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{key}\r\n').encode()

    def test_scoped_answer_waits_for_ack_and_cleans_up_on_remote_bye(self):
        peer, token, tag, live = self.incoming(self.offer())
        self.command(f'accept other {live} 2000')
        self.assertEqual(self.read_owner(), 404)
        self.command(f'accept {SCOPE} {live} 2000')
        self.assertEqual(peer.response(200, 'INVITE', token), tag)
        self.assertTrue(b'inline:' in peer.body)
        self.assertFalse(select.select([self.owner.stdout], [], [], .12)[0])
        peer.send('ACK', token, to_tag=tag, branch=token+'ack')
        self.assertEqual(self.read_owner(), 200)
        self.command(f'reject {SCOPE} {live} 486 500')
        self.assertEqual(self.read_owner(), 409)
        peer.send('BYE', token, to_tag=tag, branch=token+'bye', seq=2)
        peer.response(200, 'BYE', token, branch=token+'bye', seq=2)
        self.command('release')
        self.assertEqual(self.read_owner(), 200)
        self.command(f'accept {SCOPE} {live} 500')
        self.assertEqual(self.read_owner(), 404)

    def test_transfer_timeout_late_receipt_and_retry(self):
        peer, token, tag, live = self.incoming(self.offer())
        refer = f'refer {SCOPE} {live} tel:+12025550100'
        self.command(refer+' 200')
        self.assertEqual(self.read_owner(), 409)
        self.command(f'accept {SCOPE} {live} 2000')
        peer.response(200, 'INVITE', token)
        peer.send('ACK', token, to_tag=tag, branch=token+'ack')
        self.assertEqual(self.read_owner(), 200)
        self.command(refer+' 200')
        original = peer.dialog_request('REFER', token, tag)
        self.assertEqual(self.read_owner(), 504)
        self.command(refer+' 200')
        self.assertEqual(self.read_owner(), 409)
        peer.reply_dialog(original, 486)
        # A later request has its own receipt, even if the old 486 is repeated.
        end = time.monotonic() + 3
        while time.monotonic() < end:
            self.command(refer+' 2000')
            if not select.select([self.owner.stdout], [], [], .08)[0]:
                break
            self.assertEqual(self.read_owner(), 409)
        else:
            self.fail('late refusal never released the timed-out operation')
        retry = peer.dialog_request('REFER', token, tag)
        peer.reply_dialog(original, 486)
        self.assertFalse(select.select([self.owner.stdout], [], [], .08)[0])
        peer.reply_dialog(retry, 403)
        self.assertEqual(self.read_owner(), 502)
        peer.send('BYE', token, to_tag=tag, branch=token+'bye', seq=2)
        peer.response(200, 'BYE', token, branch=token+'bye', seq=2)

    def test_invalid_media_address_releases_reservation_for_reject(self):
        peer, token, tag, live = self.incoming(self.offer('127.0.0.2'))
        self.command(f'accept {SCOPE} {live} 2000')
        self.assertEqual(self.read_owner(), 400)
        self.command(f'reject {SCOPE} {live} 488 2000')
        self.assertEqual(peer.response(488, 'INVITE', token), tag)
        peer.send('ACK', token, to_tag=tag)
        self.assertEqual(self.read_owner(), 200)

    def test_late_accept_ack_hangs_up_abandoned_media(self):
        peer, token, tag, live = self.incoming(self.offer())
        self.command(f'accept {SCOPE} {live} 150')
        peer.response(200, 'INVITE', token)
        self.assertEqual(self.read_owner(), 504)
        self.command(f'accept {SCOPE} {live} 500')
        self.assertEqual(self.read_owner(), 409)
        peer.send('ACK', token, to_tag=tag, branch=token+'ack')
        self.assertEqual(peer.reader.readline(256).split()[0], b'BYE')
        headers = {}
        while True:
            line = peer.reader.readline(8192)
            if line == b'\r\n': break
            self.assertTrue(line)
            key, val = line.rstrip().split(b':', 1)
            headers[key.lower()] = val.strip()
        self.assertEqual(headers[b'call-id'], (token+'@peer.test').encode())
        response = [b'SIP/2.0 200 OK'] + [k + b': ' + headers[k] for k in
            (b'via', b'from', b'to', b'call-id', b'cseq')]
        peer.sock.sendall(b'\r\n'.join(response + [b'Content-Length: 0',b'',b'']))

    def test_two_signed_events_share_one_carrier_disposition(self):
        peer = self.peer(self.good)
        token = secrets.token_hex(8)
        peer.send('INVITE', token)
        tag = peer.response(180, 'INVITE', token)
        first, second = self.subscriber()['event'], self.subscriber()['event']
        self.assertEqual(first['type'], 'live.transport.incoming')
        self.assertEqual(second['type'], 'realtime.call.incoming')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['created_at'], second['created_at'])
        self.assertEqual(first['data']['sip_headers'], second['data']['sip_headers'])
        self.assertEqual(set(second['data']), {'call_id','sip_headers'})
        live, rtc = first['data']['session_id'], second['data']['call_id']
        self.assertRegex(rtc, r'^rtc_[0-9a-f]{32}$')
        self.assertNotEqual(live[5:], rtc[4:])
        self.command(f'reject foreign {rtc} 603 100')
        self.assertEqual(self.read_owner(), 404)
        self.command(f'reject {SCOPE} {rtc} 603 125')
        peer.response(603, 'INVITE', token)
        self.assertEqual(self.read_owner(), 504)
        self.command(f'reject {SCOPE} {live} 486 100')
        self.assertEqual(self.read_owner(), 409)
        peer.send('ACK', token, to_tag=tag)
        end = time.monotonic()+2
        while time.monotonic() < end:
            self.command(f'reject {SCOPE} {live} 486 100')
            if self.read_owner() == 404:
                self.command(f'reject {SCOPE} {rtc} 486 100')
                self.assertEqual(self.read_owner(), 404)
                return
            time.sleep(.01)
        self.fail('two handles must retire together')

    def test_realtime_only_subscription(self):
        peer = self.peer(self.good)
        token = secrets.token_hex(8)
        peer.send('INVITE', token)
        tag = peer.response(180, 'INVITE', token)
        event = self.subscriber()['event']
        self.assertEqual(event['type'], 'realtime.call.incoming')
        self.command(f"reject {SCOPE} {event['data']['call_id']} 486 2000")
        peer.response(486, 'INVITE', token)
        peer.send('ACK', token, to_tag=tag)
        self.assertEqual(self.read_owner(), 200)
        self.assertFalse(select.select([self.peer_server.stdout], [], [], .15)[0])
