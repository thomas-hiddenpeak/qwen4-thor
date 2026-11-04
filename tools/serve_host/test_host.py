"""Generation identity, stale/unknown semantics and compatible monitor wiring."""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.serve_host import core as c,source as s,observer as o
from tools.serve_service import package as pkg
from tools.serve_notify import notify as n,test_notify as nt

BASE=pkg.ROOT/'.q4t-work/serve-host-observation-20261104'
PREVIOUS=pkg.ROOT/'.q4t-work/serve-host-envelope-20261104'


def original():
    plan=pkg.read(PREVIOUS/'plan.json')
    roots=list((PREVIOUS/'owned-generations/generations').iterdir())
    run=next(path/'run' for path in roots if pkg.read(path/'reservation.json')['kind']=='backend')
    policy=dict(backend_run=str(run),backend_package=plan['package']['package'],
        backend_manifest_sha256=plan['package']['manifest_sha256'],host_resources=plan['host_resources'])
    return policy,run


class Host(unittest.TestCase):
    def setUp(self):
        parent=BASE/'cpu-fixtures';parent.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=parent));self.root.chmod(0o700)
        self.policy,run=original();self.run=self.root/'source';self.run.mkdir(mode=0o700)
        for name in ('controller.json','status.json','terminal.json','resources.json','resources-final.json'):
            shutil.copyfile(run/name,self.run/name);(self.run/name).chmod(0o600)
        self.policy['backend_run']=str(self.run)

    def read(self,name):return pkg.read(self.run/name)
    def write(self,name,value):pkg.save(self.run/name,value)

    def test_completed_real_generation_keeps_final_evidence_without_starting_it(self):
        before={p.name:pkg.sha(p) for p in self.run.iterdir()}
        value,bound=s.observe(self.policy,None)
        self.assertEqual((value['state'],value['kind'],value['reason']),('STOPPED','final','none'))
        self.assertTrue(value['cleanup_confirmed'])
        self.assertEqual(value['resources']['memory.peak'],17109327872)
        self.assertEqual(before,{p.name:pkg.sha(p) for p in self.run.iterdir()})
        again,new=s.observe(self.policy,bound);self.assertEqual(bound,new)
        self.assertEqual(again['resources'],value['resources'])

    def test_never_created_is_waiting_but_missing_bound_directory_is_unknown(self):
        value,bound=s.observe(self.policy,None)
        target=self.root/'renamed';self.run.rename(target)
        value,same=s.observe(self.policy,bound)
        self.assertEqual(value['state'],'UNKNOWN');self.assertEqual(same,bound)
        value,_=s.observe(self.policy,None);self.assertEqual(value['state'],'WAITING')

    def test_replaced_directory_or_controller_does_not_rebind(self):
        _,bound=s.observe(self.policy,None)
        control=self.read('controller.json');control['created_unix']+=1;self.write('controller.json',control)
        value,new=s.observe(self.policy,bound)
        self.assertEqual((value['state'],value['reason']),('UNKNOWN','binding_invalid'));self.assertEqual(new,bound)

    def test_symlink_hardlink_public_permissions_and_corrupt_metadata_are_unknown(self):
        for kind in ('symlink','hardlink','mode','corrupt'):
            with self.subTest(kind=kind):
                original=(self.run/'controller.json').read_bytes();saved=self.root/('saved-'+kind);saved.write_bytes(original);saved.chmod(0o600)
                target=self.run/'controller.json';target.unlink()
                if kind=='symlink':target.symlink_to(saved)
                elif kind=='hardlink':os.link(saved,target)
                else:target.write_bytes(original if kind=='mode' else b'{"x":NaN}');target.chmod(0o644 if kind=='mode' else 0o600)
                value,_=s.observe(self.policy,None)
                self.assertEqual(value['state'],'UNKNOWN');self.assertIsNone(value['resources'])
                target.unlink();target.write_bytes(original);target.chmod(0o600)

    def test_boot_change_never_presents_old_stopped_receipt_as_current(self):
        now=s.clock();now['boot_id']='11111111-2222-3333-4444-555555555555'
        value,_=s.observe(self.policy,None,now)
        self.assertEqual((value['state'],value['reason']),('UNKNOWN','boot_changed'))

    def test_old_terminal_cannot_hide_live_owner(self):
        with patch.object(s,'live',return_value=True):value,_=s.observe(self.policy,None)
        self.assertEqual((value['state'],value['reason']),('UNKNOWN','owner_absent'))

    def test_resource_record_replacement_and_terminal_mismatch_are_unknown(self):
        _,bound=s.observe(self.policy,None)
        v=self.read('resources.json');v['token']+='changed';self.write('resources.json',v)
        value,new=s.observe(self.policy,bound)
        self.assertEqual(value['state'],'UNKNOWN');self.assertEqual(new,bound)

    def running(self,age=1):
        (self.run/'terminal.json').unlink();(self.run/'resources-final.json').unlink()
        worker=self.read('status.json');controller=self.read('controller.json')
        record=self.read('resources.json')
        worker['state']='READY';worker['backend']=dict(
            pid=99999997,start_ticks=123,boot_id=controller['owner']['boot_id'])
        worker['host_resources']['observation']['sampled_monotonic']=100-age
        worker['updated_unix']=time.time()
        worker['host_resources']['payload_removed']=False;worker['host_resources']['violation']=None
        self.write('status.json',worker)
        now=s.clock();now['monotonic']=100
        snapshot=copy.deepcopy(worker['host_resources']['observation'])
        def proc(pid):return dict(ppid=controller['owner']['pid'] if pid==worker['owner']['pid'] else worker['owner']['pid'])
        def membership(pid):return '/'+str((Path(record['root'])/('control' if pid==worker['owner']['pid'] else 'payload')).relative_to(s.r.CGROUP))
        return worker,now,snapshot,proc,membership

    def test_fresh_running_reads_actual_sampler_and_checks_ancestry(self):
        worker,now,snapshot,proc,membership=self.running()
        with patch.object(s,'live',return_value=True),patch.object(s.inspection,'process_row',side_effect=proc),\
             patch.object(s.r,'membership',side_effect=membership),patch.object(s.r,'sample',return_value=snapshot) as sample:
            value,bound=s.observe(self.policy,None,now)
        self.assertEqual((value['state'],value['kind']),('RUNNING','current'));self.assertTrue(value['backend_ready'])
        sample.assert_called_once();self.assertIsNotNone(bound['resource_sha256'])

    def test_stale_or_future_worker_does_not_export_old_values(self):
        for age in (31,-1):
            root=self.run;folder=self.root/('age-'+str(age));shutil.copytree(root,folder)
            with self.subTest(age=age):
                self.run=folder;self.policy['backend_run']=str(folder)
                _,now,_,_,_=self.running(age)
                with patch.object(s,'live',return_value=True),patch.object(s.inspection,'process_row',return_value=dict(ppid=self.read('controller.json')['owner']['pid'])):
                    value,bound=s.observe(self.policy,None,now)
                self.assertEqual((value['state'],value['reason']),('UNKNOWN','source_stale'))
                self.assertIsNone(value['resources']);self.assertIsNotNone(bound)
            self.run=root;self.policy['backend_run']=str(root)

    def test_lost_controller_without_terminal_is_unknown_not_stopped(self):
        (self.run/'terminal.json').unlink()
        value,_=s.observe(self.policy,None)
        self.assertEqual((value['state'],value['reason']),('UNKNOWN','owner_absent'))

    def test_sampler_failure_keeps_first_controller_binding(self):
        _,now,_,proc,membership=self.running()
        with patch.object(s,'live',return_value=True),patch.object(s.inspection,'process_row',side_effect=proc),\
             patch.object(s.r,'membership',side_effect=membership),patch.object(s.r,'sample',side_effect=FileNotFoundError):
            value,bound=s.observe(self.policy,None,now)
        self.assertEqual(value['state'],'UNKNOWN');self.assertIsNotNone(bound)

    def test_real_kernel_fault_counters_project_without_zeroing_or_current_claim(self):
        for case,reason in (('oom','host_resource_oom'),('tasks','host_resource_tasks')):
            original=pkg.read(PREVIOUS/'kernel-02'/case/'resources-final.json')
            values=c.snapshot(original['final'],original['record']['policy'])
            value=c.view('FAILED',reason,kind='final',values=values,cleanup=True);value['evaluated_unix']=1
            text=c.metrics(value,'fixture','a'*64)
            self.assertIn('kind="final"} 1',text);self.assertNotIn('PID',text)
            self.assertGreater(values['memory.events']['oom_kill'] if case=='oom' else values['pids.events']['max'],0)

    def test_unknown_metrics_omit_resource_values_and_keep_original_timestamp(self):
        value=c.view('UNKNOWN','source_stale');value['evaluated_unix']=123
        text=c.metrics(value,'fixture','a'*64)
        self.assertNotIn('q4t_host_memory_current_bytes',text)
        self.assertIn('q4t_host_evaluated_timestamp_seconds',text);self.assertIn('} 123\n',text)
        for private in ('backend_run','pid','api_key','/sys/fs','model_dir'):self.assertNotIn(private,text)

    def test_boolean_fractional_and_nonfinite_resource_values_rejected(self):
        raw=self.read('resources-final.json')['final']
        for bad in (True,-1,.5,float('nan'),float('inf')):
            value=copy.deepcopy(raw);value['memory.current']=bad
            with self.assertRaises(c.Invalid):c.snapshot(value,self.policy['host_resources'])
        value=copy.deepcopy(raw);value['effective_limits']['pids.max']=True
        with self.assertRaises(c.Invalid):c.snapshot(value,self.policy['host_resources'])

    def test_pinned_package_nginx_syntax_read_only_observe_and_exclusive_claim(self):
        plan=pkg.read(PREVIOUS/'plan.json');nginx=Path(plan['gateways'][0]['package'])/'nginx'
        receipt=o.prepare(self.policy['backend_package'],self.policy['backend_manifest_sha256'],self.run,
            'fixture',nginx,19448,self.root/'observer')
        state=Path(receipt['state']);digest=receipt['policy_sha256']
        value=o.observe_once(state,digest);self.assertEqual(value['state'],'STOPPED')
        before={p.name:pkg.sha(p) for p in state.iterdir() if p.is_file()}
        with o.Directory(state) as directory,directory.claim('observer.lock',0):
            self.assertNotEqual(o.watch(state,digest),0)
        self.assertEqual(before,{p.name:pkg.sha(p) for p in state.iterdir() if p.is_file()})
        self.assertNotIn('[Install]',o.unit(state,digest))
        self.assertFalse((state/'nginx.pid').exists())

    def test_notify_v1_unchanged_v2_explicit_unique_port_and_generation_group(self):
        p=dict(version=1,deployment='fixture',runner_port=18128,prometheus_port=19095,
            alertmanager_port=19096,receiver_url='https://ops.example/alerts',receiver_token_file='/private/token')
        old=n.configs(n.policy(p),self.root)
        self.assertEqual(len(old['prometheus']['scrape_configs']),3)
        for bad in (dict(p,host_observer_port=19448),dict(p,version=2),dict(p,version=2,host_observer_port=18128),
                    dict(p,version=2,host_observer_port=True)):
            with self.assertRaises(RuntimeError):n.policy(bad)
        new=n.configs(n.policy(dict(p,version=2,host_observer_port=19448)),self.root)
        self.assertEqual(new['prometheus']['scrape_configs'][-1]['job_name'],'q4t-host')
        self.assertIn('generation',new['alertmanager']['route']['group_by'])
        self.assertNotIn('generation',old['alertmanager']['route']['group_by'])

    def test_real_notify_v2_configuration_checks_and_package_verification(self):
        p=nt.private_inputs(self.root);p.update(version=2,host_observer_port=19448)
        path=self.root/'policy-private.json';pkg.save(path,p)
        result=n.prepare(path,nt.dependencies(),self.root/'notify')
        manifest=n.verify(result['package'],result['manifest_sha256'])
        self.assertEqual((manifest['version'],manifest['policy_version']),(3,2))
        self.assertIn('source/deploy/serve-ops/q4t-host.rules.yml',manifest['files'])
        self.assertEqual(manifest['ports']['host_observer'],19448)
