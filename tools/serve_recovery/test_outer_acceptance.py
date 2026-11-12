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


class RepairTests(unittest.TestCase):
    setUp = DriverTests.setUp

    def fixture(self):
        counts = dict(starts=2,chats=0,probes=0,controls=86)
        for name in ('execution','lease','state/runs/fixture/primary/controller-1/model-1',
                     'state/runs/fixture/primary/controller-1/model-2'):
            (self.path/name).mkdir(parents=True)
        plan = dict(output=str(self.path),scenario='outer_resource_version',
                    limits=driver.LIMITS,package=dict(fixture=True))
        engine.save(self.path/'plan.json',plan)
        engine.save(self.path/'execution/result.json',dict(status='FAILED',cleanup_failure=None,
            package=plan['package'],budget=dict(counts=counts),seconds=24.958))
        engine.save(self.path/'execution/counts.json',dict(counts=counts,events=[]))
        engine.save(self.path/'execution/launcher-result.json',dict(state='STOPPED',stop_durable=True,
            cleanup_confirmed=True,tree_cleanup_verified=True))
        engine.save(self.path/'host-result.json',dict(resource_cleanup=dict(removed=True)))
        engine.save(self.path/'lease/exit.json',dict(status='COMMAND_FAILED'))
        (self.path/'lease/events.jsonl').write_text('\n'.join(json.dumps(e) for e in
            [dict(event='lease_acquired',monotonic=100),dict(event='lease_guard_finished',monotonic=127)]))
        for p in (self.path/'state').glob('runs/*/primary/controller-1/model-*'):
            (p/'model.log').write_text('CPU fixture: NvRmMemInitNvmap failed: error Permission denied')
        return self.path/'plan.json'

    def test_carries_exact_consumption_full_lease_interval_and_original_failures(self):
        path = self.fixture(); prior = driver.prior_completion(path)
        self.assertEqual(prior['prior_counts'],dict(starts=2,chats=0,probes=0,controls=86))
        self.assertEqual(prior['prior_seconds'],27)
        self.assertEqual(len(prior['prior_evidence']),9)
        for row in prior['prior_evidence']:
            self.assertEqual(row['sha256'],engine.sha(row['path']))
        self.assertEqual(driver.remaining_seconds(dict(limits=driver.REPAIR_LIMITS,**prior)),873)

    def test_environment_abort_incomplete_cleanup_and_repeated_repair_are_rejected(self):
        path=self.fixture()
        for file,key,value in [('lease/exit.json','status','ENV_ABORT'),
            ('execution/launcher-result.json','cleanup_confirmed',False),
            ('plan.json','scenario','outer_resource_identity_repair')]:
            p=self.path/file;old=p.read_bytes();data=json.loads(old);data[key]=value;engine.save(p,data)
            try:
                with self.assertRaises(RuntimeError):driver.prior_completion(path)
            finally:p.write_bytes(old)
        log=next((self.path/'state').glob('runs/*/primary/controller-1/model-1/model.log'))
        log.write_text('different failure')
        with self.assertRaises(RuntimeError):driver.prior_completion(path)

    def test_repair_budget_admits_only_three_more_starts_without_resetting_other_counts(self):
        prior=driver.prior_completion(self.fixture());out=self.path/'new-execution';out.mkdir()
        budget=driver.LEGACY['Budget'](out,initialize=True,baseline=prior['prior_counts'],limits=driver.REPAIR_LIMITS)
        self.assertEqual([budget.reserve('starts','fixed-model') for _ in range(3)],[3,4,5])
        with self.assertRaises(RuntimeError):budget.reserve('starts','beyond-fixed-repair')
        self.assertEqual(engine.read(out/'counts.json')['counts']['controls'],86)
        for kind in ('chats','probes'):
            for _ in range(driver.REPAIR_LIMITS[kind]):budget.reserve(kind,'bounded')
            with self.assertRaises(RuntimeError):budget.reserve(kind,'exhausted')

    def test_original_and_repair_scenarios_have_distinct_fixed_caps(self):
        resolve=driver.LEGACY['verify'].__globals__['limits_for']
        self.assertEqual(resolve('outer_resource_version')['starts'],3)
        self.assertEqual(resolve('outer_resource_identity_repair')['starts'],5)
        self.assertEqual(driver.REPAIR_LIMITS,dict(driver.LIMITS,starts=5))


