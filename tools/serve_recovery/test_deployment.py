"""STOPPED deployment CPU transactions; synthetic engine, real files and locks."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest import mock

from tools.serve_host import native
from tools.serve_recovery import access_policy, access_store, bundle, deployment as d
from tools.serve_recovery import ledger, quota_policy, quota_store, retention, service, store, usage_store
from tools.serve_recovery import test_native_bundle, test_usage_store


def save(path, value):
    path.write_bytes(ledger.canonical(value));path.chmod(0o600)


class EmbeddingTargetTests(unittest.TestCase):
    def target(self, previous, following):
        base = dict(ingress_mode='native', version=11, repository='/repo',
                    lock_path='/lock', engine_manifest_sha256='sha',
                    files={'source/tools/serve_recovery/deployment.py': 'sha'})
        cores = [dict(model_dir='/model', settings=s) for s in (previous, following)]
        with mock.patch.object(bundle, 'verify', return_value=base), \
             mock.patch.object(bundle.engine, 'verify', side_effect=cores):
            return d.check_target(dict(package='/old', manifest_sha256='a'),
                                  dict(package='/new', manifest_sha256='b'))

    def test_embedding_deployment_addition_and_asset_relocation_keep_identity(self):
        value = dict(bundle.engine.NATIVE_EMBEDDING, directory='/assets/old')
        self.target({}, dict(native_embedding=value))
        self.target(dict(native_embedding=value), dict(native_embedding=dict(
            value, directory='/assets/new')))

    def test_embedding_deployment_cannot_drop_accepted_model_on_rollback(self):
        value = dict(bundle.engine.NATIVE_EMBEDDING, directory='/assets')
        for selected in [None, dict(value, revision='changed'),
                         dict(value, weight_sha256='changed')]:
            with self.subTest(selected=selected), self.assertRaisesRegex(
                    ValueError, 'remove or change accepted embedding'):
                self.target(dict(native_embedding=value),
                            {} if selected is None else dict(native_embedding=selected))


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_bundle.BundleTests()
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        receipt = self.fixture.prepare()
        self.package = Path(receipt['package']);self.digest = receipt['manifest_sha256']
        self.legacy = self.fixture.directory/'legacy';shutil.copytree(self.package, self.legacy)
        value = bundle.engine.read(self.legacy/'manifest.json')
        names = {*('source/'+n for n in bundle.V6_SOURCES), 'gateway-policy.json',
                 'native-build.json', *('bin/'+n for n in bundle.EXECUTABLES)}
        value.update(version=6, profile='q4t-service-recovery-v6',
                     files={n:h for n,h in value['files'].items() if n in names})
        bundle.engine.save(self.legacy/'manifest.json', value)
        self.old_digest = bundle.engine.sha(self.legacy/'manifest.json')
        (self.legacy/'bin/q4t').chmod(0o700)
        (self.legacy/'bin/q4t').write_bytes(bundle.launcher(self.old_digest, value['python']['path']))
        (self.legacy/'bin/q4t').chmod(0o500)
        self.state = self.fixture.directory/'state'
        result = service.initialize(self.legacy, self.old_digest, self.state)
        self.sid = result['service_id'];self.binding = access_store.binding(self.state, self.sid)
        self.request = str(uuid.uuid4())

    def switch(self, **kwargs):
        return d.switch(self.state, self.package, self.digest, expected=self.old_digest,
                        request_id=self.request, **kwargs)

    def core(self):return json.loads((self.state/'ledger.json').read_bytes())['state']

    def test_legacy_upgrade_and_replay_preserve_core_identity_and_all_budgets(self):
        before = self.core();result = self.switch()
        self.assertEqual(result['current']['version'], 2);self.assertEqual(self.core(), before)
        self.assertEqual(result['current']['service_id'], self.sid)
        self.assertFalse(result['automatic_start']);self.assertFalse(result['model_actions'])
        for writer in d.FAMILIES.values():
            with access_store.open_bound(self.binding, writer) as journal:
                self.assertEqual(journal.head['state']['policy']['revision'], 0)
        self.assertTrue(self.switch()['replayed'])
        d.ready(self.state, d.read_descriptor(self.state))
        with self.assertRaises(ValueError):service.descriptor(self.state, self.legacy, self.old_digest)
        observed = native.read_ledger(dict(state=str(self.state), descriptor=d.read_descriptor(self.state)))
        self.assertEqual(observed['desired'], 'STOPPED')

    def test_v8_v9_round_trip_preserves_established_client_state(self):
        self.switch()
        previous = self.fixture.directory/'v8'
        shutil.copytree(self.package, previous)
        value = bundle.engine.read(previous/'manifest.json')
        value.update(version=8, profile='q4t-service-recovery-v8')
        bundle.engine.save(previous/'manifest.json', value)
        digest = bundle.engine.sha(previous/'manifest.json')
        (previous/'bin/q4t').chmod(0o700)
        (previous/'bin/q4t').write_bytes(bundle.launcher(digest, value['python']['path']))
        before = {name:(self.state/writer.directory_name/'policy.json').read_bytes()
                  for name,writer in d.FAMILIES.items()}
        core = self.core()
        with mock.patch.object(service.subprocess, 'Popen') as start:
            # First select v8 as a deployment; rollback must never invent
            # history for a package which has not previously been selected.
            selected = d.switch(self.state, previous, digest, expected=self.digest,
                                request_id=str(uuid.uuid4()))
            forward = d.switch(self.state, self.package, self.digest, expected=digest,
                               request_id=str(uuid.uuid4()))
            backward = d.switch(self.state, previous, digest, expected=self.digest,
                                request_id=str(uuid.uuid4()), rollback=True)
            start.assert_not_called()
        for receipt in (selected, forward, backward):
            self.assertFalse(receipt['model_actions'])
            self.assertFalse(receipt['automatic_start'])
        self.assertEqual(self.core(), core)
        for name,writer in d.FAMILIES.items():
            self.assertEqual(before[name], (self.state/writer.directory_name/'policy.json').read_bytes())
        d.ready(self.state, d.read_descriptor(self.state))

    def test_rollback_keeps_rotations_revocations_dynamic_limits_and_usage(self):
        self.switch()
        with access_store.open_bound(self.binding) as journal:
            previous = journal.head['state']['policy']
            candidate = access_policy.from_keys([dict(id='alpha', api_keys=['C'*40]),
                                                 dict(id='beta', api_keys=[])], previous)
            journal.transact(str(uuid.uuid4()), previous['revision'], candidate)
        with quota_store.open_bound(self.binding) as journal:
            previous = journal.head['state']['policy'];clients = copy.deepcopy(previous['clients'])
            clients[0]['requests_per_minute'] = 1
            journal.transact(str(uuid.uuid4()), previous['revision'], quota_policy.desired(previous, clients))
        owner = test_usage_store.owner()
        usage_store.execute(self.binding, dict(sequence=1, op='begin', generation=1, owner=owner), now_ns=100)
        usage_store.execute(self.binding, dict(sequence=2, op='seal', generation=1, owner=owner,
                                              snapshot=test_usage_store.snapshot()), now_ns=200)
        before = {name:(self.state/writer.directory_name/'policy.json').read_bytes()
                  for name,writer in d.FAMILIES.items()}
        second = self.fixture.directory/'second';shutil.copytree(self.package, second)
        d.switch(self.state, second, self.digest, expected=self.digest, request_id=str(uuid.uuid4()))
        result = d.switch(self.state, self.package, self.digest, expected=self.digest,
                          request_id=str(uuid.uuid4()), rollback=True)
        self.assertTrue(result['switched'])
        for name,writer in d.FAMILIES.items():
            self.assertEqual(before[name], (self.state/writer.directory_name/'policy.json').read_bytes())
        with self.assertRaisesRegex(ValueError, 'target must preserve'):
            d.switch(self.state, self.legacy, self.old_digest, expected=self.digest,
                     request_id=str(uuid.uuid4()), rollback=True)
        d.ready(self.state, d.read_descriptor(self.state))

    def test_before_and_after_descriptor_rename_resume_same_transaction(self):
        original = d.write_descriptor
        for after in (False, True):
            with self.subTest(after=after):
                if after:
                    target = self.fixture.directory/'second';shutil.copytree(self.package, target)
                    expected = self.digest;request = str(uuid.uuid4())
                else:target = self.package;expected = self.old_digest;request = self.request
                def fail(state, value):
                    if after:original(state, value)
                    raise OSError('CPU descriptor interruption')
                with mock.patch.object(d, 'write_descriptor', side_effect=fail), self.assertRaises(OSError):
                    d.switch(self.state, target, self.digest, expected=expected, request_id=request)
                self.assertEqual(d.status(self.state)['phase'], 'prepared')
                with self.assertRaises(ValueError):d.ready(self.state, d.read_descriptor(self.state))
                with mock.patch.object(service, 'acquire_lease') as lease, self.assertRaises(ValueError):
                    desc = d.read_descriptor(self.state)
                    service.launch(desc['package'], desc['manifest_sha256'], self.state)
                lease.assert_not_called()
                result = d.switch(self.state, target, self.digest, expected=expected, request_id=request)
                self.assertTrue(result['switched']);self.assertTrue(d.status(self.state)['ready'])

    def test_partial_unpublished_bootstrap_recovers_but_published_loss_never_resets(self):
        original = store.Store.write_all
        triggered = False
        def short(fd, raw):
            nonlocal triggered
            if not triggered and b'q4t-client-access-store-v1' in raw:
                triggered = True;os.write(fd, raw[:17]);raise OSError('CPU bootstrap short write')
            return original(fd, raw)
        with mock.patch.object(store.Store, 'write_all', side_effect=short), self.assertRaises(OSError):self.switch()
        self.assertTrue(triggered);self.assertFalse((self.state/access_store.MARKER).exists())
        self.switch()
        shutil.rmtree(self.state/'client-access')
        with self.assertRaises(FileNotFoundError):
            d.switch(self.state, self.package, self.digest, expected=self.digest, request_id=str(uuid.uuid4()))
        self.assertFalse((self.state/'client-access').exists())

    def test_bootstrap_outer_marker_publication_can_resume_without_reseeding(self):
        original = os.replace;fired = False
        def rename(src, dst, **kwargs):
            nonlocal fired
            if not fired and Path(dst).name == 'client-limits.json':
                fired = True;raise OSError('CPU outer marker commit')
            return original(src, dst, **kwargs)
        with mock.patch.object(os, 'replace', side_effect=rename), self.assertRaises(OSError):self.switch()
        before = (self.state/'client-limits/policy.json').read_bytes()
        self.switch();self.assertEqual(before, (self.state/'client-limits/policy.json').read_bytes())

    def test_owned_locks_nonstopped_state_and_changed_request_refuse_switch(self):
        before = (self.state/'service.json').read_bytes()
        for name in ('deployment.lock', 'guardian.lock', 'writer.lock'):
            with d.claim(self.state, name=name), self.assertRaises(BlockingIOError):self.switch()
        self.assertEqual(before, (self.state/'service.json').read_bytes())
        with store.Store(self.state, self.sid) as core:
            value = copy.deepcopy(core.state);value['desired'] = 'RUNNING'
            core.commit(value)
        with self.assertRaisesRegex(ValueError, 'STOPPED'):self.switch()
        with store.Store(self.state, self.sid) as core:
            value = copy.deepcopy(core.state);value['desired'] = 'STOPPED';core.commit(value)
        self.switch()
        with self.assertRaisesRegex(ValueError, 'idempotency'):
            d.switch(self.state, self.package, self.digest, expected='a'*64, request_id=self.request)

    def test_bad_package_or_client_capacity_does_not_publish_pending_switch(self):
        with self.assertRaises(ValueError):
            d.switch(self.state, self.package, 'a'*64, expected=self.old_digest, request_id=self.request)
        self.assertFalse((self.state/d.Store.marker_name).exists())
        with mock.patch.object(d, 'consistent_clients', side_effect=ValueError('CPU incompatible capacity')):
            with self.assertRaises(ValueError):self.switch()
        self.assertEqual(d.status(self.state)['phase'], 'stable')
        d.ready(self.state, d.read_descriptor(self.state))
        self.assertNotIn('service_contract', json.loads((self.state/'ledger.json').read_bytes()))

    def test_file_capacity_blocks_old_package_until_explicit_legacy_migration(self):
        self.switch()
        previous = self.fixture.directory/'v10'
        shutil.copytree(self.package, previous)
        value = bundle.engine.read(previous/'manifest.json')
        value.update(version=10, profile='q4t-service-recovery-v10')
        bundle.engine.save(previous/'manifest.json', value)
        digest = bundle.engine.sha(previous/'manifest.json')
        (previous/'bin/q4t').chmod(0o700)
        (previous/'bin/q4t').write_bytes(bundle.launcher(digest, value['python']['path']))
        d.switch(self.state, previous, digest, expected=self.digest, request_id=str(uuid.uuid4()))
        d.switch(self.state, self.package, self.digest, expected=digest, request_id=str(uuid.uuid4()))
        with quota_store.open_bound(self.binding) as journal:
            before = journal.head['state']['policy']
            clients = [dict(c, max_file_body_bytes=512*1024**2+65536) for c in before['clients']]
            journal.transact(str(uuid.uuid4()), before['revision'], quota_policy.desired(before, clients))
        paths = ['service.json', 'ledger.json', *(
            writer.directory_name+'/policy.json' for writer in d.FAMILIES.values())]
        saved = {name: (self.state/name).read_bytes() for name in paths}
        with mock.patch.object(service.subprocess, 'Popen') as start:
            with self.assertRaisesRegex(ValueError, 'file client policy requires recovery profile v11'):
                d.switch(self.state, previous, digest, expected=self.digest,
                         request_id=str(uuid.uuid4()), rollback=True)
            start.assert_not_called()
        for name, raw in saved.items():self.assertEqual((self.state/name).read_bytes(), raw)
        self.assertEqual(d.status(self.state)['phase'], 'stable')
        # Use the same durable transaction path as limits-apply; the CLI
        # proposal and live C++ usage conservation are tested independently.
        with quota_store.open_bound(self.binding) as journal:
            before = journal.head['state']['policy']
            clients = copy.deepcopy(before['clients'])
            for client in clients:client['max_file_body_bytes'] = client['max_body_bytes']
            aligned = quota_policy.desired(before, clients)
            journal.transact(str(uuid.uuid4()), before['revision'], aligned)
            for client in clients:client.pop('max_file_body_bytes')
            legacy = quota_policy.desired(aligned, clients, policy_version=3)
            journal.transact(str(uuid.uuid4()), aligned['revision'], legacy)
        limits = (self.state/'client-limits/policy.json').read_bytes()
        result = d.switch(self.state, previous, digest, expected=self.digest,
                          request_id=str(uuid.uuid4()), rollback=True)
        self.assertFalse(result['model_actions']);self.assertFalse(result['automatic_start'])
        self.assertEqual((self.state/'client-limits/policy.json').read_bytes(), limits)
        for name in ('client-access/policy.json', 'client-usage/policy.json'):
            self.assertEqual((self.state/name).read_bytes(), saved[name])
        d.ready(self.state, d.read_descriptor(self.state))

    def test_readonly_observation_and_retention_protect_selected_and_rollback_packages(self):
        self.switch();second = self.fixture.directory/'second';shutil.copytree(self.package, second)
        d.switch(self.state, second, self.digest, expected=self.digest, request_id=str(uuid.uuid4()))
        records = {}
        paths = retention.dependencies(self.state, anchors=[self.state/'service.json'], records=records)
        for path in (self.package, second, self.legacy):self.assertIn(path, paths)
        self.assertIn(str(self.state/'service-deployment/policy.json'), records)
        self.assertEqual(retention.state_descriptor(self.state)[2]['version'], 2)

    def test_cli_pinned_target_upgrade_status_and_error_do_not_expose_keys(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = service.main(['deploy', '--state', str(self.state), '--request-id', self.request,
                '--expected-manifest-sha256', self.old_digest], package=self.package, digest=self.digest)
        self.assertEqual(code, 0, errors.getvalue())
        self.assertTrue(json.loads(output.getvalue())['switched'])
        for secret in ('A'*40, 'B'*40):self.assertNotIn(secret, output.getvalue()+errors.getvalue())

    def test_retention_lock_cannot_overlap_a_deployment_switch(self):
        with retention.claim(self.state):
            with self.assertRaises(BlockingIOError):self.switch()
        with d.claim(self.state):
            with self.assertRaises(BlockingIOError):
                with retention.claim(self.state):pass

    def test_actual_retained_legacy_store_cannot_grant_after_upgrade(self):
        path = os.environ.get('Q4T_LEGACY_STORE_SOURCE')
        if path is None:self.skipTest('requires explicitly pinned pre-deployment store source')
        spec = importlib.util.spec_from_file_location('q4t_old_store_cpu', path)
        old = importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
        with old.Store(self.state, self.sid):pass
        before = self.core();self.switch()
        with self.assertRaises(ValueError):old.Store(self.state, self.sid)
        self.assertEqual(self.core(), before)


if __name__ == '__main__':unittest.main()
