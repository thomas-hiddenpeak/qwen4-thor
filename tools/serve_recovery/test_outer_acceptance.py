"""Validation-driver CPU checks; no GPU, host domain or model activation."""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery import ledger, lifecycle, outer_runtime, outer_acceptance as driver, usage_store
from tools.serve_service import package as engine


class DriverTests(unittest.TestCase):
    def setUp(self):
        root = ROOT/'.q4t-work/serve-outer-business-20261112/cpu'
        root.mkdir(parents=True,exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root);self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_resource_descriptor_is_actual_and_not_consumed(self):
        first,second = os.pipe()
        try:
            os.write(second,b'alive')
            with mock.patch.dict(os.environ,Q4T_RESOURCE_ENTRY_FD=str(first)):
                self.assertEqual(driver.resource_fd(),first)
            self.assertEqual(os.read(first,5),b'alive')
            for raw in ('2','-1','999999999999','closed','1.0'):
                with mock.patch.dict(os.environ,Q4T_RESOURCE_ENTRY_FD=raw),self.assertRaises(ValueError):
                    driver.resource_fd()
        finally:os.close(first);os.close(second)

    def test_strict_guard_forwards_only_real_resource_and_held_lease(self):
        lock = self.path/'lease';lock.write_text('')
        first,second = os.pipe()
        try:
            with lock.open('r+') as held:
                env = dict(Q4T_MANAGED_LEASE_FD=str(held.fileno()),Q4T_RESOURCE_ENTRY_FD=str(first))
                plan = dict(lease_path=str(lock),output=str(self.path))
                with mock.patch.dict(os.environ,env),mock.patch.dict(driver.LEGACY,verify=lambda _:None),\
                     mock.patch.object(driver.os,'execve') as execute:
                    with self.assertRaises((ValueError,RuntimeError)):driver.guard(plan,self.path/'plan.json')
                    execute.assert_not_called()
                    fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    driver.guard(plan,self.path/'plan.json')
                    argv = execute.call_args.args[1]
                    self.assertEqual(argv[argv.index('--compute-backend')+1],'nvml')
                    self.assertEqual(argv[argv.index('--lease-fd')+1],str(held.fileno()))
                    self.assertEqual([argv[i+1] for i,a in enumerate(argv) if a=='--inherit-fd'],[str(first)])
                    self.assertIn('--inherit-lease',argv)
        finally:os.close(first);os.close(second)

    def test_driver_only_rewrites_its_own_launcher_and_preserves_other_dispatch(self):
        actor = driver.Stage.__new__(driver.Stage)
        launch = [sys.executable,'-I','-B',driver.LEGACY['__file__'],'launcher','--plan','/plan']
        def run(_):
            subprocess.Popen(launch,pass_fds=(10,))
            subprocess.Popen(['/bin/true'],pass_fds=())
            return 19
        with mock.patch.object(driver,'resource_fd',return_value=11),\
             mock.patch.object(driver.LEGACY['Stage'],'run',run),\
             mock.patch.object(subprocess,'Popen') as called:
            self.assertEqual(actor.run(),19)
        first,second = called.call_args_list
        self.assertEqual(first.args[0][3],str(Path(driver.__file__).resolve()))
        self.assertEqual(first.kwargs['pass_fds'],(10,11))
        self.assertEqual(second.args[0],['/bin/true']);self.assertEqual(second.kwargs['pass_fds'],())

    def test_launcher_wraps_guardian_after_real_resource_dispatch_prefix(self):
        package = self.path/'package';package.mkdir()
        execution = self.path/'execution';execution.mkdir()
        run = self.path/'runs'/'one';run.mkdir(parents=True)
        config = run/'guardian.json';probe = self.path/'probe';probe.write_bytes(b'CPU fixture only')
        recovery = dict(registry=dict(cpu=True),payloads={name:{role:[role,'--config','/'+name+'/'+role+'.json']
            for role in ('controller','gateway')} for name in ('primary','fallback')})
        engine.save(config,dict(version_recovery=recovery))
        plan = dict(output=str(self.path),package=dict(package=str(package),manifest_sha256='a'*64),
            registration=dict(registry=recovery['registry']),probe_wrapper=dict(path=str(probe),sha256=engine.sha(probe)))
        guardian = str(package/'bin/q4t-guardian')
        command = [sys.executable,'-I','-B','/resource_launch.py','--child','--',guardian,'--config',str(config)]
        def run(*args,**kwargs):
            self.assertTrue(kwargs['activate'])
            subprocess.Popen(command,pass_fds=(20,21))
            return dict(state='STOPPED',cleanup_confirmed=True)
        with mock.patch.dict(driver.LEGACY,load_modules=lambda _: (None,None,None,None,None,engine),verify=lambda _:None),\
             mock.patch.object(outer_runtime,'run',run),mock.patch.object(subprocess,'Popen') as spawn:
            self.assertEqual(driver.launcher(plan,self.path/'plan.json'),0)
        self.assertEqual(spawn.call_args.args[0],command)
        self.assertEqual(spawn.call_args.kwargs['pass_fds'],(20,21))
        actual = engine.read(config)['version_recovery']
        for name,commands in actual['payloads'].items():
            for role,argv in commands.items():
                self.assertEqual(argv[3:5],[str(Path(driver.__file__).resolve()),role])
                self.assertEqual(argv[-1],'/'+name+'/'+role+'.json')
        self.assertEqual(actual['cuda_probe'],str(probe))
        self.assertEqual(engine.read(execution/'wrapper-one.json')['production'],recovery)

    def test_state_and_accounting_use_exact_preinitialized_resource_service(self):
        actor = driver.Stage.__new__(driver.Stage)
        actor.plan = dict(output=str(self.path),service_id='original',package=dict(manifest_sha256='a'*64))
        actor.package = self.path/'package'
        actor.service = SimpleNamespace(descriptor=mock.Mock(return_value=dict(service_id='original')))
        actor.engine = SimpleNamespace(require=ledger.require,read=lambda _:dict(state=dict(desired='STOPPED')))
        actor.service_id = actor.initialize_state()['service_id']
        with mock.patch.object(lifecycle,'read_family',return_value={'original':True}) as read:
            self.assertEqual(actor.family('usage'),{'original':True})
        read.assert_called_once_with(self.path/'state',dict(service_id='original'),usage_store.Store)
        actor.service.descriptor.return_value = dict(service_id='foreign')
        with self.assertRaises(ValueError):actor.initialize_state()

    def test_actual_subreaper_is_restored_after_business_entry(self):
        libc = ctypes.CDLL(None,use_errno=True)
        def value():
            result=ctypes.c_int();self.assertEqual(libc.prctl(37,ctypes.byref(result),0,0,0),0)
            return result.value
        before=value();plan=self.path/'plan.json';plan.write_text('{}')
        def run():self.assertEqual(value(),1);return 23
        with mock.patch.object(sys,'argv',['driver','run','--plan',str(plan)]),\
             mock.patch.object(driver,'Stage') as stage:
            stage.return_value.run.side_effect=run;self.assertEqual(driver.main(),23)
        self.assertEqual(value(),before)

    def test_actual_exited_child_is_reaped_without_signal_or_second_reap(self):
        pid=os.fork()
        if pid==0:os._exit(7)
        os.waitid(os.P_PID,pid,os.WEXITED|os.WNOWAIT)
        with mock.patch.object(os,'kill') as kill:
            self.assertEqual(driver.finish_child(pid),7)
            with self.assertRaises(ChildProcessError):driver.finish_child(pid)
        kill.assert_not_called()

    def test_host_cleanup_receipt_retains_original_lock_and_pending_failure(self):
        lock=self.path/'lease';lock.write_text('external_cleanup_pending')
        with lock.open('r+') as held,lock.open('r+') as separate:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for clean in (False,True):
                driver.finish_lease(held.fileno(),self.path,clean)
                self.assertEqual(json.loads(lock.read_text())['state'],'released' if clean else 'cleanup_pending')
                with self.assertRaises(BlockingIOError):fcntl.flock(separate,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with lock.open('r+') as following:fcntl.flock(following,fcntl.LOCK_EX|fcntl.LOCK_NB)


@unittest.skipUnless(os.geteuid()==0,'explicit administrator CPU source check')
class OwnerTests(DriverTests):
    def owner(self):
        uid,gid=int(os.environ['SUDO_UID']),int(os.environ['SUDO_GID'])
        os.chown(self.path,uid,gid)
        return dict(runtime_uid=uid,runtime_gid=gid,output=str(self.path))

    def test_real_private_source_check_runs_after_permanent_owner_drop(self):
        from tools.serve_gateway import client_policy
        plan=self.owner();private=self.path/'private.json';private.write_bytes(b'owner-only')
        os.chown(private,plan['runtime_uid'],plan['runtime_gid']);private.chmod(0o400)
        def verify(_):
            assert os.getresuid()==(plan['runtime_uid'],)*3
            assert os.getresgid()==(plan['runtime_gid'],)*3 and os.getgroups()==[]
            assert client_policy.read_private(private)==b'owner-only'
            (self.path/'owner-result.json').write_text(json.dumps(dict(uid=os.geteuid(),verified=True)))
        with mock.patch.dict(driver.LEGACY,verify=verify):driver.verify_as_owner(plan)
        self.assertEqual(os.geteuid(),0)
        self.assertEqual(json.loads((self.path/'owner-result.json').read_text()),
                         dict(uid=plan['runtime_uid'],verified=True))

    def test_invalid_owner_or_failed_child_never_advances_to_resource_action(self):
        plan=self.owner()
        with mock.patch.object(os,'fork') as fork,self.assertRaises(ValueError):
            driver.verify_as_owner(dict(plan,runtime_uid=0))
        fork.assert_not_called()
        def fail(_):raise ValueError('explicit CPU private source rejection')
        with mock.patch.dict(driver.LEGACY,verify=fail),self.assertRaisesRegex(ValueError,'verification failed'):
            driver.verify_as_owner(plan)
        self.assertEqual(os.geteuid(),0)


if __name__ == '__main__':unittest.main(module=None)
