"""CPU-only real model-side accounting IPC; no HTTP/model/GPU substitution."""
import copy
import hashlib
import json
import subprocess
import sys
import unittest

from tools.serve_recovery import ledger, quota_policy
from tools.serve_recovery.test_gateway import config
from tools.serve_recovery.test_quota_admin import ROOT, Server


def request(server, op, payload=None, **fields):
    raw = None if payload is None else ledger.canonical(payload)
    if op == 'usage_restore' and payload is None:raw = b'null'
    with server.connection() as sock:
        header = dict(op=op, **fields)
        if raw is not None:
            header.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        sock.sendall(ledger.canonical(header))
        if raw is not None:
            for offset in range(0, len(raw), 113):sock.sendall(raw[offset:offset+113])
        return json.loads(sock.recv(65536))


class UsageAdminTests(unittest.TestCase):
    def setUp(self):
        self.policy = quota_policy.from_base(config(ROOT)['backend_policy'])
        self.policy['gateway_token'] = 'a'*64
        for client in self.policy['clients']:
            client['requests_per_minute'] = 1
            client['tokens_per_minute'] = 100

    def server(self, policy=None):
        server = Server(self.policy if policy is None else policy)
        self.addCleanup(server.close)
        return server

    def test_live_window_survives_real_process_replacement_and_replay(self):
        old = self.server()
        self.assertFalse(request(old, 'usage_freeze')['ok'])
        self.assertTrue(request(old, 'usage_restore')['ok'])
        self.assertTrue(old.command('reserve', id='alpha')['ok'])
        self.assertFalse(request(old, 'usage_freeze')['ok'])
        self.assertTrue(request(old, 'usage_status')['usage']['frozen'])
        old.command('settle')
        sealed = request(old, 'usage_freeze')
        self.assertTrue(sealed['ok'])
        self.assertEqual(sealed, request(old, 'usage_freeze'))
        self.assertFalse(old.command('reserve', id='beta')['ok'])
        self.assertEqual(sealed['usage']['snapshot']['clients'][0]['tokens'], '5')
        next_model = self.server()
        snapshot = sealed['usage']['snapshot']
        self.assertTrue(request(next_model, 'usage_restore', snapshot)['ok'])
        self.assertFalse(next_model.command('reserve', id='alpha')['ok'])
        self.assertTrue(next_model.command('reserve', id='beta')['ok'])
        next_model.command('settle')
        self.assertTrue(request(next_model, 'usage_restore', snapshot)['ok'])
        self.assertFalse(next_model.command('reserve', id='beta')['ok'])
        self.assertFalse(request(next_model, 'usage_restore')['ok'])
        self.assertNotEqual(old.process.pid, next_model.process.pid)
        self.assertIsNone(old.process.poll())

    def test_foreign_same_uid_can_observe_but_cannot_freeze_or_restore(self):
        server = self.server()
        self.assertTrue(request(server, 'usage_restore')['ok'])
        script = '''import hashlib,json,socket,sys
name=sys.argv[1]
results=[]
for op in ('usage_status','usage_freeze','usage_restore'):
 with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as s:
  s.settimeout(2);s.connect('\\0'+name)
  msg={'op':op}
  if op=='usage_restore':msg.update(bytes=4,sha256=hashlib.sha256(b'null').hexdigest())
  s.send(json.dumps(msg).encode())
  results.append(json.loads(s.recv(65536))['ok'])
print(json.dumps(results))
'''
        output = subprocess.check_output([sys.executable, '-I', '-B', '-c', script,
                                          server.address], text=True, timeout=5)
        self.assertEqual(json.loads(output), [True, False, False])
        self.assertFalse(request(server, 'usage_status')['usage']['frozen'])
        self.assertTrue(server.command('reserve', id='alpha')['ok'])
        server.command('settle')

    def test_large_snapshot_and_corrupt_payload_are_bounded_and_transactional(self):
        policy = copy.deepcopy(self.policy)
        policy['clients'] = [dict(policy['clients'][0], id='client_'+str(i))
                             for i in range(64)]
        old, restored = self.server(policy), self.server(policy)
        self.assertTrue(request(old, 'usage_restore')['ok'])
        snapshot = request(old, 'usage_freeze')['usage']['snapshot']
        self.assertGreater(len(ledger.canonical(snapshot)), 8192)
        bad = copy.deepcopy(snapshot);bad['clients'][0]['tokens'] = 1
        self.assertFalse(request(restored, 'usage_restore', bad)['ok'])
        self.assertFalse(request(restored, 'usage_status')['usage']['recovered'])
        self.assertTrue(request(restored, 'usage_restore', snapshot)['ok'])
        self.assertTrue(restored.command('reserve', id='client_63')['ok'])
        restored.command('settle')

    def test_corrupt_digest_and_incomplete_restore_do_not_consume_startup(self):
        server = self.server()
        with server.connection() as sock:
            sock.sendall(ledger.canonical(dict(op='usage_restore', bytes=4, sha256='0'*64)))
            sock.sendall(b'null')
            self.assertFalse(json.loads(sock.recv(65536))['ok'])
        with server.connection() as sock:
            sock.sendall(ledger.canonical(dict(op='usage_restore', bytes=4,
                sha256=hashlib.sha256(b'null').hexdigest())))
            sock.sendall(b'nu')
            self.assertEqual(sock.recv(65536), b'')
        self.assertFalse(request(server, 'usage_status')['usage']['recovered'])
        self.assertTrue(request(server, 'usage_restore')['ok'])
        self.assertTrue(server.command('reserve', id='alpha')['ok'])
        server.command('settle')


if __name__ == '__main__':unittest.main()
