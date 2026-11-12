"""Read-only fallback projection with real journals and synthetic CPU payloads."""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from tools.serve_host import native
from tools.serve_recovery import bundle, control, deployment, ledger, lifecycle
from tools.serve_recovery import profile_manager, test_version_registration
from tools.serve_recovery import version_launch, version_runtime, version_store

ROOT=Path(__file__).resolve().parents[2]


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.base=test_version_registration.RegistrationTests()
        self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.install();self.selected=self.base.consume()
        self.state,self.bound=self.base.state,self.base.binding
        self.desc=deployment.read_descriptor(self.state)
        # The shared fixture deliberately mocks engine verification. Supply
        # distinct executable inodes/metadata, not runnable model binaries.
        for name,item in self.base.registration['payloads'].items():
            package=Path(item['descriptor']['package'])
            raw=b'cpu engine' if name=='fallback' else b'cpu changed engine version'
            (package/'engine/q4t').write_bytes(raw)
            self.assertEqual(bundle.engine.sha(package/'engine/q4t'),item['binding']['binary_sha256'])
            bundle.engine.save(package/'engine/q4t.release.json',dict(chat_protocol=dict(
                observability=dict(version=1 if name=='fallback' else 2))))
        self.binding=native.bind(self.desc['package'],self.desc['manifest_sha256'],self.state)
        self.live=json.loads((ROOT/'docs/evidence/serve-progress-business-20261109/'
            'business-02/execution/ready-2.json').read_bytes())
        item=self.base.registration['payloads']['fallback']
        self.live.update(service_id=self.desc['service_id'],
            model_binary_sha256=item['binding']['binary_sha256'],
            model_protocol_sha256=item['binding']['protocol_sha256'],
            version_recovery=dict(enabled=True,root_manifest_sha256=self.desc['manifest_sha256'],
                cycle_id=self.selected['registry']['cycle_id'],selected='fallback',launch_payload='fallback',
                payload_manifest_sha256=item['descriptor']['manifest_sha256'],fallback_consumed=True,
                protected=False,pending=False,payload_prepared=True,state_available=True,
                last_fault=self.selected['switch']['fault'],reason='',worker_pid=12345))
        self.before={n:(self.state/n).read_bytes() for n in
            ['ledger.json','version-selection/policy.json','profile-selection/policy.json']}

    def collect(self, **options):
        arguments=dict(
            request=lambda *_a,**_k:self.live,check_process=lambda *_a,**_k:None,
            resources=lambda _pid:dict(rss_bytes=100),runtime=lambda _binding,_pid:{})
        arguments.update(options)
        return native.collect(self.binding,**arguments)

    def preserved(self):
        for name,raw in self.before.items():self.assertEqual((self.state/name).read_bytes(),raw,name)

    def fallback(self):
        return version_runtime.prepare(self.bound,self.selected,dict(
            root_manifest_sha256=self.desc['manifest_sha256'],payload='fallback',
            cleanup_complete=True,lease_continuous=True,roles_retired=True))

    def test_actual_fallback_inode_and_metrics_contract_are_checked_before_and_after_collection(self):
        checked=[];observed=[]
        result=self.collect(check_process=lambda pid,_start,exe,_ctx,**_k:checked.append((pid,exe)),
            runtime=lambda binding,_pid:observed.append(binding) or {})
        self.assertTrue(result['business_ready']);self.assertTrue(result['sources']['guardian'])
        expected=native.inode(Path(self.base.fallback['package'])/'engine/q4t')
        self.assertNotEqual(expected,self.binding['engine'])
        self.assertEqual([exe for pid,exe in checked if pid==self.live['model_pid']],[expected,expected])
        self.assertEqual(observed[0]['observation_version'],1)
        self.assertEqual(result['version_recovery']['last_fault'],self.selected['switch']['fault'])
        self.assertNotIn('worker_pid',result['version_recovery'])
        self.assertIn('q4t_service_version_fallback_consumed 1',native.metrics(result))
        self.assertNotIn(self.base.fallback['package'],json.dumps(result));self.preserved()

    def test_live_version_writer_lock_and_unknown_commit_do_not_block_healthy_projection(self):
        with version_store.open_bound(self.bound):
            path=self.state/'version-selection/policy.pending'
            path.write_bytes(b'unknown CPU interrupted commit');path.chmod(0o600)
            try:
                self.live['version_recovery']['state_available']=False
                result=self.collect()
                self.assertTrue(result['business_ready'])
                self.assertFalse(result['version_recovery']['state_available'])
                self.assertEqual(path.read_bytes(),b'unknown CPU interrupted commit')
            finally:path.unlink()
        self.preserved()

    def test_unknown_payload_and_wrong_actual_model_identity_are_not_trusted(self):
        original=copy.deepcopy(self.live)
        for field in ('payload_manifest_sha256','root_manifest_sha256','model_binary_sha256'):
            self.live=copy.deepcopy(original)
            target=self.live if field.startswith('model_') else self.live['version_recovery']
            target[field]='0'*64
            with self.subTest(field=field):
                self.assertFalse(self.collect()['sources']['guardian'])
        self.preserved()

    def test_payload_change_without_matching_generation_invalidates_collected_metrics(self):
        changed=copy.deepcopy(self.live)
        changed['version_recovery']['payload_manifest_sha256']=self.base.root['manifest_sha256']
        values=iter((self.live,changed))
        result=self.collect(request=lambda *_a,**_k:next(values))
        self.assertEqual(result['reason'],'identity_changed')
        self.assertIsNone(result['business_ready']);self.assertIsNone(result['runtime'])
        self.preserved()

    def test_stopped_version_and_profile_status_read_fallback_without_reconciling_or_resetting(self):
        self.fallback()
        self.before['profile-selection/policy.json']=(self.state/'profile-selection/policy.json').read_bytes()
        with mock.patch.object(control,'request',side_effect=OSError('no live CPU service')), \
                mock.patch.object(version_store,'open_bound',side_effect=AssertionError('observer writes')):
            value=version_launch.status(self.state,self.desc)
            self.assertEqual(value['launch_payload'],'fallback')
            result=profile_manager.status(self.state,self.desc,bundle.verify(
                self.desc['package'],self.desc['manifest_sha256']))
            self.assertEqual(result['profile_payload_manifest_sha256'],self.base.fallback['manifest_sha256'])
            check=lifecycle.check(self.desc['package'],self.desc['manifest_sha256'],self.state)
            self.assertEqual(check['status'],'PASS')
            self.assertEqual(check['adaptive_profiles']['root_manifest_sha256'],self.desc['manifest_sha256'])
        self.preserved()

    def test_stopped_observer_does_not_reconcile_pending_version_transaction(self):
        path=self.state/'version-selection/policy.pending'
        path.write_bytes(b'unknown pending observation');path.chmod(0o600)
        with mock.patch.object(control,'request',side_effect=OSError('stopped')), \
                self.assertRaisesRegex(ValueError,'commit unknown'):
            version_launch.status(self.state,self.desc)
        self.assertEqual(path.read_bytes(),b'unknown pending observation');self.preserved()

    def test_live_cli_checks_control_root_birth_before_accepting_retained_payload(self):
        with mock.patch.object(control,'request',return_value=self.live), \
                mock.patch.object(native,'process',side_effect=ValueError('CPU wrong root birth')) as checked, \
                self.assertRaisesRegex(ValueError,'wrong root birth'):
            version_launch.status(self.state,self.desc)
        checked.assert_called_once();self.preserved()


if __name__=='__main__':unittest.main()
