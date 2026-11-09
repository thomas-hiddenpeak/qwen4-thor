"""CPU CLI/native identity tests. Release verification and Guardian are explicit seams."""
import contextlib
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_store, control, journal, ledger, model_admin
from tools.serve_recovery import quota_control, quota_manager, quota_policy, quota_store
from tools.serve_recovery import service, store, usage_manager, usage_policy, usage_store
from tools.serve_recovery.test_gateway import config
from tools.serve_recovery.test_quota_admin import FIXTURE, ROOT, Server
from tools.serve_recovery.test_quota_usage_admin import request
from tools.serve_service import package as engine


class UsageManagementTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.q4t-work/serve-access-20261109/usage-management-fixtures';base.mkdir(parents=True,exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.package=self.root/'package';(self.package/'engine').mkdir(parents=True)
        self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.service_id=str(uuid.uuid4());self.boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        with store.Store(self.state,self.service_id,initialize=True):pass
        self.desc=dict(version=1,service_id=self.service_id,package=str(self.package),manifest_sha256='a'*64)
        engine.save(self.state/'service.json',self.desc)
        engine.save(self.package/'engine/manifest.json',dict(files={'q4t':engine.sha(FIXTURE)}))
        self.manifest=dict(version=8,ingress_mode='native',
            engine_manifest_sha256=engine.sha(self.package/'engine/manifest.json'))
        self.binding=access_store.binding(self.state,self.service_id)
        self.policy=quota_policy.from_base(config(self.root)['backend_policy'])
        for client in self.policy['clients']:client['requests_per_minute']=1
        quota_store.initialize_bound(self.binding,self.policy)

    def model(self,generation):
        env=dict(os.environ,Q4T_SERVICE_ID=self.service_id,Q4T_MODEL_GENERATION=str(generation),
            Q4T_POLICY_PARENT_PID=str(os.getpid()),Q4T_POLICY_PARENT_START=str(journal.process_start(os.getpid())))
        model=Server(quota_policy.runtime(self.policy,'a'*64),mode='environment',env=env,
                     address=quota_control.address(self.service_id,generation))
        self.addCleanup(model.close)
        snapshot=dict(guardian_pid=os.getppid(),guardian_start_ticks=1,
            controller_pid=os.getpid(),controller_start_ticks=journal.process_start(os.getpid()),
            controller_generation=1,model_pid=model.process.pid,
            model_start_ticks=journal.process_start(model.process.pid),model_generation=generation,
            model_ready=True,stop_requested=False,quota_wait_ms=0)
        owner=dict(pid=model.process.pid,start_ticks=snapshot['model_start_ticks'],boot_id=self.boot)
        return model,snapshot,owner

    def cli(self,operation,snapshot):
        out,err=io.StringIO(),io.StringIO()
        def status(service_id,op,**_kwargs):
            self.assertEqual((service_id,op),(self.service_id,'status'))
            return dict(snapshot)
        # Release verification is tested independently by real bundle tests.
        # PID/birth/parent/ELF and model-side socket here are all real.
        with mock.patch.object(service.bundle,'verify',return_value=self.manifest), \
             mock.patch.object(control,'request',side_effect=status), \
             mock.patch.object(control,'binding',side_effect=AssertionError('operator must not request gateway secrets')), \
             contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=service.main([operation,'--state',str(self.state)],package=self.package,digest='a'*64)
        raw=out.getvalue()+err.getvalue();self.assertNotIn('a'*64,raw)
        return code,json.loads(raw)

    def transaction(self,op,generation,owner,snapshot=None,now=None):
        value=dict(sequence=1,op=op,generation=generation,owner=owner)
        if op=='seal':value['snapshot']=snapshot
        return usage_store.execute(self.binding,value,now_ns=now)

    def closed(self):
        usage_store.initialize_bound(self.binding)
        model,_,owner=self.model(1)
        self.transaction('begin',1,owner)
        self.assertTrue(request(model,'usage_restore')['ok'])
        self.assertTrue(model.command('reserve',id='alpha')['ok']);model.command('settle')
        value=request(model,'usage_freeze')['usage']['snapshot']
        self.transaction('seal',1,owner,value)
        return value

    def test_limits_cli_observes_real_native_policy_without_gateway_binding_privilege(self):
        model,snapshot,_=self.model(1)
        code,value=self.cli('limits-status',snapshot)
        self.assertEqual(code,0);self.assertTrue(value['live_applied'])
        self.assertEqual(value['live']['model_pid'],model.process.pid)
        self.assertEqual(value['live']['content_sha256'],quota_policy.digest(self.policy))
        self.assertNotIn('gateway_token',json.dumps(value))

    def test_usage_cli_identifies_exact_restore_and_survives_durable_state_loss(self):
        self.closed()
        model,snapshot,owner=self.model(2)
        active=self.transaction('begin',2,owner)
        plan=usage_policy.restoration(active,self.boot,time.monotonic_ns())
        self.assertTrue(request(model,'usage_restore',plan['snapshot'])['ok'])
        code,value=self.cli('usage-status',snapshot)
        self.assertEqual(code,0);self.assertEqual(value['continuity'],'exact')
        self.assertEqual(value['durable']['snapshot_kind'],'restore_source')
        self.assertNotIn('clients',value['live'])
        (self.state/'client-usage/policy.json').unlink()
        code,value=self.cli('usage-status',snapshot)
        self.assertEqual(code,1);self.assertTrue(value['live']['recovered'])
        self.assertEqual(value['continuity'],'unknown')
        self.assertFalse(model.command('reserve',id='alpha')['ok'])

    def test_identity_birth_executable_and_generation_changes_refuse_live_claim(self):
        _,snapshot,_=self.model(1)
        for key in ('model_start_ticks','controller_start_ticks'):
            wrong=dict(snapshot);wrong[key]+=1
            with mock.patch.object(control,'request',return_value=wrong):
                with self.assertRaises(ValueError):quota_manager.live(self.desc,self.manifest)
        with mock.patch.object(control,'request',side_effect=[snapshot,dict(snapshot,model_generation=2)]):
            with self.assertRaisesRegex(ValueError,'generation'):quota_manager.live(self.desc,self.manifest)
        engine.save(self.package/'engine/manifest.json',dict(files={'q4t':'f'*64}))
        changed=dict(self.manifest,engine_manifest_sha256=engine.sha(self.package/'engine/manifest.json'))
        with mock.patch.object(control,'request',return_value=snapshot):
            with self.assertRaisesRegex(ValueError,'executable'):model_admin.observe(self.desc,changed)

    def test_legacy_usage_migration_is_stopped_exclusive_and_preserves_core(self):
        legacy=dict(self.manifest,version=7)
        fd=os.open(self.state/'guardian.lock',os.O_RDWR|os.O_CREAT,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):usage_manager.migrate(self.state,self.desc,legacy)
        finally:os.close(fd)
        with store.Store(self.state,self.service_id) as writer:
            writer.transact(dict(id=str(uuid.uuid4()),expected_revision=writer.state['revision'],
                                 operation='desired',args=dict(state='RUNNING')))
        with self.assertRaisesRegex(ValueError,'STOPPED'):usage_manager.migrate(self.state,self.desc,legacy)
        with store.Store(self.state,self.service_id) as writer:
            writer.transact(dict(id=str(uuid.uuid4()),expected_revision=writer.state['revision'],
                                 operation='desired',args=dict(state='STOPPED')))
        before=(self.state/'ledger.json').read_bytes();limits=(self.state/'client-limits/policy.json').read_bytes()
        self.assertTrue(usage_manager.migrate(self.state,self.desc,legacy)['initialized'])
        self.assertFalse(usage_manager.migrate(self.state,self.desc,legacy)['initialized'])
        self.assertEqual((self.state/'ledger.json').read_bytes(),before)
        self.assertEqual((self.state/'client-limits/policy.json').read_bytes(),limits)
        with self.assertRaises(ValueError):usage_manager.migrate(self.state,self.desc,self.manifest)
        shutil.rmtree(self.state/'client-usage')
        with self.assertRaises(OSError):usage_manager.migrate(self.state,self.desc,legacy)
        self.assertFalse((self.state/'client-usage').exists())

    def test_cross_boot_receipt_is_stable_and_sealed_snapshot_is_not_live_usage(self):
        usage_store.initialize_bound(self.binding)
        model,snapshot,owner=self.model(2)
        old=dict(owner,boot_id=str(uuid.uuid4()))
        self.transaction('begin',1,old,now=1)
        # Obtain the actual native wire/digest, then preserve it as an old-boot
        # closed source. No fake model or clock is used for the actual restore.
        prior,_,_=self.model(1)
        request(prior,'usage_restore');prior.command('reserve',id='alpha');prior.command('settle')
        closed=request(prior,'usage_freeze')['usage']['snapshot']
        self.transaction('seal',1,old,closed)
        active=self.transaction('begin',2,owner)
        plan=usage_policy.restoration(active,self.boot,time.monotonic_ns())
        self.assertEqual(plan['mode'],'cross_boot')
        self.assertTrue(request(model,'usage_restore',plan['snapshot'])['ok'])
        code,value=self.cli('usage-status',snapshot)
        self.assertEqual(code,0);self.assertEqual(value['continuity'],'cross_boot')
        self.assertEqual(model.command('status')['tokens'],0)
        current=request(model,'usage_freeze')['usage']['snapshot']
        self.assertEqual(current['clients'][0]['settled_tokens'],'5')
        self.transaction('seal',2,owner,current)
        code,value=self.cli('usage-status',dict(snapshot,model_ready=False,stop_requested=True))
        self.assertEqual(code,0)
        self.assertEqual(value['durable']['snapshot_kind'],'closed_generation')
        self.assertIsNone(value['live']);self.assertEqual(value['continuity'],'unknown')


if __name__ == '__main__':unittest.main()
