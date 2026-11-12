"""CPU previous-boot ledger fixtures; never reboot the host or query a GPU."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery import access_store, deployment, ledger, lifecycle
from tools.serve_recovery import outer_instance, outer_policy, outer_reconcile, outer_store, store
from tools.serve_recovery.test_outer_reconcile import cycle, read_core, read_outer, transact


class RebootTests(unittest.TestCase):
    def test_another_boot_continues_the_exact_existing_cleanup_checkpoint(self):
        self.setup_state(); middle = ledger.Stamp(str(uuid.uuid4()), self.now.monotonic_ns)
        outer_store.commit_once(self.binding, 'prepare_reboot', dict(core=self.before,
            ticket=self.outer_before['pending']['ticket']), clock=lambda: middle)
        checkpoint = read_outer(self.binding)['pending']['cleanup']
        self.assertEqual(checkpoint['boot_id'], middle.boot_id)
        result = self.reboot(); self.preserve()
        self.assertEqual(result['observed_boot_id'], store.stamp().boot_id)

    def setup_state(self, *, same_boot=False, current_slots=False, stopped=True):
        base=ROOT/'.q4t-work/serve-outer-recovery-20261112/reboot-fixtures'
        base.mkdir(mode=0o700,parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=base);self.addCleanup(temporary.cleanup)
        self.path=Path(temporary.name);self.sid=str(uuid.uuid4());self.digest='d'*64
        self.now=store.stamp();self.old=ledger.Stamp(
            self.now.boot_id if same_boot else str(uuid.uuid4()),self.now.monotonic_ns)
        self.desc=dict(version=1,service_id=self.sid,package=str(self.path/'sealed'),
                       manifest_sha256=self.digest)
        deployment.write_descriptor(self.path,self.desc)
        # Explicit historical CPU data, not invented observations of live tasks.
        old_owner=dict(pid=123,start_ticks=456,boot_id=self.old.boot_id)
        with store.Store(self.path,self.sid,initialize=True,clock=lambda:self.old) as core:
            core.require_service_contract();transact(core,'desired',state='RUNNING')
            cycle(core,old_owner)
        self.binding=access_store.binding(self.path,self.sid)
        outer_store.initialize_bound(self.binding,outer_policy.initial(self.sid,self.old))
        outer_store.commit_once(self.binding,'configure',dict(enabled=True),clock=lambda:self.old)
        outer_store.commit_once(self.binding,'request',dict(core=read_core(self.path,self.sid),
            ticket=str(uuid.uuid4()),manifest_sha256=self.digest,cleanup_verified=True,identity_valid=True,
            custody=dict(supervisor=old_owner,instance_token=str(uuid.uuid4()),tree_scope=str(uuid.uuid4()))),
            clock=lambda:self.old)
        stamp=self.now if current_slots else self.old
        with store.Store(self.path,self.sid,clock=lambda:stamp) as core:
            cycle(core,dict(old_owner,boot_id=stamp.boot_id),leave=True)
            transact(core,'fault',generation=core.state['generations']['model'],fingerprint='f'*64,unknown=False)
            transact(core,'quarantine',reason='device_failure')
            if stopped:transact(core,'desired',state='STOPPED')
        self.before=read_core(self.path,self.sid);self.outer_before=read_outer(self.binding)

    def reboot(self):
        with outer_instance.Instance(self.path,self.sid) as instance:
            return outer_reconcile.reboot(instance)

    def preserve(self, *, desired=None):
        after=read_core(self.path,self.sid);outer=read_outer(self.binding)
        for key in ('generations','quarantine','attempts','failures','not_before','circuit_until'):
            self.assertEqual(after[key],self.before[key],key)
        self.assertEqual(after['desired'],desired or self.before['desired'])
        self.assertFalse(any(after['active'].values()))
        self.assertEqual(outer['total'],self.outer_before['total'])
        self.assertEqual(outer['starts'],self.outer_before['starts'])
        self.assertIsNone(outer['pending'])
        return after,outer

    def test_original_boot_slots_retire_without_clearing_stop_faults_or_charges(self):
        self.setup_state();result=self.reboot();self.preserve()
        self.assertEqual(result['mode'],'previous_boot')
        self.assertEqual(result['observed_boot_id'],store.stamp().boot_id)
        self.assertEqual([row['role'] for row in result['retired']],['model','controller','gateway'])
        self.assertFalse(result['device_authorized']);self.assertFalse(result['model_actions'])

    def test_same_boot_cannot_be_substituted_for_actual_tree_proof(self):
        self.setup_state(same_boot=True)
        with self.assertRaisesRegex(ValueError,'previous-boot'):self.reboot()
        self.assertEqual(read_core(self.path,self.sid),self.before)
        self.assertEqual(read_outer(self.binding),self.outer_before)

    def test_current_boot_slots_are_not_retired_as_old_processes(self):
        self.setup_state(current_slots=True)
        with self.assertRaisesRegex(ValueError,'original slot boot proof'):self.reboot()
        self.assertEqual(read_core(self.path,self.sid),self.before)
        self.assertEqual(read_outer(self.binding),self.outer_before)

    def test_unrecorded_birth_after_clock_advance_requires_another_actual_proof(self):
        self.setup_state(stopped=False)
        value=lifecycle.request_stop(self.path,self.desc['package'],self.digest)
        self.assertTrue(value['stop_durable']);self.assertFalse(value['cleanup_confirmed'])
        before=read_core(self.path,self.sid)
        with self.assertRaisesRegex(ValueError,'original slot boot proof'):self.reboot()
        self.assertEqual(read_core(self.path,self.sid),before)
        self.assertEqual(read_outer(self.binding),self.outer_before)

    def test_persisted_checkpoint_resumes_partial_retirement_after_offline_stop(self):
        self.setup_state(stopped=False);original=os.replace
        def fail(src,dst,**kwargs):
            if src==store.PENDING:raise OSError('injected reboot core pre-rename')
            return original(src,dst,**kwargs)
        with mock.patch.object(os,'replace',side_effect=fail):
            with self.assertRaises(OSError):self.reboot()
        checkpoint=read_outer(self.binding)['pending']['cleanup']
        self.assertIsNotNone(checkpoint);self.assertTrue((self.path/store.PENDING).exists())
        self.assertTrue(lifecycle.request_stop(self.path,self.desc['package'],self.digest)['stop_durable'])
        after_stop=read_core(self.path,self.sid)
        self.assertIsNone(after_stop['active']['model'])
        self.assertEqual(after_stop['clock']['boot_id'],self.now.boot_id)
        result=self.reboot();self.preserve(desired='STOPPED')
        self.assertEqual([row['role'] for row in result['retired']],['controller','gateway'])

    def test_lost_final_receipt_does_not_repeat_retirement_or_refill_outer_budget(self):
        self.setup_state();original=outer_store.Store.transact
        def lost(writer,request,revision,candidate):
            result=original(writer,request,revision,candidate)
            if candidate['last']['operation']=='retired':raise OSError('lost final reboot receipt')
            return result
        with mock.patch.object(outer_store.Store,'transact',new=lost):
            with self.assertRaises(OSError):self.reboot()
        after,outer=self.preserve()
        self.assertEqual(self.reboot()['mode'],'no_pending')
        self.assertEqual(read_core(self.path,self.sid),after)
        self.assertEqual(read_outer(self.binding),outer)

    def test_cleanup_checkpoint_rejects_new_generations_and_replaced_tickets(self):
        self.setup_state()
        pending=self.outer_before['pending']
        prepared=outer_policy.change(self.outer_before,'prepare_reboot',
            dict(ticket=pending['ticket'],core=self.before),self.now)['pending']
        outer_reconcile.reboot_checkpoint(self.before,prepared,self.now.boot_id)
        changed=deepcopy(self.before);changed['generations']['model']+=1
        with self.assertRaisesRegex(ValueError,'checkpoint'):
            outer_reconcile.reboot_checkpoint(changed,prepared,self.now.boot_id)
        changed=deepcopy(self.before);changed['active']['gateway']['ticket']=str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError,'slot was replaced'):
            outer_reconcile.reboot_checkpoint(changed,prepared,self.now.boot_id)


if __name__=='__main__':unittest.main(module=None)
