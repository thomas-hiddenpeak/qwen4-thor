"""Unpublished initialization crash recovery and no reset after publication."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_policy, access_store, bundle, deployment
from tools.serve_recovery import initialization as init, ledger, quota_policy, quota_store, service, store
from tools.serve_recovery import test_native_bundle, test_usage_store, usage_store


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_bundle.BundleTests()
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        receipt = self.fixture.prepare();self.package = Path(receipt['package'])
        self.digest = receipt['manifest_sha256'];self.manifest = bundle.verify(self.package, self.digest)
        self.state = self.fixture.directory/'state'

    def start(self):return service.initialize(self.package, self.digest, self.state)

    def resume(self):return init.finish(self.state, self.package, self.digest, self.manifest)

    def test_published_initialization_replay_preserves_changed_keys_limits_usage_and_core(self):
        with mock.patch.object(service.subprocess, 'Popen') as spawn:result = self.start()
        spawn.assert_not_called();self.assertFalse(result['automatic_start'])
        desc = deployment.read_descriptor(self.state);self.assertEqual(desc['version'], 2)
        binding = access_store.binding(self.state, desc['service_id'])
        with access_store.open_bound(binding) as journal:
            previous = journal.head['state']['policy']
            candidate = access_policy.from_keys([dict(id='alpha', api_keys=['C'*40]), dict(id='beta', api_keys=[])], previous)
            journal.transact(str(uuid.uuid4()), previous['revision'], candidate)
        with quota_store.open_bound(binding) as journal:
            previous = journal.head['state']['policy'];clients = copy.deepcopy(previous['clients'])
            clients[0]['tokens_per_minute'] = 123
            journal.transact(str(uuid.uuid4()), previous['revision'], quota_policy.desired(previous, clients))
        owner = test_usage_store.owner()
        usage_store.execute(binding, dict(sequence=1, op='begin', generation=1, owner=owner), now_ns=100)
        usage_store.execute(binding, dict(sequence=2, op='seal', generation=1, owner=owner,
                                          snapshot=test_usage_store.snapshot()), now_ns=200)
        names = ['ledger.json', 'service.json', *(w.directory_name+'/policy.json' for w in deployment.FAMILIES.values())]
        before = {n:(self.state/n).read_bytes() for n in names}
        self.assertTrue(self.resume()['resumed'])
        self.assertEqual(before, {n:(self.state/n).read_bytes() for n in names})
        with self.assertRaises(FileExistsError):self.start()

    def test_partial_first_intent_has_no_authority_and_cli_resume_finishes(self):
        original = store.Store.write_all;fired = False
        def write(fd, raw):
            nonlocal fired
            if not fired and init.FORMAT.encode() in raw:
                fired = True;os.write(fd, raw[:19]);raise OSError('CPU first intent interruption')
            return original(fd, raw)
        with mock.patch.object(store.Store, 'write_all', side_effect=write), self.assertRaises(OSError):self.start()
        self.assertFalse((self.state/'writer.lock').exists());self.assertFalse((self.state/'service.json').exists())
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = service.main(['init-resume', '--state', str(self.state)], package=self.package, digest=self.digest)
        self.assertEqual(code, 0, error.getvalue());self.assertTrue(json.loads(output.getvalue())['initialized'])

    def test_partial_core_and_policy_writes_resume_with_same_durable_identity(self):
        original = store.Store.write_all
        for name in ('writer.lock', 'ledger.pending', 'client-access/writer.lock', 'service-deployment/policy.pending'):
            with self.subTest(name=name):
                self.state = self.fixture.directory/('state-'+str(uuid.uuid4()));fired = False
                def write(fd, raw):
                    nonlocal fired
                    if not fired and os.readlink(f'/proc/self/fd/{fd}') == str(self.state/name):
                        fired = True;os.write(fd, raw[:11]);raise OSError('CPU partial initial file')
                    return original(fd, raw)
                with mock.patch.object(store.Store, 'write_all', side_effect=write), self.assertRaises(OSError):self.start()
                self.assertTrue(fired);value = json.loads((self.state/init.INTENT).read_bytes())
                self.assertFalse((self.state/'service.json').exists())
                result = self.resume();self.assertEqual(result['service_id'], value['descriptor']['service_id'])
                current = json.loads((self.state/'ledger.json').read_bytes())
                self.assertEqual(current['state'], value['core']);self.assertEqual(current['service_contract'], store.SERVICE_CONTRACT)

    def test_publishing_unknown_commit_can_restore_descriptor_without_reseeding(self):
        original = deployment.write_descriptor
        for after in (False, True):
            with self.subTest(after=after):
                self.state = self.fixture.directory/('state-'+str(uuid.uuid4()))
                def write(state, value):
                    if after:original(state, value)
                    raise OSError('CPU descriptor acknowledgement lost')
                with mock.patch.object(deployment, 'write_descriptor', side_effect=write), self.assertRaises(OSError):self.start()
                value = json.loads((self.state/init.INTENT).read_bytes());self.assertEqual(value['phase'], 'publishing')
                before = (self.state/'ledger.json').read_bytes()
                with mock.patch.object(init, 'initialize_core', side_effect=AssertionError('cannot reseed')), \
                     mock.patch.object(deployment, 'initialize_family', side_effect=AssertionError('cannot reseed')):
                    self.assertTrue(self.resume()['initialized'])
                self.assertEqual(before, (self.state/'ledger.json').read_bytes())

    def test_missing_published_journal_or_changed_package_cannot_initialize_again(self):
        self.start();before = (self.state/'ledger.json').read_bytes()
        with self.assertRaises(ValueError):init.finish(self.state, self.package, 'a'*64, self.manifest)
        shutil.rmtree(self.state/'client-access')
        with self.assertRaises(FileNotFoundError):self.resume()
        self.assertFalse((self.state/'client-access').exists());self.assertEqual(before, (self.state/'ledger.json').read_bytes())

    def test_owner_locks_corrupt_intent_and_foreign_unbound_files_refuse_recovery(self):
        self.start()
        for name in ('deployment.lock', 'guardian.lock', 'writer.lock'):
            with deployment.claim(self.state, name=name), self.assertRaises(BlockingIOError):self.resume()
        (self.state/init.INTENT).write_bytes(b'{}')
        with self.assertRaises(ValueError):self.resume()
        self.state = self.fixture.directory/'foreign';self.state.mkdir(mode=0o700)
        (self.state/'ledger.json').write_bytes(b'foreign state')
        with self.assertRaises(ValueError):self.resume()
        self.assertEqual((self.state/'ledger.json').read_bytes(), b'foreign state')

    def test_new_service_can_upgrade_and_rollback_without_reinitializing_original_package(self):
        self.start();second = self.fixture.directory/'second';shutil.copytree(self.package, second)
        before = (self.state/'ledger.json').read_bytes()
        deployment.switch(self.state, second, self.digest, expected=self.digest, request_id=str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError, 'deployment changed'):self.resume()
        deployment.switch(self.state, self.package, self.digest, expected=self.digest,
                          request_id=str(uuid.uuid4()), rollback=True)
        self.assertEqual(before, (self.state/'ledger.json').read_bytes())
        self.assertEqual(deployment.read_descriptor(self.state)['deployment_revision'], 3)


if __name__ == '__main__':unittest.main()
