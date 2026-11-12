"""Actual source-bound live-root observation; no model/GPU work or host service."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery import access_store, control, deployment, ledger, outer_policy
from tools.serve_recovery import outer_observe, outer_store, store
from tools.serve_recovery import test_native_bundle, test_outer_parent, test_runtime


class ObserveTests(unittest.TestCase):
    cleanup=test_outer_parent.ParentTests.cleanup
    wait_guardian=test_outer_parent.ParentTests.wait_guardian
    leave_parent=test_outer_parent.ParentTests.leave_parent
    core=test_outer_parent.ParentTests.core

    def setUp(self):
        self.fixture=test_native_bundle.BundleTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        shutil.copyfile(test_runtime.BINARY,self.fixture.guardian)
        receipt=json.loads(self.fixture.build.read_text())
        receipt['guardian_sha256']=test_native_bundle.bundle.engine.sha(self.fixture.guardian)
        self.fixture.save(self.fixture.build,receipt)
        prepared=self.fixture.prepare();self.package=Path(prepared['package'])
        self.digest=prepared['manifest_sha256']
        with mock.patch.dict(os.environ,Q4T_GUARDIAN_BINARY=str(self.package/'bin/q4t-guardian')):
            test_outer_parent.ParentTests.setUp(self)
        self.state=Path(self.value['directory'])
        deployment.write_descriptor(self.state,dict(version=1,service_id=self.sid,
            package=str(self.package),manifest_sha256=self.digest))
        # CPU fixture bookkeeping for an already live root; this is not the
        # automatic outer dispatcher or evidence that its integration passed.
        now=store.stamp();binding=access_store.binding(self.state,self.sid)
        outer_store.initialize_bound(binding,outer_policy.initial(self.sid,now))
        outer_store.commit_once(binding,'configure',dict(enabled=True))
        core=ledger.new_state(self.sid,now);core['desired']='RUNNING'
        status=self.value['status']
        origin=dict(pid=status['outer_origin_pid'],start_ticks=status['outer_origin_start_ticks'],
                    boot_id=now.boot_id)
        attempt=outer_store.commit_once(binding,'request',dict(core=core,ticket=str(uuid.uuid4()),
            manifest_sha256=self.digest,cleanup_verified=True,identity_valid=True,
            custody=dict(supervisor=origin,instance_token=str(uuid.uuid4()),tree_scope=str(uuid.uuid4()))))
        outer_store.commit_once(binding,'spawned',dict(ticket=attempt['state']['pending']['ticket'],
            owner=dict(pid=self.pid,start_ticks=status['guardian_start_ticks'],boot_id=now.boot_id)))

    def lost(self):
        self.leave_parent()
        return test_outer_parent.until(lambda:(value if (value:=control.request(
            self.sid,'status'))['outer_owner_lost'] else None))

    def observer(self):return outer_observe.Observer(self.state,self.package,self.digest)

    def stable_identity(self,first,last):
        for name in ('guardian_pid','controller_pid','gateway_pid','model_pid','model_generation'):
            self.assertEqual(first[name],last[name])
        self.assertTrue(last['business_ready']);self.assertFalse(last['stop_requested'])

    def other_observer(self):
        code='''import json,sys
sys.path.insert(0,sys.argv[1])
from tools.serve_recovery import control
print(json.dumps(control.outer_observation(sys.argv[2],adopt=True)))
'''
        result=subprocess.run([sys.executable,'-I','-B','-c',code,str(ROOT),self.sid],
                              capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_verified_observer_restores_health_without_changing_any_core_journal(self):
        first=self.lost();before=self.core()
        outer=(self.state/'outer-recovery/policy.json').read_bytes()
        with self.observer() as observer:
            adopted=observer.sample(adopt=True);self.assertEqual(adopted['state'],'ATTACHED')
            value=observer.sample();self.assertEqual(value['state'],'ATTACHED')
            self.stable_identity(first,value['guardian']);self.assertEqual(value['guardian']['state'],'READY')
            self.assertFalse(value['tree_cleanup_verified']);self.assertFalse(value['model_actions'])
            self.assertEqual(self.core(),before)
            self.assertEqual((self.state/'outer-recovery/policy.json').read_bytes(),outer)

    def test_existing_observer_cannot_be_displaced_by_another_live_client(self):
        self.lost()
        with self.observer() as observer:
            self.assertEqual(observer.sample(adopt=True)['state'],'ATTACHED')
            denied=self.other_observer();self.assertFalse(denied['outer_observation_accepted'])
            self.assertEqual(denied['outer_owner_pid'],os.getpid())
            self.assertEqual(observer.sample()['state'],'ATTACHED')

    def test_observation_error_neither_stops_nor_retires_healthy_root(self):
        first=self.lost();before=self.core()
        with self.observer() as observer:
            self.assertEqual(observer.sample(adopt=True)['state'],'ATTACHED')
            with mock.patch.object(control,'outer_observation',side_effect=TimeoutError('CPU timeout')):
                result=observer.sample()
            self.assertEqual(result['state'],'UNKNOWN');self.assertFalse(result['root_exited'])
            self.assertFalse(result['tree_cleanup_verified']);self.assertEqual(self.core(),before)
            self.stable_identity(first,control.request(self.sid,'status'))
            self.assertEqual(observer.sample()['state'],'ATTACHED')

    def test_heartbeat_timeout_only_degrades_and_same_live_observer_can_recover(self):
        first=self.lost();before=self.core()
        with self.observer() as observer:
            self.assertEqual(observer.sample(adopt=True)['state'],'ATTACHED')
            stale=test_outer_parent.until(lambda:(value if (value:=control.request(
                self.sid,'status'))['outer_owner_lost'] else None),seconds=4)
            self.assertEqual(stale['state'],'READY_DEGRADED');self.stable_identity(first,stale)
            self.assertEqual(self.core(),before)
            recovered=observer.sample();self.assertEqual(recovered['state'],'ATTACHED')
            self.assertEqual(recovered['guardian']['state'],'READY')
            self.stable_identity(first,recovered['guardian'])

    def test_adopted_observer_process_exit_preserves_root_and_can_be_readopted(self):
        first=self.lost();receipt=self.other_observer()
        self.assertTrue(receipt['outer_observation_accepted'])
        lost=test_outer_parent.until(lambda:(value if (value:=control.request(
            self.sid,'status'))['outer_owner_lost'] else None))
        self.stable_identity(first,lost)
        with self.observer() as observer:
            self.assertEqual(observer.sample(adopt=True)['state'],'ATTACHED')
            self.assertEqual(self.core()['desired'],'RUNNING')

    def test_actual_root_exit_is_distinct_from_unknown_observation_and_tree_cleanup(self):
        self.lost()
        with self.observer() as observer:
            self.assertEqual(observer.sample(adopt=True)['state'],'ATTACHED')
            self.assertTrue(control.request(self.sid,'stop',timeout=4)['cleanup_confirmed'])
            self.wait_guardian();self.assertTrue(observer.exited())
            result=observer.sample();self.assertEqual(result['state'],'EXITED')
            self.assertTrue(result['root_exited']);self.assertFalse(result['tree_cleanup_verified'])

    def test_changed_release_identity_cannot_adopt_or_mutate_live_core(self):
        first=self.lost();before=self.core()
        desc=deployment.read_descriptor(self.state)
        deployment.write_descriptor(self.state,dict(desc,manifest_sha256='b'*64))
        with self.assertRaisesRegex(ValueError,'identity|deployment'):
            self.observer()
        self.assertEqual(self.core(),before)
        self.stable_identity(first,control.request(self.sid,'status'))


if __name__=='__main__':unittest.main(module=None)
