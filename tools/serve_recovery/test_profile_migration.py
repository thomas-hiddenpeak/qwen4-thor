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

    def extension(self, mutate=None, *, capacity=False):
        new = copy.deepcopy(self.new)
        if capacity:
            new['profiles'] = profile_contract.profiles(dict(
                new['profiles']['primary'], max_tokens=512))
        records = {name:copy.deepcopy(row['record']) for name,row in self.second.items()}
        for name, record in records.items():
            record['capabilities']['chat']['refusal_output'] = dict(
                profile='q4t-refusal-v1', chat_json=True, chat_sse=True)
            record['capabilities']['template'].update(
                message_profile='qwen3.8-openai-v4', renderer_source_sha256='e'*64)
            if capacity:
                from tools.serve_service import supervisor
                record['configuration'].update(supervisor.expected_configuration(
                    new['profiles'][name]))
                record['configuration']['default_output_tokens'] = 512
            if mutate:
                mutate(record['capabilities'])
        return new, bind_records(new, records)

    def extended_permit(self, new, evidence, **kwargs):
        return migration.permit(self.old, new, self.first, evidence,
            self.config, self.config, **kwargs)

    def search_extension(self):
        previous = {n:copy.deepcopy(row['record']) for n,row in self.first.items()}
        for record in previous.values():
            record['capabilities'].setdefault('responses', {})['output_items'] = [
                'message', 'reasoning', 'function_call', 'custom_tool_call', 'compaction']
        self.first = bind_records(self.old, previous)
        target = copy.deepcopy(previous)
        for record in target.values():
            response = record['capabilities']['responses']
            response['output_items'].append('file_search_call')
            response['file_search'] = dict(profile='q4t-file-search-v1', implemented=True,
                                           enabled=True, shared_output_budget=True)
        return target, bind_records(self.new, target)

    def test_search_output_extension_requires_explicit_upgrade_and_preserves_original(self):
        _, evidence = self.search_extension()
        original = copy.deepcopy((self.first, evidence))
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            self.extended_permit(self.new, evidence)
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            self.extended_permit(self.new, evidence, automatic=True,
                                 allow_capability_extension=True)
        grant = self.extended_permit(self.new, evidence, allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.EXTENSION_FORMAT)
        self.assertEqual((self.first, evidence), original)
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            migration.permit(self.new, self.old, evidence, self.first, self.config,
                             self.config, allow_capability_extension=True)

    def test_search_output_enum_rejects_unknown_duplicate_type_and_missing_implementation(self):
        records, _ = self.search_extension()
        valid = records['primary']['capabilities']
        mutations = [
            lambda r:r['output_items'].append('web_search_call'),
            lambda r:r['output_items'].append('file_search_call'),
            lambda r:r['output_items'].append(True),
            lambda r:r.update(output_items={'file_search_call':True}),
            lambda r:r.pop('file_search'),
            lambda r:r['file_search'].update(implemented=False),
            lambda r:r['file_search'].update(implemented=1),
            lambda r:r['file_search'].update(profile='unknown')]
        for mutation in mutations:
            value = copy.deepcopy(valid)
            mutation(value['responses'])
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                migration.semantic_capabilities(value)

    def test_same_search_capabilities_can_recover_but_other_arrays_stay_exact(self):
        records, evidence = self.search_extension()
        old = copy.deepcopy(self.old)
        same = bind_records(old, copy.deepcopy(records))
        grant = migration.permit(old, self.new, same, evidence, self.config,
                                 self.config, automatic=True)
        self.assertEqual(grant['format'], migration.FORMAT)
        changed = copy.deepcopy(records)
        for record in changed.values():
            record['capabilities']['template']['roles'].append('new_role')
        altered = bind_records(self.new, changed)
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            migration.permit(old, self.new, same, altered, self.config,
                             self.config, allow_capability_extension=True)

    def test_feature_extension_requires_explicit_deployment_and_cannot_be_automatic(self):
        new, evidence = self.extension()
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            self.extended_permit(new, evidence)
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            self.extended_permit(new, evidence, automatic=True,
                                 allow_capability_extension=True)
        grant = self.extended_permit(new, evidence, allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.EXTENSION_FORMAT)
        self.assertEqual(grant['target_capabilities_sha256'], ledger.digest(
            migration.observations(new, evidence)))
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            migration.permit(new, self.old, evidence, self.first, self.config,
                self.config, allow_capability_extension=True)

    def test_extension_keeps_pending_work_budget_and_original_capability_evidence(self):
        new, evidence = self.extension()
        original = copy.deepcopy((self.old, self.first, self.pending))
        grant = self.extended_permit(new, evidence, allow_capability_extension=True)
        value = migration.candidate(self.pending, grant, self.stamp(999999))
        for key in ('history','pending','not_before_ns','selected','profiles',
                    'completed_generation','light_since_ns'):
            self.assertEqual(value[key], self.pending[key])
        self.assertEqual(value['clock']['logical_ns'], self.pending['clock']['logical_ns'])
        self.assertEqual((self.old,self.first,self.pending), original)
        with self.assertRaises(ValueError):
            policy.transition(self.pending, value)
        migration.AuthorizedContract(grant).transition(self.pending, value)
        for bad in (dict(grant, automatic=True), dict(grant, target_capabilities_sha256='x'),
                    dict(grant, target_capabilities_sha256=grant['capabilities_sha256'])):
            with self.assertRaises(ValueError):
                migration.validate(bad)
        damaged = copy.deepcopy(evidence)
        damaged['light']['record']['capabilities']['chat']['refusal_output']['chat_sse'] = False
        with self.assertRaises(ValueError):
            self.extended_permit(new, damaged, allow_capability_extension=True)

    def test_new_feature_cannot_hide_existing_media_tools_embeddings_or_type_loss(self):
        for mutate in (lambda c:c.pop('tools'), lambda c:c.pop('media'),
                       lambda c:c['embeddings']['native_backend'].update(enabled=False),
                       lambda c:c['chat'].update(stream=False),
                       lambda c:c['template'].update(name_max_bytes=True),
                       lambda c:c['template'].update(roles=['user'])):
            new, evidence = self.extension(mutate)
            with self.subTest(mutate=mutate), self.assertRaisesRegex(
                    ValueError, 'application capabilities'):
                self.extended_permit(new, evidence, allow_capability_extension=True)

    def test_renderer_fingerprint_is_bound_separately_but_unknown_template_is_not_ignored(self):
        records = {name:copy.deepcopy(row['record']) for name,row in self.second.items()}
        for record in records.values():
            record['capabilities']['template']['renderer_source_sha256'] = 'e'*64
        evidence = bind_records(self.new, records)
        grant = self.extended_permit(self.new, evidence, automatic=True)
        self.assertEqual(grant['format'], migration.FORMAT)
        for profile in ('unknown-v5', 'qwen3.8-openai-v4'):
            invalid = copy.deepcopy(records)
            for record in invalid.values():
                record['capabilities']['template']['message_profile'] = profile
            evidence = bind_records(self.new, invalid)
            with self.assertRaises(ValueError):
                self.extended_permit(self.new, evidence, allow_capability_extension=True)

    def test_history_source_change_preserves_original_qualification_identity(self):
        records = {n:copy.deepcopy(r['record']) for n,r in self.first.items()}
        for record in records.values():
            record['capabilities']['template']['tool_history_source_sha256'] = '1'*64
        self.first = bind_records(self.old, records)
        updated = copy.deepcopy(records)
        for record in updated.values():
            record['capabilities']['template']['tool_history_source_sha256'] = '2'*64
        evidence = bind_records(self.new, updated)
        original = copy.deepcopy((self.first, evidence))
        grant = self.extended_permit(self.new, evidence, automatic=True)
        self.assertEqual(grant['format'], migration.FORMAT)
        self.assertEqual((self.first, evidence), original)
        self.assertNotEqual(migration.observations(self.old, self.first),
                            migration.observations(self.new, evidence))

    def test_template_fingerprints_reject_invalid_values_without_mutating_observation(self):
        for field in ('renderer_source_sha256', 'tool_history_source_sha256'):
            for value in (None, True, 1, '', 'g'*64, 'a'*63, ['a'*64]):
                caps = {'template':{field:value}}
                before = copy.deepcopy(caps)
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                        ValueError, 'template source identity'):
                    migration.semantic_capabilities(caps)
                self.assertEqual(caps, before)

    def test_history_fingerprint_cannot_hide_async_or_template_contract_loss(self):
        records = {n:copy.deepcopy(r['record']) for n,r in self.first.items()}
        for record in records.values():
            record['capabilities']['template']['tool_history_source_sha256'] = '1'*64
            record['capabilities'].setdefault('responses', {})['async_tools'] = {
                'profile':'q4t-async-tools-v1', 'late_results':True}
        self.first = bind_records(self.old, records)
        for change in (
                lambda c:c['responses']['async_tools'].update(late_results=False),
                lambda c:c['responses'].pop('async_tools'),
                lambda c:c['template'].update(message_profile='unknown-v9'),
                lambda c:c['template'].update(roles=['user'])):
            updated = copy.deepcopy(records)
            for record in updated.values():
                caps = record['capabilities']
                caps['template']['tool_history_source_sha256'] = '2'*64
                change(caps)
            evidence = bind_records(self.new, updated)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.extended_permit(self.new, evidence,
                                     allow_capability_extension=True)

    def test_combined_extension_and_capacity_preserves_historical_qualification(self):
        new, evidence = self.extension(capacity=True)
        with self.assertRaisesRegex(ValueError, 'explicit historical'):
            self.extended_permit(new, evidence, allow_capability_extension=True)
        grant = self.extended_permit(new, evidence, allow_capability_extension=True,
                                     allow_capacity_change=True)
        value = migration.candidate(self.pending, grant, self.stamp(9000))
        self.assertEqual(value['format'], policy.HISTORY_FORMAT)
        pending = self.pending['pending']
        self.assertEqual(value['history_bindings'][pending['request_id']]['registry_sha256'],
                         profile_contract.digest(self.old))
        self.assertEqual(value['pending'], pending)
        self.assertEqual(value['history'], self.pending['history'])
        migration.AuthorizedContract(grant).transition(self.pending, value)

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
