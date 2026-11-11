"""STOPPED repair after actual journal fallback; CPU payloads are synthetic."""
import copy
import json
import os
from pathlib import Path
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import bundle, deployment, ledger, profile_store
from tools.serve_recovery import test_version_registration, version_launch
from tools.serve_recovery import version_policy, version_runtime, version_store


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.base = test_version_registration.RegistrationTests()
        self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.install()
        self.state,self.binding = self.base.state,self.base.binding
        self.selected = self.base.consume()
        self.request = str(uuid.uuid4())
        self.before = {n:(self.state/n).read_bytes() for n in
            ['ledger.json', *(w.directory_name+'/policy.json' for w in deployment.FAMILIES.values())]}
        self.profile_before = self.profiles()

    def profiles(self):
        return profile_store.execute(self.binding,dict(sequence=1,op='load'))

    def fallback(self):
        return version_runtime.prepare(self.binding,self.selected,dict(
            root_manifest_sha256=self.base.root['manifest_sha256'],payload='fallback',
            cleanup_complete=True,lease_continuous=True,roles_retired=True))

    def target(self, **options):
        fixture = self.base.base.fixture
        fixture.binary.write_bytes(b'CPU repaired after version fallback')
        fixture.base['files']['q4t'] = bundle.engine.sha(fixture.binary)
        fixture.base['protocol_sha256'] = 'f'*64
        self.package,self.digest = self.base.base.prepare('repair-target',**options)

    def repair(self, **options):
        return deployment.switch(self.state,self.package,self.digest,
            expected=self.base.root['manifest_sha256'],request_id=self.request,**options)

    def preserved(self, increment):
        value = self.profiles()
        self.assertEqual(value['revision'],self.profile_before['revision']+increment)
        for name in ('clock','pending','history','selected','not_before_ns','light_since_ns'):
            self.assertEqual(value[name],self.profile_before[name],name)
        for name,raw in self.before.items():self.assertEqual((self.state/name).read_bytes(),raw,name)
        self.assertEqual(self.base.load(),self.selected)
        return value

    @staticmethod
    def rename_failure(original):
        def fail(src,dst,**kwargs):
            fd=kwargs.get('dst_dir_fd')
            if str(src)=='policy.pending' and fd is not None and Path(
                    os.readlink('/proc/self/fd/'+str(fd))).name=='profile-selection':
                raise OSError('CPU interrupted actual profile rename')
            return original(src,dst,**kwargs)
        return fail

    def terminal(self):
        event=copy.deepcopy(self.selected['switch']['fault'])
        event.update(generation=2,payload_sha256=self.selected['registry'][
            'payloads']['fallback']['manifest_sha256'])
        proof=dict(desired='RUNNING',boot_id=self.selected['boot_id'],
            payload_sha256=event['payload_sha256'],generations={r:2 for r in version_policy.ROLES},
            termination_requested=False,**{k:True for k in version_policy.GUARDS})
        candidate=version_policy.observe(self.selected,event,proof,str(uuid.uuid4()))
        self.selected=version_store.execute(self.binding,dict(sequence=1,op='commit',
            request_id=str(uuid.uuid4()),expected_revision=self.selected['revision'],candidate=candidate))

    def test_terminal_fallback_repairs_current_state_then_explicitly_registers_new_cycle(self):
        self.fallback();self.terminal();self.target();self.repair()
        self.preserved(2)
        desc=deployment.read_descriptor(self.state)
        with self.assertRaisesRegex(ValueError,'explicit version registration'):
            version_launch.configuration(self.state,desc,self.state/'must-not-start')
        registration=self.base.prepare(dict(package=str(self.package),manifest_sha256=self.digest))
        self.base.install(registration,self.selected['registry']['cycle_id'])
        self.assertIsNone(self.base.load()['terminal_fault'])
        self.assertIsNone(self.base.load()['switch'])
        path=self.state/'version-registrations'/(registration['registry']['cycle_id']+'.json')
        self.assertEqual(json.loads(path.read_bytes())['previous'],self.selected)
        self.assertFalse((self.state/'must-not-start').exists())
        for name,raw in self.before.items():self.assertEqual((self.state/name).read_bytes(),raw,name)

    def test_unstarted_fallback_intent_repairs_primary_without_start_or_extra_migration(self):
        self.target();self.repair();self.preserved(1)
        self.assertTrue(self.repair()['replayed']);self.preserved(1)

    def test_pending_automatic_profile_migration_resumes_only_original_switch_then_repairs(self):
        with mock.patch.object(os,'replace',side_effect=self.rename_failure(os.replace)), \
                self.assertRaises(OSError):self.fallback()
        pending=self.state/'profile-selection/policy.pending'
        self.assertTrue(pending.exists())
        self.target();self.repair();self.preserved(2)
        self.assertFalse(pending.exists())

    def test_interrupted_repair_profile_commit_resumes_exact_deployment_once(self):
        self.fallback();self.target()
        with mock.patch.object(os,'replace',side_effect=self.rename_failure(os.replace)), \
                self.assertRaises(OSError):self.repair()
        self.assertEqual(deployment.status(self.state)['phase'],'prepared')
        with self.assertRaisesRegex(ValueError,'resume identity'):
            deployment.switch(self.state,self.package,self.digest,
                expected=self.base.root['manifest_sha256'],request_id=str(uuid.uuid4()))
        self.repair();self.preserved(2)
        self.assertTrue(self.repair()['replayed']);self.preserved(2)

    def test_descriptor_advanced_before_failure_replays_using_retained_control_root(self):
        self.fallback();self.target();original=deployment.write_descriptor
        def fail(state,value):original(state,value);raise OSError('after descriptor replacement')
        with mock.patch.object(deployment,'write_descriptor',side_effect=fail), \
                self.assertRaises(OSError):self.repair()
        self.assertEqual(deployment.read_descriptor(self.state)['manifest_sha256'],self.digest)
        raw=(self.state/'profile-selection/policy.json').read_bytes()
        self.repair();self.preserved(2)
        self.assertEqual((self.state/'profile-selection/policy.json').read_bytes(),raw)

    def test_resigned_foreign_automatic_receipt_cannot_authorize_repair(self):
        with mock.patch.object(os,'replace',side_effect=self.rename_failure(os.replace)), \
                self.assertRaises(OSError):self.fallback()
        path=self.state/'profile-selection/policy.pending';value=json.loads(path.read_bytes())
        value['state']['receipts'][-1]['id']=str(uuid.uuid4())
        value['sha256']=ledger.digest(value['state']);path.write_bytes(ledger.canonical(value))
        raw=(self.state/'service-deployment/policy.json').read_bytes()
        self.target()
        with self.assertRaisesRegex(ValueError,'another deployment or version switch'):self.repair()
        self.assertEqual((self.state/'service-deployment/policy.json').read_bytes(),raw)
        self.assertTrue(path.exists())
        self.assertEqual(self.base.load(),self.selected)

    def test_explicit_capacity_repair_preserves_fallback_pending_original_qualification(self):
        self.fallback();self.target(max_tokens=512);self.repair()
        value=self.preserved(2)
        self.assertEqual(value['format'],'q4t-profile-selection-v2')
        self.assertNotEqual(value['profiles'],self.profile_before['profiles'])
        self.assertIn(value['pending']['request_id'],value['history_bindings'])

    def test_legacy_root_rollback_then_forward_keeps_consumed_cycle_and_current_journal(self):
        self.fallback()
        old=self.base.fallback
        deployment.switch(self.state,old['package'],old['manifest_sha256'],
            expected=self.base.root['manifest_sha256'],request_id=str(uuid.uuid4()),rollback=True)
        raw=(self.state/'version-selection/policy.json').read_bytes()
        deployment.switch(self.state,self.base.root['package'],self.base.root['manifest_sha256'],
            expected=old['manifest_sha256'],request_id=str(uuid.uuid4()))
        self.preserved(2)
        self.assertEqual((self.state/'version-selection/policy.json').read_bytes(),raw)
        run=self.state/'runs'/str(uuid.uuid4());run.mkdir(mode=0o700)
        cfg=version_launch.configuration(self.state,deployment.read_descriptor(self.state),run)
        self.assertEqual(cfg['registry'],self.selected['registry'])
        self.assertIsNotNone(self.base.load()['switch'])

    def test_another_explicit_repair_before_registration_keeps_old_cycle_without_adopting_it(self):
        self.fallback();self.target();self.repair();self.preserved(2)
        first=self.digest
        fixture=self.base.base.fixture
        fixture.binary.write_bytes(b'CPU second explicit repair')
        fixture.base['files']['q4t']=bundle.engine.sha(fixture.binary)
        fixture.base['protocol_sha256']='9'*64
        package,digest=self.base.base.prepare('second-repair')
        deployment.switch(self.state,package,digest,expected=first,request_id=str(uuid.uuid4()))
        self.preserved(3)
        with self.assertRaisesRegex(ValueError,'explicit version registration'):
            version_launch.configuration(self.state,deployment.read_descriptor(self.state),
                                         self.state/'must-remain-stopped')
        self.assertFalse((self.state/'must-remain-stopped').exists())


if __name__=='__main__':unittest.main()
