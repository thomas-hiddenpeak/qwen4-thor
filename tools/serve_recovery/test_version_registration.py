"""Real registration files/locks; all model and service receipts are CPU fakes."""
import copy
import json
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_store, bundle, deployment, ledger, store
from tools.serve_recovery import test_profile_deployment, version_contract as contract
from tools.serve_recovery import version_policy, version_registration as manager, version_store


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.base = test_profile_deployment.DeploymentTests()
        self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.switch()
        self.state = self.base.state
        self.binding = self.base.binding
        self.root = dict(package=str(self.base.new),manifest_sha256=self.base.new_sha)
        self.fallback = dict(package=str(self.base.old),manifest_sha256=self.base.old_sha)
        old = contract.payload(self.fallback)
        self.decision = self.base.fixture.directory/'cpu-acceptance.json'
        self.release = self.base.fixture.directory/'cpu-service-release.json'
        bundle.engine.save(self.decision,dict(status='PASS_CPU_FIXTURE',acceptance='ACCEPT_CPU_FIXTURE',
            manifest_sha256=self.base.old_sha,engine_binary_sha256=old['binding']['binary_sha256'],
            actual_business=dict(status='PASS_CPU_FIXTURE',env_abort=False,
                postflight={k:True for k in ('sources_unchanged','owned_processes_gone',
                                            'ports_free','lock_released')})))
        bundle.engine.save(self.release,dict(acceptance='ACCEPT_CPU_FIXTURE',recovery_accepted=True,
            package=str(self.base.old),manifest_sha256=self.base.old_sha,
            engine_binary_sha256=old['binding']['binary_sha256'],
            acceptance_sha256=bundle.engine.sha(self.decision)))
        self.receipt = self.receipts()
        self.registration = self.prepare()
        self.core_before = (self.state/'ledger.json').read_bytes()

    def receipts(self):
        return {name:dict(path=str(path),sha256=bundle.engine.sha(path))
                for name,path in [('release',self.release),('decision',self.decision)]}

    def prepare(self, root=None):
        root = self.root if root is None else root
        return contract.prepare(root,root,self.fallback,str(uuid.uuid4()),
                                fallback_acceptance=self.receipt)

    def install(self, registration=None, expected=None):
        registration = self.registration if registration is None else registration
        return manager.install(self.state,registration,
            request_id=registration['registry']['cycle_id'],expected_cycle=expected)

    def load(self):
        return version_store.execute(self.binding,dict(sequence=1,op='load'))

    def consume(self):
        value = self.load();payload = value['registry']['payloads']['primary']
        event = dict(role='model',generation=1,boot_id=value['boot_id'],
            payload_sha256=payload['manifest_sha256'],kind='native_process_signal',
            signal=11,fingerprint='e'*64)
        proof = dict(desired='RUNNING',boot_id=value['boot_id'],
            payload_sha256=payload['manifest_sha256'],generations={r:1 for r in version_policy.ROLES},
            termination_requested=False,**{k:True for k in version_policy.GUARDS})
        candidate = version_policy.observe(value,event,proof,str(uuid.uuid4()))
        return version_store.execute(self.binding,dict(sequence=2,op='commit',
            request_id=str(uuid.uuid4()),expected_revision=value['revision'],candidate=candidate))

    def test_exact_qualification_registration_and_later_replay_keep_consumed_state(self):
        contract.verify(self.registration)
        self.install();spent = self.consume()
        replay = self.install()
        self.assertTrue(replay['fallback_consumed'])
        self.assertEqual(self.load(),spent)
        actual = manager.load(self.state,deployment.read_descriptor(self.state))
        self.assertEqual(actual['registration'],self.registration)
        self.assertEqual(actual['state'],spent)
        self.assertEqual((self.state/'ledger.json').read_bytes(),self.core_before)

    def test_resigned_failed_service_business_does_not_qualify_old_payload(self):
        decision = bundle.engine.read(self.decision)
        decision['actual_business']['env_abort'] = True
        bundle.engine.save(self.decision,decision)
        release = bundle.engine.read(self.release)
        release['acceptance_sha256'] = bundle.engine.sha(self.decision)
        bundle.engine.save(self.release,release)
        self.receipt = self.receipts()
        with self.assertRaisesRegex(ValueError,'real service business'):self.prepare()
        self.assertFalse(manager.present(self.state))

    def test_resigned_registration_or_changed_capacity_cannot_bypass_actual_contract(self):
        for field in ('compatibility_sha256','qualification_sha256','root_manifest_sha256'):
            bad = copy.deepcopy(self.registration);bad['registry'][field]='f'*64
            with self.assertRaises(ValueError):contract.verify(bad)
        package,digest = self.base.prepare('changed-capacity',max_tokens=512)
        root = dict(package=str(package),manifest_sha256=digest)
        with self.assertRaisesRegex(ValueError,'settings or capacity'):self.prepare(root)

    def test_partial_intent_recovers_frozen_seed_without_reading_a_new_boot(self):
        original = store.Store.write_all;fired = False
        def short(fd,raw):
            nonlocal fired
            if not fired and b'q4t-version-registration-intent-v1' in raw:
                fired=True;os.write(fd,raw[:73]);raise OSError('partial registration intent')
            return original(fd,raw)
        with mock.patch.object(store.Store,'write_all',side_effect=short),self.assertRaises(OSError):
            self.install()
        self.assertTrue(fired)
        with mock.patch.object(store,'stamp',side_effect=AssertionError('new registration boot')):
            self.install()
        self.assertEqual(self.load()['boot_id'],self.registration['bootstrap_boot_id'])
        self.assertEqual((self.state/'ledger.json').read_bytes(),self.core_before)

    def test_initial_family_marker_failure_resumes_original_unpublished_seed(self):
        original = os.replace
        def fail(src,dst,**kwargs):
            if Path(dst).name == 'version-selection.json':raise OSError('outer version marker')
            return original(src,dst,**kwargs)
        with mock.patch.object(os,'replace',side_effect=fail),self.assertRaises(OSError):self.install()
        before = (self.state/'version-selection/policy.json').read_bytes()
        with self.assertRaises(OSError):manager.load(self.state,deployment.read_descriptor(self.state))
        self.install()
        self.assertEqual(before,(self.state/'version-selection/policy.json').read_bytes())

    def test_same_deployment_cannot_rearm_a_consumed_cycle(self):
        self.install();spent = self.consume();next_registration = self.prepare()
        with self.assertRaisesRegex(ValueError,'changed repair deployment'):
            self.install(next_registration,spent['registry']['cycle_id'])
        self.assertEqual(self.load(),spent)

    def repair(self):
        self.base.fixture.binary.write_bytes(b'cpu explicit repair engine')
        self.base.fixture.base['files']['q4t'] = bundle.engine.sha(self.base.fixture.binary)
        self.base.fixture.base['protocol_sha256'] = 'f'*64
        package,digest = self.base.prepare('repaired-root')
        deployment.switch(self.state,package,digest,expected=self.root['manifest_sha256'],
                          request_id=str(uuid.uuid4()))
        return self.prepare(dict(package=str(package),manifest_sha256=digest))

    def test_interrupted_explicit_repair_keeps_predecessor_and_resumes_one_cycle(self):
        self.install();spent = self.consume();repaired = self.repair()
        original = os.replace
        def fail(src,dst,**kwargs):
            fd = kwargs.get('dst_dir_fd')
            if (str(src)=='policy.pending' and fd is not None and
                Path(os.readlink('/proc/self/fd/'+str(fd))).name == 'version-selection'):
                raise OSError('repair before rename')
            return original(src,dst,**kwargs)
        with mock.patch.object(os,'replace',side_effect=fail),self.assertRaises(OSError):
            self.install(repaired,spent['registry']['cycle_id'])
        with self.assertRaises(ValueError):self.load()
        self.install(repaired,spent['registry']['cycle_id'])
        value = self.load()
        self.assertEqual(value['revision'],spent['revision']+1)
        self.assertIsNone(value['switch'])
        path = self.state/manager.DIRECTORY/(repaired['registry']['cycle_id']+'.json')
        self.assertEqual(json.loads(path.read_bytes())['previous'],spent)
        self.assertEqual((self.state/'ledger.json').read_bytes(),self.core_before)
        self.assertEqual(manager.load(self.state,deployment.read_descriptor(self.state))['state'],value)

    def test_registration_requires_owned_locks_and_durable_stop(self):
        for name in ('deployment.lock','guardian.lock','writer.lock'):
            with deployment.claim(self.state,name=name),self.assertRaises(BlockingIOError):self.install()
        with store.Store(self.state,self.binding['service_id']) as writer:
            writer.transact(dict(id=str(uuid.uuid4()),expected_revision=writer.state['revision'],
                                 operation='desired',args=dict(state='RUNNING')))
        before = (self.state/'ledger.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'stopped authority'):self.install()
        self.assertEqual((self.state/'ledger.json').read_bytes(),before)
        self.assertFalse(manager.present(self.state))

    def test_published_missing_state_never_resets(self):
        self.install();spent = self.consume()
        shutil.rmtree(self.state/'version-selection')
        with self.assertRaises(OSError):self.install()
        self.assertFalse((self.state/'version-selection').exists())
        self.assertTrue((self.state/'version-selection.json').exists())
        self.assertTrue(spent['switch'])

    def test_missing_selected_intent_blocks_launch_without_resetting_state(self):
        self.install();spent = self.consume()
        path = self.state/manager.DIRECTORY/(spent['registry']['cycle_id']+'.json')
        path.unlink()
        with self.assertRaisesRegex(ValueError,'registration is missing'):
            manager.load(self.state,deployment.read_descriptor(self.state))
        self.assertEqual(self.load(),spent)
        self.assertEqual((self.state/'ledger.json').read_bytes(),self.core_before)


if __name__=='__main__':unittest.main()
