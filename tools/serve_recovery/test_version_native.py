"""Actual native root/CPU children; qualification and CUDA probe are fakes."""
import copy
import json
import os
from pathlib import Path
import resource
import signal
import time
import unittest
import uuid

from tools.serve_recovery import access_store, ledger, store
from tools.serve_recovery import test_runtime as runtime, version_policy, version_store

ROOT=Path(__file__).resolve().parents[2]


def fixture(service, *, hung_probe=False, bad_fallback=False):
    config=service.config
    payloads={};identities={}
    for name,binary,protocol in [('primary','c','d'),('fallback','e','f')]:
        binding=dict(binary_sha256=binary*64,protocol_sha256=protocol*64,
            port=18000,operations_port=18001,
            profile_capacities=dict(primary='a'*64,light='a'*64),
            profile_registry_sha256='9'*64,controller_sha256='3'*64,ingress_sha256='4'*64)
        command=list(config['controller'])
        command += ['--binary-fingerprint',('b' if bad_fallback and name=='fallback' else binary)*64,
                    '--protocol-fingerprint',protocol*64]
        if name=='fallback':
            path=service.directory/'fallback';path.mkdir(mode=0o700)
            command[command.index('--model-fixture')+1]=str(path)
        payloads[name]=dict(controller=command,gateway=config['gateway'],binding=binding)
        identities[name]=dict(manifest_sha256=('a' if name=='primary' else 'b')*64,
                              binding_sha256=ledger.digest(binding))
    registry=version_policy.registry(dict(format=version_policy.REGISTRY_FORMAT,
        cycle_id=str(uuid.uuid4()),root_manifest_sha256='7'*64,payloads=identities,
        compatibility_sha256='1'*64,qualification_sha256='2'*64))
    binding=access_store.binding(service.directory,service.service_id)
    version_store.initialize_bound(binding,version_policy.initial(registry,store.stamp().boot_id))
    probe='/usr/bin/true'
    if hung_probe:
        path=service.directory/'cpu-probe'
        path.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(300)\n')
        path.chmod(0o700);probe=str(path)
    return dict(registry=registry,payloads=payloads,cuda_probe=probe,
        journal=str(ROOT/'tests/fixtures/recovery/version_journal_fixture.py'))


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime.RuntimeTests.setUpClass.__func__(cls)
        cls.core_limit=resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE,(0,cls.core_limit[1]))

    @classmethod
    def tearDownClass(cls):
        resource.setrlimit(resource.RLIMIT_CORE,cls.core_limit)
        runtime.RuntimeTests.tearDownClass.__func__(cls)

    setUp=runtime.RuntimeTests.setUp
    tearDown=runtime.RuntimeTests.tearDown

    def launch(self,**options):
        service=runtime.Service(self.output,models=True,gateway=True,
            capacity_fingerprint='a'*64,version_fixture=lambda s:fixture(s,**options))
        self.services.append(service)
        return service,service.wait(lambda s:s['business_ready'])

    def signal(self,value,role,signum):
        runtime.signal_identity(value[role+'_pid'],value[role+'_start_ticks'],signum)

    def finish(self,service):
        self.assertTrue(service.stop()['cleanup_confirmed'])
        self.assertEqual(service.persistent()['desired'],'STOPPED')
        self.assertFalse(any(service.persistent()['active'].values()))
        service.assert_lease_free()

    def test_actual_controller_fault_replaces_whole_tree_then_fallback_fault_protects(self):
        service,before=self.launch()
        time.sleep(.3)  # Establish a pre-fault CPU memory witness.
        self.signal(before,'controller',signal.SIGABRT)
        after=service.wait(lambda s:s['business_ready'] and
            s['version_recovery']['selected']=='fallback',timeout=20)
        for role in version_policy.ROLES:
            self.assertGreater(after[role+'_generation'],before[role+'_generation'])
            self.assertNotEqual(after[role+'_pid'],before[role+'_pid'])
        self.assertEqual(after['model_binary_sha256'],'e'*64)
        self.assertEqual(after['model_protocol_sha256'],'f'*64)
        self.assertEqual(after['version_recovery']['probe_count'],1)
        time.sleep(.3)
        self.signal(after,'controller',signal.SIGILL)
        protected=service.wait(lambda s:s['version_recovery']['protected'],timeout=10)
        self.assertFalse(protected['business_ready'])
        self.assertEqual(protected['version_recovery']['probe_count'],2)
        self.assertEqual(protected['model_generation'],after['model_generation'])
        self.assertEqual(len(service.persistent()['attempts']['model']),1)
        self.finish(service)

    def test_version_worker_exit_recovers_only_worker_with_same_healthy_tree(self):
        service,before=self.launch()
        version=before['version_recovery']
        runtime.signal_identity(version['worker_pid'],version['worker_start_ticks'],signal.SIGSTOP)
        # Idle workers have no transaction deadline. Terminating one provides
        # a real channel failure; a blocked pending worker is covered below.
        runtime.signal_identity(version['worker_pid'],version['worker_start_ticks'],signal.SIGKILL)
        after=service.wait(lambda s:s['version_recovery']['state_available'] and
            s['version_recovery']['worker_pid']!=version['worker_pid'],timeout=15)
        for role in version_policy.ROLES:
            self.assertEqual(after[role+'_pid'],before[role+'_pid'])
            self.assertEqual(after[role+'_generation'],before[role+'_generation'])
        self.assertEqual(after['journal_pid'],before['journal_pid'])
        self.assertTrue(after['business_ready'])
        self.assertFalse(after['version_recovery']['fallback_consumed'])
        self.finish(service)

    def test_pending_version_write_timeout_reaps_worker_before_resuming_one_switch(self):
        service,before=self.launch();time.sleep(.3)
        version=before['version_recovery']
        runtime.signal_identity(version['worker_pid'],version['worker_start_ticks'],signal.SIGSTOP)
        self.signal(before,'controller',signal.SIGABRT)
        after=service.wait(lambda s:s['business_ready'] and
            s['version_recovery']['selected']=='fallback',timeout=30)
        self.assertNotEqual(after['version_recovery']['worker_pid'],version['worker_pid'])
        self.assertEqual(after['journal_pid'],before['journal_pid'])
        self.assertEqual(after['version_recovery']['probe_count'],1)
        self.assertEqual(len(service.persistent()['attempts']['model']),1)
        self.finish(service)

    def test_unknown_kill_uses_existing_role_budget_without_version_blame(self):
        service,before=self.launch()
        self.signal(before,'controller',signal.SIGKILL)
        after=service.wait(lambda s:s['business_ready'] and
            s['controller_generation']>before['controller_generation'],timeout=15)
        self.assertEqual(after['version_recovery']['selected'],'primary')
        self.assertFalse(after['version_recovery']['fallback_consumed'])
        self.assertEqual(after['version_recovery']['probe_count'],0)
        self.assertEqual(len(service.persistent()['attempts']['controller']),1)
        self.finish(service)

    def test_alive_or_malformed_model_exit_reports_leave_original_tree_running(self):
        service,before=self.launch()
        marker=service.directory/'model-exit-report.json'
        for report in (dict(model_generation=before['model_generation'],pid=before['model_pid'],
                            start_ticks=before['model_start_ticks'],signal=11),dict(bad=True)):
            marker.write_text(json.dumps(report))
            end=time.monotonic()+2
            while marker.exists() and time.monotonic()<end:time.sleep(.02)
            self.assertFalse(marker.exists())
        after=service.wait(lambda s:s['auxiliary_projection_errors']>=1)
        self.assertTrue(after['business_ready'])
        self.assertFalse(after['version_recovery']['fallback_consumed'])
        for role in version_policy.ROLES:self.assertEqual(after[role+'_pid'],before[role+'_pid'])
        self.finish(service)

    def test_eof_before_fatal_wait_result_is_not_overwritten_by_cleanup_signal(self):
        service,before=self.launch();time.sleep(.3)
        (service.directory/'cpu-fatal-close-first').touch()
        after=service.wait(lambda s:s['business_ready'] and
            s['version_recovery']['selected']=='fallback',timeout=20)
        self.assertEqual(after['version_recovery']['last_fault']['role'],'controller')
        self.assertEqual(after['version_recovery']['last_fault']['signal'],6)
        self.assertEqual(after['model_generation'],before['model_generation']+1)
        self.finish(service)

    def test_model_parent_wait_result_preserves_original_fault_before_fallback(self):
        service,before=self.launch();time.sleep(.3)
        self.signal(before,'model',signal.SIGSEGV)
        after=service.wait(lambda s:s['business_ready'] and
            s['version_recovery']['selected']=='fallback',timeout=40)
        self.assertEqual(after['version_recovery']['last_fault']['role'],'model')
        self.assertEqual(after['version_recovery']['last_fault']['signal'],11)
        faults=service.persistent()['failures']
        self.assertEqual(len(faults),1)
        self.assertEqual(faults[0]['generation'],before['model_generation'])
        self.assertTrue(faults[0]['unknown'])
        self.finish(service)

    def test_stop_during_device_probe_never_starts_fallback_and_reaps_helpers(self):
        service,before=self.launch(hung_probe=True);time.sleep(.3)
        self.signal(before,'controller',signal.SIGABRT)
        pending=service.wait(lambda s:s['version_recovery']['probe_pid'] is not None,timeout=8)
        self.assertEqual(pending['model_generation'],before['model_generation'])
        self.finish(service)
        bound=access_store.binding(service.directory,service.service_id)
        value=version_store.execute(bound,dict(sequence=1,op='load'))
        self.assertIsNone(value['switch'])

    def test_unqualified_fallback_binding_never_opens_business(self):
        service,before=self.launch(bad_fallback=True);time.sleep(.3)
        self.signal(before,'controller',signal.SIGABRT)
        failed=service.wait(lambda s:s['version_recovery']['fallback_consumed'] and
            s['model_generation']>before['model_generation'] and
            s['state']=='RECOVERING',timeout=20)
        self.assertFalse(failed['business_ready'])
        self.assertTrue(failed['version_recovery']['pending'])
        self.assertEqual(failed['version_recovery']['selected'],'primary')
        self.finish(service)


if __name__=='__main__':unittest.main()
