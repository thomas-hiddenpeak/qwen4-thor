"""Qualified migration representation and real journal faults; CPU fixtures only."""
import copy
import hashlib
import json
import os
import uuid
from unittest import mock
import unittest

from tools.serve_recovery import access_store, ledger, profile_contract, profile_store
from tools.serve_recovery import profile_migration as migration, profile_policy as policy
from tools.serve_recovery import test_native_bundle, test_profile_integration


def bind_records(registry, records):
    """Re-sign explicit CPU observations, never actual model qualification."""
    evidence = {}
    for name, record in records.items():
        record.update(identity=copy.deepcopy(registry['identity']),
                      capacity=copy.deepcopy(registry['profiles'][name]))
        record['capabilities']['protocol_source_sha256'] = registry['identity']['protocol_sha256']
        record['configuration']['protocol_sha256'] = registry['identity']['protocol_sha256']
        sha = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        registry['qualifications'][name] = profile_contract.qualification(name,
            record['capacity'], record['identity'], record['capabilities'],
            record['configuration'], record['startup_memory'], sha)
        evidence[name] = dict(record=record, sha256=sha)
    return evidence


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_bundle.BundleTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        _, self.old, records = test_profile_integration.evidence_files(self.fixture)
        self.first = bind_records(self.old, records)
        self.new = copy.deepcopy(self.old)
        self.new['identity'].update(binary_sha256='b'*64, protocol_sha256='d'*64)
        self.second = bind_records(self.new, copy.deepcopy(records))
        self.config = copy.deepcopy(policy.DEFAULTS)
        self.boot = str(uuid.uuid4())
        self.initial = policy.initial(self.old, self.stamp(0), self.config)
        self.pending = policy.prepare(self.initial, 'light', 'sustained_memory_pressure',
                                      str(uuid.uuid4()), 7, self.stamp(1))

    def stamp(self, second, boot=None):
        return ledger.Stamp(boot or self.boot, second*ledger.SECOND)

    def permit(self, config=None, automatic=False):
        return migration.permit(self.old,self.new,self.first,self.second,self.config,
                                self.config if config is None else config,automatic=automatic)

    def test_actual_pending_and_history_survive_new_identity_without_time_credit(self):
        authorization = self.permit()
        for boot,second in [(self.boot,999999),(str(uuid.uuid4()),0)]:
            value = migration.candidate(self.pending,authorization,self.stamp(second,boot))
            self.assertEqual(value['registry_sha256'],profile_contract.digest(self.new))
            self.assertEqual(value['revision'],self.pending['revision']+1)
            for key in ('pending','history','selected','completed_generation','profiles',
                        'not_before_ns','light_since_ns'):
                self.assertEqual(value[key],self.pending[key])
            self.assertEqual(value['clock']['logical_ns'],self.pending['clock']['logical_ns'])
            self.assertEqual(policy.selected_for_launch(value),'light')
            migration.AuthorizedContract(authorization).transition(self.pending,value)

    def test_resigned_capability_loss_or_asset_change_is_not_a_compatible_version(self):
        for mutation in ('tools','embeddings','model_assets','model_files'):
            new = copy.deepcopy(self.new)
            records = {k:copy.deepcopy(v['record']) for k,v in self.second.items()}
            if mutation in ('tools','embeddings'):
                for record in records.values():record['capabilities'].pop(mutation)
            elif mutation == 'model_files':new['identity']['model_files']['config.json']='e'*64
            else:next(iter(new['identity']['model_assets'].values()))['ino'] += 1
            evidence = bind_records(new,records)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                migration.permit(self.old,new,self.first,evidence,self.config,self.config)

    def test_explicit_policy_change_keeps_existing_hold_and_automatic_change_refused(self):
        for seconds in (60,600):
            config=dict(self.config,cooldown_seconds=seconds)
            with self.assertRaisesRegex(ValueError,'automatic fallback'):
                self.permit(config,automatic=True)
            value=migration.candidate(self.pending,self.permit(config),self.stamp(999999))
            self.assertEqual(value['not_before_ns'],max(self.pending['not_before_ns'],
                self.pending['history'][-1]['at_logical_ns']+seconds*ledger.SECOND))
            self.assertEqual(value['history'],self.pending['history'])
        self.permit(automatic=True)

    def test_runtime_contract_cannot_migrate_or_erase_budget_with_authorized_contract(self):
        authorization=self.permit()
        value=migration.candidate(self.pending,authorization,self.stamp(2))
        with self.assertRaises(ValueError):policy.transition(self.pending,value)
        for key,replacement in [('history',[]),('pending',None),('not_before_ns',0),
                                 ('registry_sha256','f'*64),('completed_generation',8)]:
            changed=dict(copy.deepcopy(value),**{key:replacement})
            with self.subTest(key=key),self.assertRaises((ValueError,KeyError,TypeError)):
                migration.AuthorizedContract(authorization).transition(self.pending,changed)

    def journal(self):
        path=self.fixture.directory/'state';path.mkdir(mode=0o700)
        binding=access_store.binding(path,str(uuid.uuid4()))
        profile_store.initialize_bound(binding,self.initial)
        profile_store.execute(binding,dict(sequence=1,op='commit',request_id=str(uuid.uuid4()),
            expected_revision=0,candidate=self.pending))
        class AuthorizedStore(profile_store.Store):
            contract=migration.AuthorizedContract(self.permit())
        value=migration.candidate(self.pending,self.permit(),self.stamp(10000))
        return path,binding,AuthorizedStore,value,str(uuid.uuid4())

    def test_unknown_migration_rename_requires_deployment_authority_to_resume(self):
        path,binding,writer_type,value,request=self.journal()
        with access_store.open_bound(binding,writer_type) as writer:
            with mock.patch.object(access_store.os,'replace',side_effect=OSError('before rename')):
                with self.assertRaises(OSError):writer.transact(request,1,value)
        self.assertTrue((path/'profile-selection/policy.pending').is_file())
        with self.assertRaises(ValueError):profile_store.execute(binding,dict(sequence=2,op='load'))
        with access_store.open_bound(binding,writer_type) as writer:
            self.assertTrue(writer.transact(request,1,value)['replayed'])
        self.assertEqual(profile_store.execute(binding,dict(sequence=3,op='load')),value)
        self.assertFalse((path/'profile-selection/policy.pending').exists())

    def test_real_postrename_receipt_loss_preserves_original_switch_and_one_migration(self):
        _,binding,writer_type,value,request=self.journal()
        with access_store.open_bound(binding,writer_type) as writer:
            original=os.fsync
            def fail(fd):
                if fd==writer.dir_fd:raise OSError('directory fsync receipt lost')
                return original(fd)
            with mock.patch.object(access_store.os,'fsync',side_effect=fail):
                with self.assertRaises(OSError):writer.transact(request,1,value)
        with access_store.open_bound(binding,writer_type) as writer:
            self.assertTrue(writer.transact(request,1,value)['replayed'])
            self.assertEqual(len(writer.head['state']['receipts']),2)
        actual=profile_store.execute(binding,dict(sequence=4,op='load'))
        self.assertEqual(actual,value)
        self.assertEqual(actual['history'],self.pending['history'])
        self.assertEqual(actual['clock']['logical_ns'],self.pending['clock']['logical_ns'])


if __name__=='__main__':unittest.main()
