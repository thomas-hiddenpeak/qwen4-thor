"""Real STOPPED journal transactions with explicitly synthetic CPU packages."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_store, bundle, deployment, ledger
from tools.serve_recovery import profile_contract, profile_manager, profile_policy
from tools.serve_recovery import profile_compat
from tools.serve_recovery import profile_store, service, store
from tools.serve_recovery import test_native_bundle, test_profile_integration
from tools.serve_service import supervisor


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_bundle.BundleTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        # Each synthetic engine must keep its own original manifest. Returning
        # the latest fixture base for both packages would mask identity drift.
        bundle.engine.verify.side_effect = lambda p, _h: bundle.engine.read(
            Path(p)/'manifest.json')
        self.old, self.old_sha = self.prepare('old', legacy=True)
        self.fixture.binary.write_bytes(b'cpu changed engine version')
        self.fixture.base['files']['q4t'] = bundle.engine.sha(self.fixture.binary)
        self.fixture.base['protocol_sha256'] = 'd'*64
        self.new, self.new_sha = self.prepare('new')
        self.state = self.fixture.directory/'state'
        result = service.initialize(self.old, self.old_sha, self.state)
        self.binding = access_store.binding(self.state, result['service_id'])
        current = self.load()
        self.pending = profile_policy.prepare(current, 'light',
            'sustained_memory_pressure', str(uuid.uuid4()), 7,
            ledger.Stamp(current['clock']['boot_id'],
                         current['clock']['monotonic_ns']+ledger.SECOND))
        profile_store.execute(self.binding, dict(sequence=1, op='commit',
            request_id=str(uuid.uuid4()), expected_revision=0,
            candidate=self.pending))
        self.before = {n:(self.state/n).read_bytes() for n in
            ['ledger.json', *(w.directory_name+'/policy.json'
                              for w in deployment.FAMILIES.values())]}
        self.request = str(uuid.uuid4())

    def prepare(self, name, *, legacy=False, config=None, max_tokens=None,
                capability_extension=False):
        path, registry, records = test_profile_integration.evidence_files(self.fixture)
        if max_tokens is not None:
            primary = dict(registry['profiles']['primary'], max_tokens=max_tokens)
            self.fixture.base['settings']['profile'] = primary
            registry['profiles'] = profile_contract.profiles(primary)
        if max_tokens is not None or capability_extension:
            for kind, record in records.items():
                record['capacity'] = registry['profiles'][kind]
                record['configuration'].update(supervisor.expected_configuration(record['capacity']))
                record['configuration']['default_output_tokens'] = record['capacity']['max_tokens']
                if capability_extension:
                    record['capabilities']['chat']['refusal_output'] = dict(
                        profile='q4t-refusal-v1', chat_json=True, chat_sse=True)
                    record['capabilities']['template'].update(
                        message_profile='qwen3.8-openai-v4', renderer_source_sha256='e'*64)
                evidence = path.parent/(kind+'.json')
                bundle.engine.save(evidence, record)
                registry['qualifications'][kind] = profile_contract.qualification(kind,
                    record['capacity'], record['identity'], record['capabilities'],
                    record['configuration'], record['startup_memory'], bundle.engine.sha(evidence))
            bundle.engine.save(path, registry)
        receipt = self.fixture.prepare(recovery_profiles=path,
                                        recovery_profile_policy=config)
        package = self.fixture.directory/name
        shutil.move(receipt['package'], package)
        if legacy:
            # Exercise the exact v12 manifest/source inventory. Runtime policy
            # and store sources are unchanged from v12, not migration shims.
            value = bundle.engine.read(package/'manifest.json')
            extra = set(bundle.SOURCES)-set(bundle.V12_SOURCES)
            for name in extra:
                del value['files']['source/'+name]
                (package/'source'/name).unlink()
            value.update(version=12, profile='q4t-service-recovery-v12')
            bundle.engine.save(package/'manifest.json', value)
            digest = bundle.engine.sha(package/'manifest.json')
            (package/'bin/q4t').chmod(0o700)
            (package/'bin/q4t').write_bytes(bundle.launcher(digest, value['python']['path']))
            (package/'bin/q4t').chmod(0o500)
        digest = bundle.engine.sha(package/'manifest.json')
        bundle.verify(package, digest)
        return package, digest

    def load(self):
        return profile_store.execute(self.binding, dict(sequence=1, op='load'))

    def switch(self, **kwargs):
        return deployment.switch(self.state, self.new, self.new_sha,
            expected=self.old_sha, request_id=self.request, **kwargs)

    def assert_preserved(self, expected_revision):
        actual = self.load()
        self.assertEqual(actual['revision'], expected_revision)
        for name in ('pending', 'history', 'clock', 'selected', 'profiles',
                     'not_before_ns', 'light_since_ns', 'completed_generation'):
            self.assertEqual(actual[name], self.pending[name], name)
        for name, raw in self.before.items():
            self.assertEqual((self.state/name).read_bytes(), raw, name)
        return actual

    def test_upgrade_replay_and_v12_rollback_keep_original_pending_and_state(self):
        self.switch(); actual = self.assert_preserved(2)
        self.assertEqual(actual['registry_sha256'], profile_contract.digest(
            bundle.verify(self.new, self.new_sha)['recovery_profiles']))
        self.assertTrue(self.switch()['replayed'])
        self.assert_preserved(2)
        deployment.switch(self.state, self.old, self.old_sha,
            expected=self.new_sha, request_id=str(uuid.uuid4()), rollback=True)
        self.assert_preserved(3)
        # A separate interpreter imports the sealed v12 reader, not this tree.
        result = subprocess.run([sys.executable, '-B', '-c',
            'import json,sys; sys.path.insert(0,sys.argv[1]); '
            'from tools.serve_recovery import profile_store; '
            'assert profile_store.__file__.startswith(sys.argv[1]); '
            'print(json.dumps(profile_store.execute(json.loads(sys.argv[2]),'
            '{"sequence":1,"op":"load"})))',
            str(self.old/'source'), json.dumps(self.binding)],
            cwd=self.old, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.load())

    def test_qualified_feature_upgrade_replays_but_old_feature_loss_cannot_rollback(self):
        self.new, self.new_sha = self.prepare('extended', capability_extension=True)
        self.switch(); self.assert_preserved(2)
        self.assertTrue(self.switch()['replayed'])
        before = (self.state/'service-deployment/policy.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            deployment.switch(self.state, self.old, self.old_sha,
                expected=self.new_sha, request_id=str(uuid.uuid4()), rollback=True)
        self.assertEqual((self.state/'service-deployment/policy.json').read_bytes(), before)
        self.assert_preserved(2)

    def test_feature_upgrade_prepared_failure_resumes_exact_original_intent(self):
        self.new, self.new_sha = self.prepare('extended', capability_extension=True)
        with mock.patch.object(profile_manager, 'deploy', side_effect=OSError('before extension')):
            with self.assertRaises(OSError):
                self.switch()
        self.assertEqual(deployment.status(self.state)['phase'], 'prepared')
        self.assert_preserved(1)
        self.switch(); self.assert_preserved(2)
        self.assertTrue(self.switch()['replayed'])

    def test_prepared_intent_interruption_keeps_old_state_then_migrates_once(self):
        raw = (self.state/'profile-selection/policy.json').read_bytes()
        with mock.patch.object(profile_manager, 'deploy',
                side_effect=OSError('before migration')), self.assertRaises(OSError):
            self.switch()
        self.assertEqual(deployment.status(self.state)['phase'], 'prepared')
        self.assertEqual((self.state/'profile-selection/policy.json').read_bytes(), raw)
        with mock.patch.object(service, 'acquire_lease') as lease, self.assertRaises(ValueError):
            service.launch(self.old, self.old_sha, self.state)
        lease.assert_not_called()
        self.switch(); self.assert_preserved(2)

    def test_profile_rename_failure_requires_exact_deployment_and_recovers_once(self):
        original = os.replace
        def fail(src, dst, **kwargs):
            fd = kwargs.get('dst_dir_fd')
            if (str(src) == 'policy.pending' and fd is not None and
                    Path(os.readlink('/proc/self/fd/'+str(fd))).name == 'profile-selection'):
                raise OSError('profile before rename')
            return original(src, dst, **kwargs)
        with mock.patch.object(os, 'replace', side_effect=fail), self.assertRaises(OSError):
            self.switch()
        with self.assertRaises(ValueError): self.load()
        with self.assertRaisesRegex(ValueError, 'resume identity'):
            deployment.switch(self.state, self.new, self.new_sha,
                expected=self.old_sha, request_id=str(uuid.uuid4()))
        self.switch(); self.assert_preserved(2)
        self.assertFalse((self.state/'profile-selection/policy.pending').exists())

    def test_profile_directory_fsync_lost_receipt_replays_without_clock_credit(self):
        original = os.fsync
        def fail(fd):
            if (Path(os.readlink('/proc/self/fd/'+str(fd))).name == 'profile-selection' and
                    self.load_revision_unlocked() == 2):
                raise OSError('profile commit directory sync')
            return original(fd)
        with mock.patch.object(os, 'fsync', side_effect=fail), self.assertRaises(OSError):
            self.switch()
        with mock.patch.object(store, 'stamp', side_effect=AssertionError('new retry clock')):
            self.switch()
        self.assert_preserved(2)
        with profile_store.open_bound(self.binding) as writer:
            self.assertEqual(len(writer.head['state']['receipts']), 2)

    def load_revision_unlocked(self):
        return json.loads((self.state/'profile-selection/policy.json').read_bytes())[
            'state']['policy']['revision']

    def test_descriptor_and_publication_interruptions_resume_original_migration(self):
        original = deployment.write_descriptor
        for point in ('before_descriptor', 'after_descriptor', 'publication'):
            with self.subTest(point=point):
                target, digest = (self.new, self.new_sha)
                previous = deployment.read_descriptor(self.state)['manifest_sha256']
                if previous == self.new_sha: target, digest = self.old, self.old_sha
                request = str(uuid.uuid4())
                def call(**kwargs):
                    return deployment.switch(self.state, target, digest,
                        expected=previous, request_id=request, **kwargs)
                def fail(state, value):
                    if point == 'after_descriptor': original(state, value)
                    raise OSError(point)
                def publish(*_): raise OSError('publication')
                if point == 'publication':
                    with self.assertRaises(OSError): call(_publication=publish)
                else:
                    with mock.patch.object(deployment, 'write_descriptor', side_effect=fail), \
                         self.assertRaises(OSError): call()
                revision = self.load()['revision']
                self.assertEqual(deployment.status(self.state)['phase'], 'prepared')
                call(); self.assert_preserved(revision)
                self.assertTrue(deployment.status(self.state)['ready'])

    def test_explicit_policy_update_preserves_existing_cooldown_deadline(self):
        self.new, self.new_sha = self.prepare('shorter',
            config=dict(profile_policy.DEFAULTS, cooldown_seconds=60))
        self.switch(); actual = self.assert_preserved(2)
        self.assertEqual(actual['configuration']['cooldown_seconds'], 60)
        self.assertEqual(actual['not_before_ns'], self.pending['not_before_ns'])

    def test_qualification_tamper_is_rejected_before_preparing_deployment(self):
        path = self.new/'profile-light.json'
        path.chmod(0o600); path.write_bytes(b'changed qualification')
        before = (self.state/'service-deployment/policy.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'file identity'):
            self.switch()
        self.assertEqual(before, (self.state/'service-deployment/policy.json').read_bytes())
        self.assert_preserved(1)

    def test_unrelated_pending_migration_receipt_cannot_be_adopted(self):
        original = os.replace
        def fail(src, dst, **kwargs):
            fd = kwargs.get('dst_dir_fd')
            if (str(src) == 'policy.pending' and fd is not None and
                    Path(os.readlink('/proc/self/fd/'+str(fd))).name == 'profile-selection'):
                raise OSError('profile before rename')
            return original(src, dst, **kwargs)
        with mock.patch.object(os, 'replace', side_effect=fail), self.assertRaises(OSError):
            self.switch()
        path = self.state/'profile-selection/policy.pending'
        value = json.loads(path.read_bytes())
        value['state']['receipts'][-1]['id'] = str(uuid.uuid4())
        value['sha256'] = ledger.digest(value['state'])
        path.write_bytes(ledger.canonical(value))
        with self.assertRaisesRegex(ValueError, 'another deployment'): self.switch()
        self.assertEqual(self.load_revision_unlocked(), 1)
        self.assertTrue(path.exists())

    def test_capacity_upgrade_then_retained_adapted_rollback_preserves_old_intent(self):
        # Retain a real historical v1 representation reader in the old CPU
        # payload. All qualifications/engines remain explicitly synthetic.
        historical = self.fixture.directory/'old-v1-reader'
        shutil.copytree(self.old,historical)
        self.old = historical
        source = self.old/profile_compat.SOURCE
        source.chmod(0o600)
        source.write_bytes((Path(__file__).resolve().parents[2]/
            'tests/fixtures/recovery/profile_policy_v1.py.txt').read_bytes())
        source.chmod(0o400)
        manifest = bundle.engine.read(self.old/'manifest.json')
        manifest['files'][profile_compat.SOURCE] = bundle.engine.sha(source)
        bundle.engine.save(self.old/'manifest.json', manifest)
        new_old_sha = bundle.engine.sha(self.old/'manifest.json')
        (self.old/'bin/q4t').chmod(0o700)
        (self.old/'bin/q4t').write_bytes(bundle.launcher(new_old_sha,manifest['python']['path']))
        (self.old/'bin/q4t').chmod(0o500)
        # The predecessor was initialized before this fixture adjustment.
        # Reinitialize a separate private state; never edit its durable hashes.
        self.old_sha = new_old_sha
        self.state = self.fixture.directory/'history-state'
        result = service.initialize(self.old,self.old_sha,self.state)
        self.binding = access_store.binding(self.state,result['service_id'])
        current = self.load()
        self.pending = profile_policy.prepare(current,'light','sustained_memory_pressure',
            str(uuid.uuid4()),7,ledger.Stamp(current['clock']['boot_id'],
                                          current['clock']['monotonic_ns']+ledger.SECOND))
        profile_store.execute(self.binding,dict(sequence=1,op='commit',
            request_id=str(uuid.uuid4()),expected_revision=0,candidate=self.pending))
        unchanged = {name:(self.state/name).read_bytes() for name in
            ['ledger.json',*(w.directory_name+'/policy.json' for w in deployment.FAMILIES.values())]}
        self.new,self.new_sha = self.prepare('capacity-new',max_tokens=512)
        self.switch(); value = self.load()
        self.assertEqual(value['format'],profile_policy.HISTORY_FORMAT)
        self.assertEqual(value['pending'],self.pending['pending'])
        self.assertEqual(value['history'],self.pending['history'])
        before = (self.state/'service-deployment/policy.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'history adapter'):
            deployment.switch(self.state,self.old,self.old_sha,expected=self.new_sha,
                request_id=str(uuid.uuid4()),rollback=True)
        self.assertEqual(before,(self.state/'service-deployment/policy.json').read_bytes())
        adapted = profile_compat.prepare(self.old,self.old_sha,self.fixture.directory/'adapted')
        self.assertIsNone(profile_compat.retained_origin([],adapted['payload'],
                                                        adapted['payload_manifest_sha256']))
        deployment.switch(self.state,adapted['payload'],adapted['payload_manifest_sha256'],
            expected=self.new_sha,request_id=str(uuid.uuid4()),rollback=True)
        actual = self.load()
        self.assertEqual(actual['profiles'],self.pending['profiles'])
        self.assertEqual(actual['history'],self.pending['history'])
        self.assertEqual(actual['history_bindings'],value['history_bindings'])
        self.assertEqual(actual['clock'],self.pending['clock'])
        self.assertEqual(actual['revision'],3)
        for name,raw in unchanged.items():self.assertEqual((self.state/name).read_bytes(),raw)


if __name__ == '__main__': unittest.main()
