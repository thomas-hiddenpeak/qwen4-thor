"""Durable limits CPU contracts and real C++/Python policy interoperability."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from tools.serve_gateway import client_policy as cp
from tools.serve_recovery import access_policy, access_store, ledger, quota_policy as p, quota_store as s
from tools.serve_recovery.test_gateway import config

ROOT = Path(__file__).resolve().parents[2]


class QuotaStoreTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.q4t-work/serve-access-20261109/quota-store-fixtures';base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name);self.binding = access_store.binding(self.state, str(uuid.uuid4()))
        self.initial = p.from_base(config(self.state)['backend_policy'])

    def checker(self, value, *, success=True, raw=None):
        checker = Path(os.environ.get('Q4T_LIMITS_CHECKER',
            ROOT / '.q4t-work/serve-access-20261109/host/q4t_client_policy_check'))
        raw = ledger.canonical(value) if raw is None else raw
        result = subprocess.run([str(checker)], input=raw, capture_output=True, timeout=3)
        self.assertEqual(result.returncode == 0, success)
        if success:
            row = json.loads(result.stdout)
            self.assertEqual(row['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(row['content_sha256'], cp.digest(value))
            self.assertEqual(row['revision'], value.get('revision', 0))
            self.assertEqual(row['clients'], len(value['clients']))
        else:self.assertEqual(result.stdout, b'')

    def next(self, previous=None, *, rpm=7):
        previous = previous or self.initial
        clients = copy.deepcopy(previous['clients']);clients[0]['requests_per_minute'] = rpm
        return p.desired(previous, clients)

    def test_native_python_digest_ignores_runtime_token_and_json_layout(self):
        versions = [config(self.state)['backend_policy'], self.initial]
        legacy = copy.deepcopy(versions[0]);legacy['version'] = 1;legacy.pop('fair_queue')
        for client in legacy['clients']:
            for field in cp.RATE_BOUNDS:client.pop(field)
        versions.append(legacy)
        for value in versions:
            self.checker(value)
            self.checker(value, raw=(json.dumps(value, indent=2)+'\n').encode())
            rotated = dict(value, gateway_token='e'*64)
            self.assertEqual(cp.digest(value), cp.digest(rotated));self.checker(rotated)
        maximum = copy.deepcopy(self.initial);maximum['revision'] = access_policy.MAX_REVISION
        maximum['clients'] = [dict(maximum['clients'][0], id='c'+str(i)) for i in range(64)]
        for client in maximum['clients']:
            for field, (_, upper) in {**cp.BOUNDS, **cp.RATE_BOUNDS}.items():client[field] = upper
        self.checker(maximum)
        for field, changed in [('revision', True), ('revision', 1.0),
                               ('revision', access_policy.MAX_REVISION+1), ('gateway_token', 'x'*64)]:
            bad = dict(self.initial, **{field: changed})
            with self.assertRaises(ValueError):cp.validate(bad)
            self.checker(bad, success=False)
        self.checker(self.initial, success=False,
            raw=ledger.canonical(self.initial).replace(b'"version":3', b'"version":3,"version":3'))

    def test_independent_journals_and_cross_format_identity_refused(self):
        access = access_policy.from_keys([dict(id='alpha', api_keys=['a'*40])])
        access_store.initialize_bound(self.binding, access)
        before = (self.state / 'client-access/policy.json').read_bytes()
        self.assertTrue(s.initialize_bound(self.binding, self.initial)['initialized'])
        with s.open_bound(self.binding) as current:
            receipt = current.transact(str(uuid.uuid4()), 0, self.next())
            self.assertTrue(receipt['durable']);self.assertFalse(receipt['live_applied'])
        self.assertEqual((self.state / 'client-access/policy.json').read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'identity'):
            access_store.Store(self.state / 'client-limits', self.binding['service_id'], self.binding['state_identity'])
        with self.assertRaisesRegex(ValueError, 'identity'):
            s.Store(self.state / 'client-access', self.binding['service_id'], self.binding['state_identity'])
        with access_store.open_bound(self.binding) as current:
            self.assertEqual(current.head['state']['policy'], access)

    def test_full_receipt_window_and_max_clients_are_bounded(self):
        large = copy.deepcopy(self.initial)
        large['clients'] = [dict(large['clients'][0], id='client_'+str(i)) for i in range(64)]
        s.initialize_bound(self.binding, large)
        requests = []
        with s.open_bound(self.binding) as current:
            previous = large
            for i in range(access_store.MAX_RECEIPTS + 2):
                candidate = self.next(previous, rpm=i % 10)
                request = str(uuid.uuid4());requests.append((request, i, candidate))
                current.transact(request, i, candidate);previous = candidate
            self.assertEqual(len(current.head['state']['receipts']), 128)
        with s.open_bound(self.binding) as current:
            self.assertTrue(current.transact(*requests[-1])['replayed'])
            with self.assertRaisesRegex(ValueError, 'revision'):current.transact(*requests[0])
        self.assertLess((self.state / 'client-limits/policy.json').stat().st_size, p.MAX_BYTES)
        self.assertEqual(len(list((self.state / 'client-limits').iterdir())), 2)

    def test_interrupted_pre_and_postrename_updates_recover_without_reset(self):
        s.initialize_bound(self.binding, self.initial)
        request = str(uuid.uuid4());candidate = self.next()
        with s.open_bound(self.binding) as current:
            with mock.patch.object(access_store.os, 'replace', side_effect=OSError('CPU pending fault')):
                with self.assertRaises(OSError):current.transact(request, 0, candidate)
        with mock.patch.object(access_store.os, 'fsync', side_effect=OSError('CPU durability unavailable')):
            with self.assertRaises(OSError):s.open_bound(self.binding)
        self.assertTrue((self.state / 'client-limits/policy.pending').exists())
        with s.open_bound(self.binding) as current:
            self.assertEqual(current.head['state']['policy'], candidate)
            self.assertTrue(current.transact(request, 0, candidate)['replayed'])
            second = self.next(candidate, rpm=0);other = str(uuid.uuid4());sync = os.fsync
            def fail_directory(fd):
                if fd == current.dir_fd:raise OSError('CPU postrename fault')
                return sync(fd)
            with mock.patch.object(access_store.os, 'fsync', side_effect=fail_directory):
                with self.assertRaises(OSError):current.transact(other, 1, second)
        with s.open_bound(self.binding) as current:
            self.assertTrue(current.transact(other, 1, second)['replayed'])
            self.assertEqual(current.head['state']['policy']['revision'], 2)

    def test_missing_or_corrupt_limits_never_rebootstrap_original_capacity(self):
        s.initialize_bound(self.binding, self.initial)
        path = self.state / 'client-limits/policy.json';original = path.read_bytes()
        path.write_bytes(b'{')
        with self.assertRaises(ValueError):s.open_bound(self.binding)
        with self.assertRaises(ValueError):s.initialize_bound(self.binding, self.initial)
        path.write_bytes(original)
        shutil.rmtree(self.state / 'client-limits')
        with self.assertRaises(OSError):s.initialize_bound(self.binding, self.initial)
        self.assertFalse((self.state / 'client-limits').exists())
        wrong = dict(self.binding, service_id=str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError, 'binding'):s.check_binding(wrong)

    def test_slots_capacity_fairness_and_runtime_policy_contracts(self):
        candidate = self.next()
        self.assertEqual(p.runtime(candidate, 'b'*64)['revision'], 1)
        self.assertEqual(cp.digest(p.runtime(candidate, 'b'*64)), p.digest(candidate))
        for kind in ('remove', 'reorder', 'fairness', 'transport', 'revision'):
            bad = copy.deepcopy(candidate)
            if kind == 'remove':bad['clients'].pop()
            if kind == 'reorder':bad['clients'].reverse()
            if kind == 'fairness':bad['fair_queue'] = False
            if kind == 'transport':bad['gateway_token'] = 'a'*64
            if kind == 'revision':bad['revision'] = 3
            with self.assertRaises(ValueError):p.transition(self.initial, bad)
        candidate['clients'].append(dict(candidate['clients'][0], id='gamma'))
        p.transition(self.initial, candidate)
        with self.assertRaises(ValueError):p.capacity(candidate, 5, 3)
        with self.assertRaises(ValueError):p.capacity(candidate, 6, 2)
        with self.assertRaises(ValueError):p.capacity(candidate, True, 3)
        self.assertEqual(p.capacity(candidate, 6, 3), candidate)
        self.assertEqual(p.describe(candidate)['quota_clients'], 3)


if __name__ == '__main__':unittest.main()
