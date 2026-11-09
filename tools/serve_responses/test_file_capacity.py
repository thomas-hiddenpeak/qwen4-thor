"""Real native policy parsing, publication and durable file-capacity revisions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from tools.serve_gateway import client_policy as cp, gateway
from tools.serve_recovery import access_store, ledger, quota_policy, quota_store
from tools.serve_recovery.test_gateway import config
from tools.serve_recovery.test_quota_admin import Server
from tools.serve_recovery.test_quota_store import QuotaStoreTests

ROOT = Path(__file__).resolve().parents[2]


class FileCapacityTests(unittest.TestCase):
    checker = QuotaStoreTests.checker

    def setUp(self):
        base = ROOT / '.q4t-work/serve-files-20261110/capacity-fixtures'
        base.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.initial = quota_policy.from_base(config(self.state)['backend_policy'])
        self.clients = [dict(c, max_file_body_bytes=cp.FILE_BOUNDS['max_file_body_bytes'][1])
                        for c in self.initial['clients']]

    def test_file_policy_cpp_python_exact_digest_and_invalid_types(self):
        value = quota_policy.desired(self.initial, self.clients)
        self.assertEqual((value['version'], value['revision']), (4, 1))
        self.checker(value)
        self.checker(value, raw=(json.dumps(value, indent=2)+'\n').encode())
        self.checker(dict(value, gateway_token='b'*64))
        for number in (0, 1023, 512*1024**2+65537, True, 2048.0, None):
            bad = copy.deepcopy(value)
            bad['clients'][0]['max_file_body_bytes'] = number
            with self.assertRaises(ValueError):cp.validate(bad)
            self.checker(bad, success=False)
        bad = copy.deepcopy(value);bad['clients'][1].pop('max_file_body_bytes')
        self.checker(bad, success=False)
        self.checker(dict(value, version=3), success=False)
        self.assertTrue(cp.binding(ledger.canonical(value))['mutable'])

    def test_durable_explicit_upgrade_reopens_with_receipt_and_no_downgrade(self):
        binding = access_store.binding(self.state, str(uuid.uuid4()))
        quota_store.initialize_bound(binding, self.initial)
        candidate = quota_policy.desired(self.initial, self.clients)
        request = str(uuid.uuid4())
        with quota_store.open_bound(binding) as store:
            self.assertTrue(store.transact(request, 0, candidate)['durable'])
        with quota_store.open_bound(binding) as store:
            self.assertEqual(store.head['state']['policy'], candidate)
            self.assertTrue(store.transact(request, 0, candidate)['replayed'])
        self.assertEqual(quota_policy.from_base(candidate)['version'], 4)
        with self.assertRaises(ValueError):
            quota_policy.desired(candidate, self.initial['clients'])
        with self.assertRaises(ValueError):
            quota_policy.transition(candidate, dict(self.initial, revision=2))
        self.assertEqual(quota_policy.desired(self.initial,
            self.initial['clients'])['version'], 3)

    def test_actual_admin_upgrade_retains_existing_usage_and_client_slots(self):
        initial = quota_policy.runtime(self.initial, 'a'*64)
        for client in initial['clients']:
            client.update(requests_per_minute=1, tokens_per_minute=100)
        server = Server(initial);self.addCleanup(server.close)
        self.assertTrue(server.command('reserve', id='alpha')['ok'])
        candidate = dict(copy.deepcopy(initial), version=4, revision=1)
        for client in candidate['clients']:
            client['max_file_body_bytes'] = client['max_body_bytes'] * 2
        self.assertTrue(server.publish(candidate, 0)['ok'])
        self.assertEqual(server.command('status')['reserved'], 8)
        server.command('settle')
        self.assertFalse(server.command('reserve', id='alpha')['ok'])
        self.assertEqual(server.command('status')['tokens'], 5)
        self.assertEqual(server.status()['content_sha256'], cp.digest(candidate))
        self.assertFalse(server.publish(dict(initial, revision=2), 1)['ok'])
        self.assertEqual(server.status()['revision'], 1)
        aligned = copy.deepcopy(candidate);aligned['revision'] = 2
        for client in aligned['clients']:client['max_file_body_bytes'] = client['max_body_bytes']
        self.assertTrue(server.publish(aligned, 1)['ok'])
        self.assertTrue(server.publish(dict(initial, revision=3), 2)['ok'])
        self.assertEqual(server.command('status')['tokens'], 5)
        self.assertFalse(server.command('reserve', id='alpha')['ok'])
        self.assertEqual(server.status()['revision'], 3)

    def test_public_file_limit_is_explicit_and_does_not_expand_json(self):
        base = config(self.state)['policy']
        old = gateway.policy(dict(base, version=4, body_bytes=1024))
        self.assertNotIn('file_body_bytes', old)
        new = gateway.policy(dict(base, version=5, body_bytes=1024,
                                  file_body_bytes=512*1024**2+65536))
        self.assertEqual(new['body_bytes'], 1024)
        self.assertIn('http', new)
        self.assertIn('cors', new)
        for bad in (dict(new, version=4),
                    {k: v for k, v in new.items() if k != 'file_body_bytes'},
                    *(dict(new, file_body_bytes=n)
                      for n in (True, None, 1023, 512*1024**2+65537))):
            with self.assertRaises(ValueError):gateway.policy(bad)


if __name__ == '__main__':unittest.main()