class ContinuityTests(unittest.TestCase):
    setUp = DriverTests.setUp

    def values(self):
        state=dict(model_generation=2,model_pid=42,model_start_ticks=123,quota_wait_ms=0)
        usage=dict(phase='active',generation=2,owner=dict(pid=42,start_ticks=123),source=None,snapshot=None)
        return usage,state

    def test_unsealed_crash_requires_real_expired_gate_and_exact_owner(self):
        usage,state=self.values();driver.check_usage(usage,state,60,61)
        for deadline,now in ((None,61),(60,59)):
            with self.assertRaises(ValueError):driver.check_usage(usage,state,deadline,now)
        with self.assertRaises(ValueError):driver.check_usage(usage,dict(state,model_pid=43),60,61)

    def test_exact_seal_requires_previous_generation_and_snapshot(self):
        usage,state=self.values();usage.update(source=dict(generation=1),snapshot=dict(cpu=True))
        driver.check_usage(usage,state,None,1)
        for change in (dict(source=dict(generation=2)),dict(snapshot=None)):
            with self.assertRaises(ValueError):driver.check_usage(dict(usage,**change),state,60,61)

    def test_completion_keeps_state_package_and_all_consumed_evidence(self):
        path=RepairTests.fixture(self);old=engine.read(path)
        old.update(scenario='outer_resource_identity_repair',limits=driver.REPAIR_LIMITS,
            prior_counts=dict(starts=2),prior_seconds=26.25,prior_evidence=[],sources={},
            registration=dict(cpu=True),service_id='unchanged-cpu-service')
        engine.save(path,old)
        result=engine.read(self.path/'execution/result.json');counts=dict(starts=4,chats=3,probes=0,controls=563)
        result.update(seconds=128.98,budget=dict(counts=counts),
            failure="profile_managed_acceptance.py TypeError: 'NoneType' object is not subscriptable")
        engine.save(self.path/'execution/result.json',result)
        engine.save(self.path/'execution/counts.json',dict(counts=counts))
        engine.save(self.path/'state/ledger.json',dict(state=dict(desired='STOPPED',active=dict(model=None),
            quarantine=None,generations=dict(model=2))))
        (self.path/'state/outer-recovery').mkdir()
        engine.save(self.path/'state/outer-recovery/policy.json',dict(state=dict(policy=dict(total=2,pending=None,protected=False))))
        usage,state=self.values();rows=[]
        for generation in (1,2):
            value=dict(state,model_generation=generation,business_ready=True,monotonic=161)
            if generation==2:rows.append(dict(value,business_ready=False,monotonic=100,quota_wait_ms=60000))
            rows.append(value);engine.save(self.path/f'execution/usage-{generation}.json',dict(usage,generation=generation))
        (self.path/'execution/status.jsonl').write_text('\n'.join(map(json.dumps,rows)))
        engine.save(self.path/'execution/first-schema.wire',dict(id='original-chat',choices=[dict(message=dict(content='{"answer":42}'))]))
        out=self.path/'completion';before={p:p.read_bytes() for p in self.path.rglob('*') if p.is_file()}
        args=SimpleNamespace(complete_plan=path,output=out)
        def git(argv,**_):return b'' if 'status' in argv else 'cpu-source\n'
        with mock.patch.object(subprocess,'check_output',side_effect=git),\
             mock.patch.dict(driver.LEGACY,load_modules=lambda:(None,None,None,None,None,engine)):
            driver.prepare_completion(args)
        after=engine.read(out/'plan.json')
        self.assertEqual(after['state'],str(self.path/'state'));self.assertEqual(after['package'],old['package'])
        self.assertEqual(after['registration'],old['registration']);self.assertEqual(after['service_id'],old['service_id'])
        self.assertEqual(after['prior_counts'],counts);self.assertEqual(after['prior_seconds'],155.23)
        self.assertEqual(after['planned'],dict(starts=2,self_checks=2,business_chats=3,probes=1))
        for p,raw in before.items():self.assertEqual(p.read_bytes(),raw)
        for row in after['prior_evidence']:self.assertEqual(engine.sha(row['path']),row['sha256'])
        execution=out/'execution';execution.mkdir()
        budget=driver.LEGACY['Budget'](execution,initialize=True,baseline=counts,limits=after['limits'])
        self.assertEqual([budget.reserve('starts','remaining') for _ in range(2)],[5,6])
        with self.assertRaises(RuntimeError):budget.reserve('starts','no-extra')
        self.assertEqual([budget.reserve('chats','remaining') for _ in range(5)],[4,5,6,7,8])
        with self.assertRaises(RuntimeError):budget.reserve('chats','no-extra')


if __name__ == '__main__':unittest.main(module=None)
