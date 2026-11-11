"""Intent-first resume and offline operator stop; CPU journals only."""
import json
import fcntl
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery import access_store, control, deployment, journal, ledger, lifecycle
from tools.serve_recovery import outer_policy, outer_store, service, store
from tools.serve_recovery import test_runtime
from tools.serve_recovery.test_lifecycle import LifecycleTests


class ResumeTests(unittest.TestCase):
    setUp=LifecycleTests.setUp
    files=LifecycleTests.files
    command=LifecycleTests.command

    def change(self,operation,**args):
        with store.Store(self.state,self.desc['service_id']) as current:
            return current.transact(dict(id=str(uuid.uuid4()),expected_revision=current.state['revision'],
                                         operation=operation,args=args))['result']

    def core(self):
        with store.Store(self.state,self.desc['service_id']) as current:return current.state

    def running(self):self.change('desired',state='RUNNING')

    def test_resume_stopped_never_acquires_gpu_or_creates_a_run(self):
        before=self.files()
        with mock.patch.object(service,'acquire_lease',side_effect=AssertionError('GPU forbidden')), \
             mock.patch.object(service,'launch_owned',side_effect=AssertionError('launch forbidden')):
            rc,value=self.command('resume')
        self.assertEqual(rc,0);self.assertFalse(value['resumed'])
        self.assertEqual(value['desired'],'STOPPED');self.assertTrue(value['cleanup_confirmed'])
        self.assertEqual(self.files(),before);self.assertEqual(list((self.state/'runs').iterdir()),[])

    def test_running_resume_selects_nonactivating_launch_and_keeps_core(self):
        self.running();before=self.core()
        with mock.patch.object(service,'launch_owned',return_value=dict(guardian_exit_code=0)) as launch:
            rc,_=self.command('resume')
        self.assertEqual(rc,0);self.assertTrue(launch.call_args.kwargs['resume'])
        self.assertEqual(self.core(),before)

    def test_stop_between_preflight_and_actual_guardian_dispatch_wins(self):
        self.running();original=service.subprocess.Popen;observed=[];errors=[];threads=[]
        lease_path=self.fixture.directory/'private-resume-cpu-lease'
        def lease(_path):
            fd=os.open(lease_path,os.O_RDWR | os.O_CREAT,0o600)
            fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        def spawn(command,**options):
            self.assertEqual(command[-1],'--resume')
            # The operator commits STOPPED after preflight but before exec.
            self.assertTrue(lifecycle.request_stop(self.state,self.package,self.digest)['stop_durable'])
            path=Path(command[2]);config=json.loads(path.read_text())
            config.update(version=1,quota_enforced=False,gateway=[],lease_path=str(lease_path),
                controller=[sys.executable,'-I','-B',str(ROOT/'tools/serve_recovery/fixture_controller.py'),
                            '--model-fixture',str(self.state)],term_ms=100,kill_ms=200,heartbeat_ms=1000)
            config.pop('usage_handoff',None);path.write_text(json.dumps(config))
            process=original([str(test_runtime.BINARY),*command[1:]],**options)
            def stop_ready():
                try:
                    end=time.monotonic()+3
                    while time.monotonic()<end:
                        try:value=control.request(self.desc['service_id'],'status',timeout=.2)
                        except OSError:time.sleep(.01);continue
                        if value['stop_durable']:
                            observed.append(value)
                            observed.append(control.request(self.desc['service_id'],'stop',timeout=3))
                            return
                        time.sleep(.01)
                    raise TimeoutError('resumed CPU Guardian did not honor STOPPED')
                except BaseException as error:
                    errors.append(repr(error))
                    if process.poll() is None:process.terminate()
            thread=threading.Thread(target=stop_ready);thread.start();threads.append(thread)
            return process
        with mock.patch.object(service,'acquire_lease',side_effect=lease), \
             mock.patch.object(service.subprocess,'Popen',side_effect=spawn):
            rc,result=self.command('resume')
        for thread in threads:thread.join(timeout=4);self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[]);self.assertEqual(rc,0);self.assertEqual(result['guardian_exit_code'],0)
        self.assertEqual(len(observed),2);self.assertTrue(observed[1]['cleanup_confirmed'])
        self.assertEqual(self.core()['desired'],'STOPPED')
        self.assertEqual(self.core()['generations'],dict(controller=0,model=0,gateway=0))

    def test_resume_never_bypasses_live_instance_active_slots_or_fault_protection(self):
        self.running()
        with mock.patch.object(service,'launch_owned',side_effect=AssertionError('launch forbidden')):
            with deployment.claim(self.state,name='guardian.lock'):
                with self.assertRaises(BlockingIOError):lifecycle.resume(self.state,self.package,self.digest)
            self.change('authorize',role='controller',proof={k:True for k in ledger.PROOFS})
            before=self.core()
            with self.assertRaisesRegex(ValueError,'reconciled'):
                lifecycle.resume(self.state,self.package,self.digest)
            self.assertEqual(self.core(),before)
            self.change('retired',role='controller',ticket=before['active']['controller']['ticket'],verified=True)
            self.change('quarantine',reason='device_failure');before=self.core()
            with self.assertRaisesRegex(ValueError,'unprotected'):
                lifecycle.resume(self.state,self.package,self.digest)
            self.assertEqual(self.core(),before)

    def test_offline_stop_persists_intent_without_erasing_unreconciled_history(self):
        self.running()
        self.change('authorize',role='controller',proof={k:True for k in ledger.PROOFS})
        self.change('quarantine',reason='device_failure');before=self.core()
        with mock.patch.object(service.control,'request',side_effect=ConnectionRefusedError):
            rc,value=self.command('stop')
            self.assertEqual(rc,1);self.assertTrue(value['stop_durable'])
            self.assertFalse(value['cleanup_confirmed']);self.assertTrue(value['reconciliation_required'])
            after=self.core();self.assertEqual(after['desired'],'STOPPED')
            for key in ('active','generations','quarantine','attempts','failures'):
                self.assertEqual(after[key],before[key])
            self.assertEqual(after['revision'],before['revision']+1)
            rc,value=self.command('stop');self.assertEqual(self.core(),after)
            self.assertTrue(value['stop_durable']);self.assertFalse(value['cleanup_confirmed'])

    def test_offline_stop_does_not_write_through_live_guardian_or_writer(self):
        self.running();before=self.files()
        for lock in ('guardian.lock',store.LOCK):
            with deployment.claim(self.state,name=lock),self.assertRaises(BlockingIOError):
                lifecycle.request_stop(self.state,self.package,self.digest)
            self.assertEqual(self.files(),before)

    def test_interrupted_offline_stop_recovers_original_pending_intent(self):
        self.running();before=self.core()
        with mock.patch.object(os,'replace',side_effect=OSError('injected stop rename loss')):
            with self.assertRaises(OSError):lifecycle.request_stop(self.state,self.package,self.digest)
        self.assertTrue((self.state/store.PENDING).exists())
        value=lifecycle.request_stop(self.state,self.package,self.digest)
        self.assertTrue(value['stop_durable']);self.assertTrue(value['cleanup_confirmed'])
        after=self.core();self.assertEqual(after['revision'],before['revision']+1)
        self.assertEqual(after['desired'],'STOPPED')

    def test_unresolved_outer_attempt_cannot_be_reported_as_completed_stop(self):
        self.running();binding=access_store.binding(self.state,self.desc['service_id'])
        now=store.stamp()
        outer_store.initialize_bound(binding,outer_policy.initial(self.desc['service_id'],now))
        outer_store.commit_once(binding,'configure',dict(enabled=True))
        outer_store.commit_once(binding,'request',dict(core=self.core(),ticket=str(uuid.uuid4()),
            manifest_sha256=self.digest,cleanup_verified=True,identity_valid=True,custody=dict(
                supervisor=dict(pid=os.getpid(),start_ticks=journal.process_start(os.getpid()),
                                boot_id=now.boot_id),
                instance_token=str(uuid.uuid4()),tree_scope=str(uuid.uuid4()))))
        with mock.patch.object(service.control,'request',side_effect=ConnectionRefusedError):
            rc,value=self.command('stop');self.assertEqual(rc,1)
            self.assertTrue(value['stop_durable']);self.assertFalse(value['cleanup_confirmed'])
            rc,value=self.command('status');self.assertEqual(rc,1);self.assertEqual(value['state'],'UNKNOWN')
            rc,value=self.command('resume');self.assertEqual(rc,0)
            self.assertFalse(value['resumed']);self.assertFalse(value['cleanup_confirmed'])
        with outer_store.open_bound(binding) as current:
            self.assertEqual(current.head['state']['policy']['total'],1)
            self.assertIsNotNone(current.head['state']['policy']['pending'])
        (self.state/'outer-recovery.json').unlink()
        with self.assertRaisesRegex(ValueError,'lost its persistent binding'):
            lifecycle.stopped(self.state,self.package,self.digest)


if __name__=='__main__':unittest.main(module=None)
