"""Scoped native memory observations and CPU-only qualified degradation."""
from copy import deepcopy
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import controller, ledger, profile_controller, profile_memory, profile_policy
from tools.serve_recovery import resource_memory as memory, resource_launch, resource_kernel as kernel
from tools.serve_recovery import test_resource_install as fixtures, test_profile_policy


def observed(second=0, current=100, high=100, **changes):
    result = dict(format=memory.FORMAT, binding_sha256='a'*64, sampled_monotonic_ns=second*ledger.SECOND,
        memcg={'memory.current':current,'memory.peak':current,'memory.swap.current':0,'pids.current':1},
        memory_events=dict(low=0,high=1,max=0,oom=0,oom_kill=0), pids_events=dict(max=0),
        physical=dict(total_bytes=128*1024**3, available_bytes=120*1024**3),
        high_bytes=high, max_bytes=2*high, physical_ram_limit=False)
    result.update(changes); return result


class PressureTests(unittest.TestCase):
    def test_actual_pressure_requires_fresh_continuous_samples_not_event_history(self):
        detector = memory.Pressure()
        for second in range(3):
            self.assertEqual(detector.assess(observed(second), second*ledger.SECOND, 2), second == 2)
        self.assertFalse(detector.assess(observed(2), 3*ledger.SECOND, 2))
        self.assertFalse(detector.assess(observed(4, current=99), 4*ledger.SECOND, 2))
        self.assertIsNone(detector.since)
        self.assertFalse(detector.assess(observed(5), 8*ledger.SECOND, 2))
        self.assertIsNone(detector.since)
        self.assertFalse(detector.assess(observed(9), 9*ledger.SECOND, 2))

    def test_missing_corrupt_or_future_observation_cannot_trigger_or_promote(self):
        for value in (None, {}, observed(-1), observed(3), observed(0, physical_ram_limit=True),
                      observed(0, sampled_monotonic_ns=True)):
            with self.subTest(value=value):
                detector=memory.Pressure();detector.assess(observed(),0,2)
                self.assertFalse(detector.assess(value,ledger.SECOND,2));self.assertIsNone(detector.since)
                self.assertFalse(memory.recovery_clear(value,ledger.SECOND,0))
        self.assertFalse(memory.recovery_clear(observed(current=90),0,10))
        self.assertTrue(memory.recovery_clear(observed(current=89),0,10))

    def coordinator(self, selected):
        registry=test_profile_policy.registry()
        state=SimpleNamespace()
        settings=dict(profile_policy.DEFAULTS,pressure_seconds=2,recovery_seconds=30)
        state={'selected':selected,'pending':None,'configuration':settings}
        runtime=mock.Mock();runtime.ready.return_value=True;runtime.state=state
        profiles=profile_controller.Profiles.__new__(profile_controller.Profiles)
        profiles.registry=registry;profiles.selection=runtime;profiles.launch=selected
        profiles.detector=None;profiles.resource_pressure=memory.Pressure()
        actor=SimpleNamespace(phase='READY',reason='serving',grant={'generation':1},
            watchdog=mock.Mock(),resource_memory=mock.Mock())
        return profiles,actor,runtime

    def test_memcg_high_proposes_qualified_light_even_with_abundant_host_ram(self):
        profiles,actor,runtime=self.coordinator('primary')
        for second in range(3):
            runtime.nanos.return_value=second*ledger.SECOND
            actor.watchdog.snapshot.return_value=dict(state='OPEN',sampled_monotonic=second,
                total_bytes=128*1024**3,available_bytes=120*1024**3)
            actor.resource_memory.snapshot.return_value=observed(second)
            self.assertFalse(profiles.switch_ready(actor))
        runtime.prepare.assert_called_once_with('light','sustained_memory_pressure',1)
        runtime.confirm.assert_not_called()

    def test_unknown_memcg_does_not_disable_confirmed_global_pressure(self):
        profiles,actor,runtime=self.coordinator('primary');actor.resource_memory.snapshot.return_value=None
        for second in range(3):
            runtime.nanos.return_value=second*ledger.SECOND
            actor.watchdog.snapshot.return_value=dict(state='LOW',sampled_monotonic=second,
                total_bytes=128*1024**3,available_bytes=1*1024**3)
            self.assertFalse(profiles.switch_ready(actor))
        runtime.prepare.assert_called_once_with('light','sustained_memory_pressure',1)

    def test_promotion_requires_both_domains_and_resets_after_missing_sample(self):
        profiles,actor,runtime=self.coordinator('light')
        for second in range(63):
            runtime.nanos.return_value=second*ledger.SECOND
            actor.watchdog.snapshot.return_value=dict(state='OPEN',sampled_monotonic=second,
                total_bytes=128*1024**3,available_bytes=120*1024**3)
            actor.resource_memory.snapshot.return_value=None if second==30 else observed(second,current=1,high=10**9)
            self.assertFalse(profiles.switch_ready(actor))
            if second < 61: runtime.prepare.assert_not_called()
        self.assertTrue(all(c.args==('primary','sustained_memory_recovery',1) for c in runtime.prepare.call_args_list))
        self.assertGreaterEqual(runtime.prepare.call_count,1)


