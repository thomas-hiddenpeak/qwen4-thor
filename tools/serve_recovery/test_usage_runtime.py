"""Real CPU quota coordinator/native/isolated-I/O combinations; no model work."""
import copy
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_store, controller, journal, quota_control, quota_policy
from tools.serve_recovery import usage_control, usage_policy, usage_runtime, usage_store
from tools.serve_recovery.test_gateway import config
from tools.serve_recovery.test_quota_admin import ROOT, Server
from tools.serve_recovery.test_quota_usage_admin import request


class UsageRuntimeTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.q4t-work/serve-access-20261109/usage-runtime-fixtures';base.mkdir(parents=True,exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(temp.cleanup)
        self.binding=access_store.binding(Path(temp.name),str(uuid.uuid4()))
        self.boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        usage_store.initialize_bound(self.binding)
        self.policy=quota_policy.runtime(quota_policy.from_base(config(Path(temp.name))['backend_policy']),'a'*64)
        for client in self.policy['clients']:client['requests_per_minute']=1
        self.offset=0

    def nanos(self):return time.monotonic_ns()+self.offset

    def runtime(self):
        value=usage_runtime.Recovery(self.binding,nanos=self.nanos)
        self.addCleanup(self.close,value);return value

    def close(self, value):
        end=time.monotonic()+3
        while not value.close() and time.monotonic()<end:time.sleep(.01)
        self.assertIsNone(value.worker.process)

    def model(self, generation):
        env=dict(os.environ,Q4T_SERVICE_ID=self.binding['service_id'],Q4T_MODEL_GENERATION=str(generation),
            Q4T_POLICY_PARENT_PID=str(os.getpid()),Q4T_POLICY_PARENT_START=str(journal.process_start(os.getpid())))
        model=Server(self.policy,mode='environment',env=env,
                     address=quota_control.address(self.binding['service_id'],generation))
        self.addCleanup(model.close)
        owner=dict(pid=model.process.pid,start_ticks=journal.process_start(model.process.pid),boot_id=self.boot)
        return model,owner

    def until(self, step, predicate, seconds=7):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            step()
            if predicate():return
            time.sleep(.01)
        self.fail('usage runtime deadline')

    def sealed(self):
        old,owner=self.model(1);runtime=self.runtime();runtime.attach(owner,1,0)
        self.until(runtime.step,lambda:runtime.restored)
        self.assertTrue(old.command('reserve',id='alpha')['ok'])
        self.assertTrue(runtime.freeze())
        self.until(runtime.step,lambda:old.status()['configured'] and runtime.channel.observed is not None)
        self.assertFalse(runtime.sealed)
        old.command('settle')
        self.until(runtime.step,lambda:runtime.sealed)
        self.assertEqual(runtime.frozen['clients'][0]['tokens'],'5')
        self.close(runtime)
        return old,runtime.frozen

    def test_real_drain_durable_consumption_and_restore_avoid_unnecessary_window(self):
        old,_=self.sealed()
        new,owner=self.model(2);runtime=self.runtime();runtime.attach(owner,2,self.nanos()+60_000_000_000)
        self.until(runtime.step,lambda:runtime.restored)
        self.assertEqual(runtime.mode,'exact')
        self.assertFalse(new.command('reserve',id='alpha')['ok'])
        self.assertTrue(new.command('reserve',id='beta')['ok']);new.command('settle')
        self.assertTrue(runtime.receipt)
        with usage_store.open_bound(self.binding) as writer:
            state=writer.head['state']['policy']
            self.assertEqual((state['generation'],state['phase']),(2,'active'))
        self.assertIsNone(old.process.poll());self.assertIsNone(new.process.poll())

    def test_crash_unknown_waits_guard_then_auxiliary_loss_keeps_native_quota(self):
        first,owner=self.model(1);runtime=self.runtime();runtime.attach(owner,1,0)
        self.until(runtime.step,lambda:runtime.restored)
        self.assertTrue(first.command('reserve',id='alpha')['ok']);first.command('settle')
        self.close(runtime)  # No sealed handoff; treat it as uncertain exit.
        new,owner=self.model(2);next_runtime=self.runtime()
        next_runtime.attach(owner,2,self.nanos()+60_000_000_000)
        self.until(next_runtime.step,lambda:next_runtime.prepared)
        self.assertEqual(next_runtime.mode,'unknown');self.assertFalse(next_runtime.restored)
        self.assertFalse(request(new,'usage_status')['usage']['recovered'])
        self.offset=61_000_000_000
        self.until(next_runtime.step,lambda:next_runtime.restored)
        pid=new.process.pid
        self.assertTrue(new.command('reserve',id='alpha')['ok']);new.command('settle')
        with mock.patch.object(next_runtime.worker,'step',side_effect=RuntimeError('CPU helper failed')):
            next_runtime.step()
        self.assertTrue(next_runtime.restored)
        self.assertFalse(new.command('reserve',id='alpha')['ok'])
        self.assertEqual(new.process.pid,pid);self.assertIsNone(new.process.poll())

    def test_missing_journal_falls_back_without_restart_or_false_durable_state(self):
        state=Path(self.binding['state_directory'])/'client-usage/policy.json';state.unlink()
        new,owner=self.model(2);runtime=self.runtime();runtime.attach(owner,2,self.nanos()+60_000_000_000)
        self.until(runtime.step,lambda:runtime.prepared)
        self.assertEqual(runtime.mode,'unknown');self.assertFalse(runtime.restored)
        self.offset=61_000_000_000
        self.until(runtime.step,lambda:runtime.restored)
        self.assertFalse(runtime.worker.applied());self.assertFalse(state.exists())
        self.assertTrue(new.command('reserve',id='alpha')['ok']);new.command('settle')
        self.assertFalse(new.command('reserve',id='alpha')['ok'])

    def test_explicit_snapshot_refusal_can_fall_back_only_after_guard_window(self):
        _,closed=self.sealed()
        # A validly encoded but incompatible policy snapshot must be refused
        # by the model. Use a separate, explicitly synthetic predecessor store.
        with usage_store.open_bound(self.binding) as writer:
            prior=writer.head['state']['policy']
            active=usage_policy.begin(prior,2,dict(prior['owner'],pid=prior['owner']['pid']+100),time.monotonic_ns())
            writer.transact(str(uuid.uuid4()),prior['revision'],active)
            wrong=copy.deepcopy(closed);wrong['policy_sha256']='f'*64;wrong['captured_ns']=str(time.monotonic_ns())
            sealed=usage_policy.seal(active,2,active['owner'],wrong)
            writer.transact(str(uuid.uuid4()),active['revision'],sealed)
        new,owner=self.model(3);runtime=self.runtime();runtime.attach(owner,3,self.nanos()+60_000_000_000)
        self.until(runtime.step,lambda:runtime.mode=='unknown' and runtime.prepared)
        self.assertFalse(runtime.restored)
        self.assertFalse(request(new,'usage_status')['usage']['recovered'])
        self.offset=61_000_000_000
        self.until(runtime.step,lambda:runtime.restored)
        self.assertIsNone(new.process.poll())

    def test_unanswered_restore_and_wrong_birth_never_authorize_reset(self):
        self.sealed();new,owner=self.model(2)
        channel=usage_control.Channel(new.address);self.addCleanup(channel.close)
        channel.request('usage_restore')
        self.assertFalse(channel.step(owner['pid'],owner['start_ticks']+1)['applied'])
        self.assertFalse(request(new,'usage_status')['usage']['recovered'])
        new.command('stop')  # Removes private admin; fixture business remains alive.
        runtime=self.runtime();runtime.attach(owner,2,self.nanos()+60_000_000_000)
        self.until(runtime.step,lambda:runtime.prepared)
        end=time.monotonic()+3.2
        while time.monotonic()<end:runtime.step();time.sleep(.02)
        self.assertEqual(runtime.mode,'exact');self.assertFalse(runtime.restored)
        self.assertIsNone(new.process.poll())

    def test_production_close_preserves_protection_and_never_waits_on_stop_receipt(self):
        # Focused controller orchestration test; actual native/I/O are above.
        for fatal in (False,True):
            value=controller.Controller.__new__(controller.Controller)
            value.usage=mock.Mock(restored=True,sealed=True);value.usage.freeze.return_value=True
            value.grant={'generation':3,'ticket':'cpu'};value.channel=mock.Mock()
            value.stopping=value.ready=True;value.frozen=value.quarantined=False
            value.monitor=mock.Mock(fatal=fatal,job=None);value.model=mock.Mock()
            value.model.poll.return_value=None
            value.progress=None;value.progress_fault=None;value.watchdog=None
            value.policies=value.logger=value.capture=None
            value.transaction=mock.Mock(side_effect=AssertionError('stopped Guardian cannot accept retirement'))
            with mock.patch.object(controller.ownership,'cleanup',return_value=True):
                self.assertTrue(value.close_model())
            self.assertEqual(value.usage.freeze.call_count,0 if fatal else 1)
            self.assertIsNone(value.grant);value.usage.detach.assert_called_once()
            value.transaction.assert_not_called()


if __name__ == '__main__':unittest.main()
