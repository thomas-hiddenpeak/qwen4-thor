"""Exercise the production trunk registry, its worker lifetimes and carrier receipts."""
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
        port = self.start_subscriber([204])
        with socket.socket() as unused:
            unused.bind(('127.0.0.1',0))
            self.port = unused.getsockname()[1]
        network = dict(allow_http=False, public_networks=False, allowed_hosts=['localhost'],
                       allowed_networks=['127.0.0.1/32','::1/128'], ports=[port],
                       timeout_ms=3000, max_redirects=0, ca_file=str(self.ca[0]))
        file = self.root/'fixture.json'
        save(file, dict(trunks=[dict(client_id=SCOPE, listen_ip='127.0.0.1', port=self.port,
            certificate_dir=str(self.pki), peer_subject=PEER, webhook=dict(
                url=f'https://localhost:{port}/calls', secret=self.secret, network=network))]))
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

    def incoming(self):
        peer = self.peer(self.good)
        token = secrets.token_hex(8)
        peer.send('INVITE', token)
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
