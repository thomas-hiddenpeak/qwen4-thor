"""Capacity history and isolated legacy reader adaptation; no model starts."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

from tools.serve_recovery import access_store, bundle, ledger, profile_compat
from tools.serve_recovery import profile_contract, profile_migration, profile_store
from tools.serve_recovery import profile_policy as policy
from tools.serve_recovery import test_profile_migration
from tools.serve_service import supervisor

ROOT = Path(__file__).resolve().parents[2]


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.base = test_profile_migration.MigrationTests()
        self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.old = self.base.old
        self.new = copy.deepcopy(self.base.new)
        self.new['profiles'] = profile_contract.profiles(dict(
            self.new['profiles']['primary'], max_tokens=512))
        records = {k:copy.deepcopy(v['record']) for k,v in self.base.second.items()}
        for name, record in records.items():
            record['configuration'].update(supervisor.expected_configuration(
                self.new['profiles'][name]))
            record['configuration']['default_output_tokens'] = self.new['profiles'][name]['max_tokens']
        self.evidence = test_profile_migration.bind_records(self.new, records)
        self.authorization = self.permit()

    def permit(self, **kwargs):
        return profile_migration.permit(self.old, self.new, self.base.first,
            self.evidence, self.base.config, self.base.config,
            **dict(allow_capacity_change=True, **kwargs))

    def migrated(self, previous=None):
        return profile_migration.candidate(self.base.pending if previous is None else previous,
                                          self.authorization, self.base.stamp(10000))

    def test_capacity_change_requires_explicit_qualified_permission(self):
        with self.assertRaisesRegex(ValueError, 'explicit historical'):
            profile_migration.permit(self.old, self.new, self.base.first,
                self.evidence, self.base.config, self.base.config)
        with self.assertRaisesRegex(ValueError, 'explicit historical'):
            self.permit(automatic=True)
        bad = copy.deepcopy(self.authorization); bad['automatic'] = True
        with self.assertRaises(ValueError): profile_migration.validate(bad)
        self.assertEqual(self.authorization['format'], profile_migration.CAPACITY_FORMAT)

    def test_pending_original_rows_and_qualification_are_not_rewritten(self):
        value = self.migrated()
        self.assertEqual(value['format'], policy.HISTORY_FORMAT)
        for key in ('pending','history','not_before_ns','selected','completed_generation'):
            self.assertEqual(value[key], self.base.pending[key])
        row = value['pending']
        identity = value['history_bindings'][row['request_id']]
        self.assertEqual(identity['registry_sha256'], profile_contract.digest(self.old))
        self.assertEqual(identity['profiles'], self.base.pending['profiles'])
        self.assertIsNone(identity['execution'])
        self.assertNotEqual(value['profiles']['light'], row['target_sha256'])
        self.assertEqual(value['clock']['logical_ns'], self.base.pending['clock']['logical_ns'])
        self.assertEqual(policy.permission(value, 'primary', self.base.stamp(10000))['reason'],
                         'profile_intent_pending')

    def test_only_actual_migrated_capacity_can_finish_original_pending_intent(self):
        value = self.migrated(); row = value['pending']
        with self.assertRaisesRegex(ValueError, 'not confirmed'):
            policy.finish(value, 8, row['target_sha256'], self.base.stamp(10001))
        done = policy.finish(value, 8, value['profiles']['light'], self.base.stamp(10001))
        policy.transition(value, done)
        self.assertEqual(done['history'], self.base.pending['history'])
        self.assertEqual(done['history_bindings'][row['request_id']]['execution'], dict(
            registry_sha256=profile_contract.digest(self.new),
            capacity_sha256=value['profiles']['light'], generation=8))
        self.assertEqual(done['selected'], 'light')

    def test_prior_completion_and_repeated_explicit_migrations_keep_budget_history(self):
        prior = policy.finish(self.base.pending, 8, self.base.pending['profiles']['light'],
                              self.base.stamp(2))
        value = self.migrated(prior)
        row = value['history'][0]
        completed = value['history_bindings'][row['request_id']]['execution']
        self.assertEqual(completed['capacity_sha256'], row['target_sha256'])
        self.assertIsNone(completed['generation'])
        reverse = profile_migration.permit(self.new,self.old,self.evidence,self.base.first,
            self.base.config,self.base.config,allow_capacity_change=True)
        restored = profile_migration.candidate(value,reverse,self.base.stamp(20000))
        self.assertEqual(restored['profiles'],prior['profiles'])
        self.assertEqual(restored['history'],prior['history'])
        self.assertEqual(restored['history_bindings'],value['history_bindings'])
        self.assertEqual(restored['clock']['logical_ns'],prior['clock']['logical_ns'])
        self.assertEqual(policy.permission(restored,'primary',self.base.stamp(20000))['reason'],
                         'profile_cooldown')

    def test_runtime_cannot_upgrade_format_or_mutate_historical_bindings(self):
        value=self.migrated()
        with self.assertRaises(ValueError):policy.transition(self.base.pending,value)
        expected=policy.resume(value,self.base.stamp(10001))
        policy.transition(value,expected)
        row=value['pending']['request_id']
        for mutate in [lambda v:v['history_bindings'][row].update(registry_sha256='f'*64),
                       lambda v:v.update(history_bindings={}),
                       lambda v:v['history'][0].update(target_sha256=v['profiles']['light'])]:
            bad=copy.deepcopy(expected);mutate(bad)
            with self.assertRaises((ValueError,KeyError)):policy.transition(value,bad)

    def test_explicit_residency_reduction_does_not_shorten_existing_wait(self):
        prior = policy.finish(self.base.pending,8,self.base.pending['profiles']['light'],
                              self.base.stamp(2))
        config = dict(self.base.config,minimum_light_seconds=60,cooldown_seconds=60)
        authorization = profile_migration.permit(self.old,self.new,self.base.first,
            self.evidence,self.base.config,config,allow_capacity_change=True)
        value = profile_migration.candidate(prior,authorization,self.base.stamp(10000))
        self.assertEqual(value['not_before_ns'],prior['light_since_ns']+
                         self.base.config['minimum_light_seconds']*ledger.SECOND)
        self.assertEqual(policy.permission(value,'primary',self.base.stamp(10060))['reason'],
                         'profile_cooldown')

    def test_new_switch_preserves_foreign_history_then_expires_matching_bindings(self):
        value=self.migrated()
        done=policy.finish(value,8,value['profiles']['light'],self.base.stamp(10001))
        second=policy.prepare(done,'primary','sustained_memory_recovery',str(uuid.uuid4()),8,
                              self.base.stamp(10302))
        policy.transition(done,second)
        self.assertEqual(second['history'][0],value['history'][0])
        self.assertEqual(len(second['history_bindings']),2)
        finished=policy.finish(second,9,second['profiles']['primary'],self.base.stamp(10303))
        blocked=policy.permission(finished,'light',self.base.stamp(10604))
        self.assertEqual(blocked['reason'],'profile_hour_budget')
        third=policy.prepare(finished,'light','sustained_memory_pressure',str(uuid.uuid4()),9,
                             self.base.stamp(10303+86400))
        policy.transition(finished,third)
        self.assertEqual(len(third['history']),1)
        self.assertEqual(set(third['history_bindings']),{third['pending']['request_id']})

    def old_package(self):
        fixture=self.base.fixture
        path=fixture.directory/'qualifications/registry.json'
        receipt=fixture.prepare(recovery_profiles=path)
        package=Path(receipt['package'])
        source=package/profile_compat.SOURCE
        source.chmod(0o600)
        source.write_bytes((ROOT/'tests/fixtures/recovery/profile_policy_v1.py.txt').read_bytes())
        source.chmod(0o400)
        manifest=bundle.engine.read(package/'manifest.json')
        for name in set(bundle.SOURCES)-set(bundle.V12_SOURCES):
            del manifest['files']['source/'+name]
            (package/'source'/name).unlink()
        manifest.update(version=12,profile='q4t-service-recovery-v12')
        manifest['files'][profile_compat.SOURCE]=bundle.engine.sha(source)
        bundle.engine.save(package/'manifest.json',manifest)
        sha=bundle.engine.sha(package/'manifest.json')
        (package/'bin/q4t').chmod(0o700)
        (package/'bin/q4t').write_bytes(bundle.launcher(sha,manifest['python']['path']))
        (package/'bin/q4t').chmod(0o500)
        bundle.verify(package,sha)
        return package,sha

    def test_legacy_reader_adapter_seals_one_delta_and_preserves_current_journal(self):
        original,sha=self.old_package()
        before={str(p.relative_to(original)):p.read_bytes() for p in original.rglob('*') if p.is_file()}
        out=self.base.fixture.directory/'adapted'
        receipt=profile_compat.prepare(original,sha,out)
        self.assertFalse(receipt['business_acceptance'])
        self.assertEqual(before,{str(p.relative_to(original)):p.read_bytes()
                                 for p in original.rglob('*') if p.is_file()})
        state=self.base.fixture.directory/'state';state.mkdir(mode=0o700)
        binding=access_store.binding(state,str(uuid.uuid4()))
        profile_store.initialize_bound(binding,self.base.initial)
        profile_store.execute(binding,dict(sequence=1,op='commit',request_id=str(uuid.uuid4()),
            expected_revision=0,candidate=self.base.pending))
        class AuthorizedStore(profile_store.Store):
            contract=profile_migration.AuthorizedContract(self.authorization)
        value=self.migrated()
        with access_store.open_bound(binding,AuthorizedStore) as writer:
            writer.transact(str(uuid.uuid4()),1,value)
        raw=(state/'profile-selection/policy.json').read_bytes()
        code=('import json,sys; sys.path.insert(0,sys.argv[1]); '
              'from tools.serve_recovery import profile_store,profile_policy; '
              'assert profile_policy.__file__.startswith(sys.argv[1]); '
              'print(json.dumps(profile_store.execute(json.loads(sys.argv[2]),'
              '{"sequence":1,"op":"load"})))')
        for package,expected in [(original,1),(out,0)]:
            result=subprocess.run([sys.executable,'-I','-B','-c',code,
                str(package/'source'),json.dumps(binding)],cwd=package,
                capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,expected,result.stderr)
            if expected==0:self.assertEqual(json.loads(result.stdout),value)
            else:self.assertIn('LedgerError',result.stderr)
        self.assertEqual(raw,(state/'profile-selection/policy.json').read_bytes())
        # The unchanged old store executes a write through the narrow policy
        # adapter too; its current journal is never copied from a backup.
        code=('import json,sys; sys.path.insert(0,sys.argv[1]); '
              'from tools.serve_recovery import profile_store,profile_policy,ledger; '
              'b=json.loads(sys.argv[2]); v=profile_store.execute(b,{"sequence":1,"op":"load"}); '
              'c=profile_policy.finish(v,8,v["profiles"]["light"],'
              'ledger.Stamp(v["clock"]["boot_id"],v["clock"]["monotonic_ns"])); '
              'print(json.dumps(profile_store.execute(b,{"sequence":2,"op":"commit",'
              '"request_id":sys.argv[3],"expected_revision":v["revision"],"candidate":c})))')
        result=subprocess.run([sys.executable,'-I','-B','-c',code,
            str(out/'source'),json.dumps(binding),str(uuid.uuid4())],cwd=out,
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        actual=json.loads(result.stdout)
        self.assertEqual(actual['history'],value['history'])
        self.assertEqual(actual['history_bindings'][value['pending']['request_id']]['execution']
                         ['capacity_sha256'],value['profiles']['light'])
        manifest=bundle.engine.read(out/'manifest.json')
        manifest['quota_enforced']=not manifest['quota_enforced']
        bundle.engine.save(out/'manifest.json',manifest)
        with self.assertRaises(ValueError):
            profile_compat.verify(original,sha,out,bundle.engine.sha(out/'manifest.json'))

    def test_adapter_refuses_output_inside_original_before_creating_files(self):
        original,sha=self.old_package();out=original/'unsafe'
        with self.assertRaisesRegex(ValueError,'inside original'):
            profile_compat.prepare(original,sha,out)
        self.assertFalse(out.exists())


if __name__=='__main__':unittest.main()