class KernelTests(fixtures.InstallationTests):
    def setUp(self):
        super().setUp();self.selected=kernel.policy(64,256,64);self.initialize()
        self.binding=dict(directory=str(self.directory),publication=resource_launch.install.read_public(self.directory))

    def run_sample(self, mode):
        process=subprocess.Popen([sys.executable,'-I','-B',__file__,'--entry',str(self.directory),mode],
            env={},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.processes.append(process)
        raw,error=process.communicate(timeout=12)
        self.assertEqual(process.returncode,0,error.decode());return json.loads(raw)

    def test_real_cpu_high_counter_and_physical_memory_are_separate(self):
        value=self.run_sample('high');memory.validate(value)
        self.assertEqual(value['binding_sha256'],ledger.digest(self.binding))
        self.assertGreater(value['memory_events']['high'],0)
        self.assertGreaterEqual(value['memcg']['memory.current'],value['high_bytes'])
        self.assertGreater(value['physical']['total_bytes'],value['max_bytes'])
        self.assertFalse(value['physical_ram_limit'])

    def test_live_cpu_collector_is_reaped_before_slot_is_reused(self):
        result=self.run_sample('observer')
        self.assertTrue(result['observed']);self.assertTrue(result['reaped']);self.assertEqual(result['models'],0)

    def test_wrong_membership_and_changed_kernel_limit_are_unknown_not_authority(self):
        with self.assertRaisesRegex(ValueError,'self-entry'):memory.sample(self.binding)
        path=Path(self.binding['publication']['record']['root'])/'service/memory.high'
        original=path.read_text()
        try:
            path.write_text(str(65*1024**2))
            with self.assertRaisesRegex(ValueError,'limit changed'):memory.sample(self.binding)
        finally:path.write_text(original)

    def test_collector_exception_retains_original_slot_and_only_clears_observation(self):
        job=mock.Mock();job.tick.side_effect=RuntimeError('injected collector failure')
        factory=mock.Mock(return_value=job)
        observer=memory.Observer(self.binding,3,worker=dict(path='/cpu-collector',sha256='a'*64),factory=factory,clock=lambda:1)
        observer.tick();observer.value=observed(1)
        for _ in range(3):observer.tick()
        self.assertIs(observer.job,job);factory.assert_called_once()
        self.assertIsNone(observer.snapshot());self.assertEqual(observer.reason,'resource_collector_failed')
        job.close.assert_not_called();self.assertEqual(job.kill.call_count,3)

    def test_controller_configuration_binds_selected_resource_with_and_without_profiles(self):
        for profiles in (None,dict(service_id=self.sid,state_directory='/cpu-state',state_identity=[1,2])):
            value=dict(version=6,package='/cpu-package',manifest_sha256='a'*64,directory='/cpu-run',
                state_directory='/cpu-state',retention_context=None,limits=profiles,profiles=profiles,
                resources=self.binding,resource_worker=dict(path='/cpu-collector',sha256='a'*64))
            controller.validate_configuration(value)
            broken=deepcopy(value);broken['resources']['publication']['device_authorized']=True
            with self.assertRaises(ValueError):controller.validate_configuration(broken)


def child(directory,mode):
    publication=resource_launch.install.read_public(directory)
    binding=dict(directory=str(directory),publication=publication)
    if mode=='high':
        leaf=Path(publication['record']['root'])/'service'
        current=int((leaf/'memory.current').read_text())
        high=publication['record']['policy']['service']['memory_high_bytes']
        # Exercise a real high event without prolonged reclaim from a large
        # overshoot. The failed original 80 MiB case is retained as evidence.
        amount=max(256*1024,high-current+256*1024)
        pages=bytearray(amount);pages[::4096]=b'x'*len(pages[::4096])
        print(json.dumps(memory.sample(binding)),flush=True);return
    lease=os.open('/dev/null',os.O_RDONLY|os.O_CLOEXEC)
    # This CPU-only observer carries an inert FD, never the actual GPU lease.
    import hashlib
    script=Path(memory.__file__).resolve()
    # Use an owned sealed copy so production source permissions stay unchanged.
    import tempfile
    temporary=tempfile.TemporaryDirectory(dir=ROOT/'.q4t-work')
    selected=Path(temporary.name)/'resource_memory.py'
    selected.write_bytes(script.read_bytes());selected.chmod(0o400)
    # Keep the original root imports in this explicit CPU-only collector.
    data=selected.read_text().replace("ROOT = Path(__file__).resolve().parents[2]","ROOT = Path("+repr(str(ROOT))+")")
    selected.chmod(0o600);selected.write_text(data);selected.chmod(0o400)
    worker=dict(path=str(selected),sha256=hashlib.sha256(selected.read_bytes()).hexdigest())
    observer=memory.Observer(binding,lease,worker=worker);end=time.monotonic()+6
    try:
        while observer.snapshot() is None and time.monotonic()<end:
            observer.tick();time.sleep(.01)
        assert observer.snapshot() is not None,observer.reason
        observer.cancel()
        while observer.job is not None and time.monotonic()<end:
            observer.tick(allow_query=False);time.sleep(.01)
        assert observer.job is None
        print(json.dumps(dict(observed=True,reaped=True,models=0)),flush=True)
    finally:
        observer.cancel();os.close(lease);temporary.cleanup()


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--entry':
        resource_launch.enter(sys.argv[2],[sys.executable,'-I','-B',__file__,'--outer',sys.argv[2],sys.argv[3]])
    elif len(sys.argv)>1 and sys.argv[1]=='--outer':
        directory=Path(sys.argv[2]);selected=resource_launch.install.read_public(directory)
        cap=resource_launch.inherited(selected['service_id'])
        command=cap.dispatch([sys.executable,'-I','-B',__file__,'--child',str(directory),sys.argv[3]],
                            sys.executable,ROOT/'tools/serve_recovery/resource_launch.py')
        os.set_inheritable(cap.fd,True);os.execve(command[0],command,dict(os.environ))
    elif len(sys.argv)>1 and sys.argv[1]=='--child':child(Path(sys.argv[2]),sys.argv[3])
    else:unittest.main(module=None)
