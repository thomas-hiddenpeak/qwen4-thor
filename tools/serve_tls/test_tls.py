"""Pinned TLS source, fresh/unknown semantics and independently runnable packages."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.serve_tls import core as c,source as s,observer as o
from tools.serve_host import observer as host,test_host
from tools.serve_gateway import managed as m,tls
from tools.serve_service import package as pkg
from tools.serve_notify import notify as n,test_notify as nt

BASE=pkg.ROOT/'.q4t-work/serve-tls-observation-20261104'
PRIOR=pkg.ROOT/'.q4t-work/serve-tls-lifecycle-20261104'


def original():
    plan=pkg.read(PRIOR/'plan.json');gateway=plan['gateways'][0]
    policy=s.package(gateway['package'],gateway['manifest_sha256'])
    run=next(p/'run' for p in (PRIOR/'owned-generations/generations').iterdir()
        if pkg.read(p/'reservation.json')['binding']['package']==gateway['package'])
    return policy,run


class Source(unittest.TestCase):
    def setUp(self):
        parent=BASE/'cpu-fixtures';parent.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=parent));self.policy,run=original()
        self.run=self.root/'run';self.run.mkdir(mode=0o700)
        for name in ('controller.json','status.json','terminal.json'):
            shutil.copyfile(run/name,self.run/name);(self.run/name).chmod(0o600)
        self.policy['gateway_run']=str(self.run)

    def read(self,name):return pkg.read(self.run/name)
    def write(self,name,value):pkg.save(self.run/name,value)

    def running(self,age=1):
        (self.run/'terminal.json').unlink();now=s.clock()
        control=self.read('controller.json');control['owner']=m.sup.process_identity(os.getpid());self.write('controller.json',control)
        state=self.read('status.json');state.update(state='READY',owner=control['owner'],cleanup_confirmed=False,
            updated_unix=now['unix']-age,nginx=dict(control['owner'],pid=2147483646))
        state['tls']=tls.observe(self.policy['material'],now['unix']-age)
        state['listener']['owner']=state['nginx'];self.write('status.json',state)
        return now,state

    def active(self,now,bound=None):
        status=self.read('status.json')
        with patch.object(s,'live',return_value=True),patch.object(s.inspection,'process_row',return_value=dict(ppid=os.getpid())), \
            patch.object(s.m,'listener',return_value=status['listener']):return s.observe(self.policy,bound,now)

    def test_real_completed_generation_preserves_source_and_has_material_not_serving_claim(self):
        before={p.name:pkg.sha(p) for p in self.run.iterdir()}
        value,bound=s.observe(self.policy,None)
        self.assertEqual((value['state'],value['reason'],value['certificate']['state']),('STOPPED','none','VALID'))
        self.assertTrue(value['cleanup_confirmed']);self.assertIsNone(value['source_age_seconds'])
        self.assertIsNotNone(bound['nginx']);self.assertEqual(before,{p.name:pkg.sha(p) for p in self.run.iterdir()})

    def test_missing_unbound_waits_missing_bound_cannot_rebind(self):
        _,bound=s.observe(self.policy,None);self.run.rename(self.root/'renamed')
        value,new=s.observe(self.policy,bound);self.assertEqual((value['state'],new),('UNKNOWN',bound))
        value,new=s.observe(self.policy,None);self.assertEqual((value['state'],new),('WAITING',None))

    def test_controller_replacement_stays_unknown_with_original_binding(self):
        _,bound=s.observe(self.policy,None)
        control=self.read('controller.json');control['created_unix']+=1;self.write('controller.json',control)
        value,new=s.observe(self.policy,bound)
        self.assertEqual((value['state'],value['reason'],new),('UNKNOWN','binding_invalid',bound))

    def test_symlink_hardlink_permissions_and_nonfinite_json_rejected(self):
        original=(self.run/'controller.json').read_bytes()
        for kind in ('symlink','hardlink','public','corrupt'):
            path=self.run/'controller.json';path.unlink();saved=self.root/kind;saved.write_bytes(original);saved.chmod(0o600)
            if kind=='symlink':path.symlink_to(saved)
            elif kind=='hardlink':os.link(saved,path)
            else:path.write_bytes(original if kind=='public' else b'{"x":NaN}');path.chmod(0o644 if kind=='public' else 0o600)
            value,_=s.observe(self.policy,None);self.assertEqual(value['state'],'UNKNOWN')
            path.unlink();path.write_bytes(original);path.chmod(0o600)

    def test_fresh_ready_checks_exact_owner_parent_listener_and_tls_binding(self):
        now,status=self.running();value,bound=self.active(now)
        self.assertEqual((value['state'],value['source_age_seconds']),('READY',1))
        self.assertEqual(bound['nginx'],status['nginx']);self.assertEqual(bound['listener'],status['listener'])
        status['tls']['leaf_sha256']='0'*64;self.write('status.json',status)
        value,_=self.active(now,bound);self.assertEqual((value['state'],value['reason']),('UNKNOWN','binding_invalid'))

    def test_stale_wall_and_future_record_never_ready(self):
        now,status=self.running()
        for age in (31,-2):
            status['updated_unix']=now['unix']-age;self.write('status.json',status)
            value,_=self.active(now);self.assertEqual((value['state'],value['reason']),('UNKNOWN','source_stale'))
        status['updated_unix']=now['unix']-30;self.write('status.json',status)
        self.assertEqual(self.active(now)[0]['state'],'READY')

    def test_unchanged_bytes_age_by_boottime_even_if_wall_clock_stalls(self):
        now,_=self.running();_,bound=self.active(now)
        later=dict(now,boottime=now['boottime']+31)
        value,new=self.active(later,bound)
        self.assertEqual((value['state'],value['reason']),('UNKNOWN','source_stale'));self.assertEqual(new,bound)

    def test_partial_controller_is_not_pinned_before_complete_binding(self):
        now,_=self.running();complete=self.read('controller.json')
        partial=copy.deepcopy(complete);partial.pop('binding');partial.pop('claim_identity');partial['created_unix']=now['unix']
        self.write('controller.json',partial);value,bound=self.active(now)
        self.assertEqual((value['state'],bound),('STARTING',None))
        self.write('controller.json',complete);value,bound=self.active(now)
        self.assertEqual(value['state'],'READY');self.assertIsNotNone(bound)

    def test_boot_changed_or_missing_live_owner_is_unknown(self):
        value,_=s.observe(self.policy,None,dict(s.clock(),boot_id='00000000-0000-0000-0000-000000000000'))
        self.assertEqual((value['state'],value['reason']),('UNKNOWN','boot_changed'))
        (self.run/'terminal.json').unlink()
        value,_=s.observe(self.policy,None);self.assertEqual((value['state'],value['reason']),('UNKNOWN','owner_absent'))

    def test_terminal_mismatch_or_live_owner_never_claims_stopped(self):
        with patch.object(s,'live',return_value=True):value,_=s.observe(self.policy,None)
        self.assertEqual(value['state'],'UNKNOWN')
        state=self.read('status.json');state['updated_unix']+=1;self.write('status.json',state)
        self.assertEqual(s.observe(self.policy,None)[0]['state'],'UNKNOWN')

    def test_setup_failure_without_status_is_observable(self):
        (self.run/'status.json').unlink()
        control=self.read('controller.json');control.pop('binding');control.pop('claim_identity');self.write('controller.json',control)
        self.write('terminal.json',dict(state='FAILED',cleanup_confirmed=True,automatic_reactivation=False,backend_stopped_by_gateway=False))
        value,bound=s.observe(self.policy,None)
        self.assertEqual((value['state'],value['reason']),('FAILED','gateway_failure'));self.assertIsNotNone(bound)

    def test_changed_public_material_omits_dates_without_reading_secrets(self):
        source=Path(self.policy['gateway_package']);target=self.root/'package';(target/'gateway').mkdir(parents=True,mode=0o700)
        target.chmod(0o700)
        for name in ('manifest.json','gateway/manifest.json','gateway/certificate.pem','gateway/trusted-ca.pem'):
            shutil.copyfile(source/name,target/name);(target/name).chmod(0o600)
        self.policy['gateway_package']=str(target)
        (target/'gateway/certificate.pem').write_bytes(b'changed')
        value,_=s.observe(self.policy,None)
        self.assertEqual((value['state'],value['reason']),('UNKNOWN','package_unavailable'))
        wire=c.metrics(value,'fixture','a'*64)
        self.assertNotIn('q4t_tls_not_after_timestamp_seconds',wire)

    def test_package_reader_never_reads_key_policy_or_nginx_configuration(self):
        original=s.Directory.read;read_file=m.g.read_file;root=Path(self.policy['gateway_package'])
        forbidden=('private-key.pem','client-policy.json','nginx.conf')
        def read(directory,name,*a,**kw):
            if directory.path.is_relative_to(root):self.assertNotIn(name,forbidden)
            return original(directory,name,*a,**kw)
        def file(path,*a,**kw):
            if Path(path).is_relative_to(root):self.assertNotIn(Path(path).name,forbidden)
            return read_file(path,*a,**kw)
        with patch.object(s.Directory,'read',read),patch.object(m.g,'read_file',file):
            s.package(root,self.policy['gateway_manifest_sha256']);s.observe(self.policy,None)

    def test_fixed_labels_private_fields_absent_and_material_time_boundaries(self):
        material=self.policy['material'];end=material['not_after'];begin=material['not_before']
        for now,state in ((begin-1,'NOT_YET_VALID'),(begin,'VALID'),(end-604800,'EXPIRING'),(end,'EXPIRED')):
            self.assertEqual(c.certificate(material,now)['state'],state)
        value,_=s.observe(self.policy,None);wire=c.metrics(value,'fixture','a'*64)
        for item in ('gateway_run','pid=',str(pkg.ROOT),self.policy['listen_host'],material['chain'][0]['sha256']):
            self.assertNotIn(item,wire)
        with self.assertRaises(c.Invalid):c.metrics(value,'bad"label','a'*64)

    def test_static_metrics_command_keeps_original_evaluation_time(self):
        receipt=o.prepare(self.policy['gateway_package'],self.policy['gateway_manifest_sha256'],self.run,'fixture',
            Path(self.policy['gateway_package'])/'nginx',19450,self.root/'observer')
        path=Path(receipt['state']);value=o.observe_once(path,receipt['policy_sha256'])
        expected=c.metrics(value,'fixture',receipt['policy_sha256']).encode()
        result=subprocess.run([sys.executable,'-I','-B',str(path/'source/tools/serve_tls/observer.py'),'metrics',
            '--state',str(path),'--policy-sha256',receipt['policy_sha256']],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode());self.assertEqual(result.stdout,expected)
        self.assertNotIn('[Install]',o.unit(path,receipt['policy_sha256']))
        for name in ('observer.py','child.py'):
            result=subprocess.run([sys.executable,'-I','-B',str(path/'source/tools/serve_tls'/name),'--help'],capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr.decode())


class Packages(unittest.TestCase):
    def setUp(self):
        parent=BASE/'cpu-fixtures';parent.mkdir(parents=True,exist_ok=True);self.root=Path(tempfile.mkdtemp(dir=parent))

    def test_old_source_layout_demonstrates_missing_tls_and_new_host_entry_is_independent(self):
        for label,files,entry in (('host',host.LEGACY_FILES,'tools/serve_host/observer.py'),('notify',n.SOURCES,'tools/serve_notify/notify.py')):
            path=self.root/label;path.mkdir(mode=0o700)
            for name in files:
                target=path/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((pkg.ROOT/name).read_bytes())
            result=subprocess.run([sys.executable,'-I','-B',str(path/entry),'--help'],capture_output=True,timeout=10)
            (path/'missing-dependency.log').write_bytes(result.stderr)
            self.assertNotEqual(result.returncode,0);self.assertIn(b'tls',result.stderr)
        policy,run=test_host.original();nginx=Path(original()[0]['gateway_package'])/'nginx'
        receipt=host.prepare(policy['backend_package'],policy['backend_manifest_sha256'],run,'fixture',nginx,19450,self.root/'host-fixed')
        path=Path(receipt['state']);self.assertEqual(host.verify(path,receipt['policy_sha256'])['version'],2)
        result=subprocess.run([sys.executable,'-I','-B',str(path/'source/tools/serve_host/observer.py'),'observe',
            '--state',str(path),'--policy-sha256',receipt['policy_sha256']],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode());self.assertEqual(json.loads(result.stdout)['state'],'STOPPED')

    def test_policy_v1_v2_v3_routes_are_explicit_and_ports_unique(self):
        base=nt.private_inputs(self.root)
        for version,extra,count in ((1,{},3),(2,dict(host_observer_port=19448),4),
            (3,dict(tls_observer_port=19450),4),(3,dict(tls_observer_port=19450,host_observer_port=19448),5)):
            p=n.policy(dict(base,version=version,**extra));configs=n.configs(p,self.root)
            self.assertEqual(len(configs['prometheus']['scrape_configs']),count)
            self.assertEqual('q4t-tls' in [v['job_name'] for v in configs['prometheus']['scrape_configs']],version==3)
        for bad in (dict(base,tls_observer_port=19450),dict(base,version=3),
            dict(base,version=3,tls_observer_port=base['runner_port']),dict(base,version=3,tls_observer_port=True)):
            with self.assertRaises(RuntimeError):n.policy(bad)

    def test_all_new_notify_policy_versions_have_isolated_v3_entries(self):
        for version in (1,2,3):
            private=self.root/f'private-{version}';private.mkdir(mode=0o700);p=nt.private_inputs(private)
            p['version']=version
            if version==2:p['host_observer_port']=19448
            if version==3:p['tls_observer_port']=19450
            pkg.save(private/'policy-private.json',p)
            receipt=n.prepare(private/'policy-private.json',nt.dependencies(),self.root/f'notify-{version}')
            path=Path(receipt['package']);manifest=n.verify(path,receipt['manifest_sha256'])
            self.assertEqual((manifest['version'],manifest['policy_version']),(3,version))
            result=subprocess.run([sys.executable,'-I','-B',str(path/'source/tools/serve_notify/notify.py'),'check',
                '--package',str(path),'--manifest-sha256',receipt['manifest_sha256']],capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            result=subprocess.run([sys.executable,'-I','-B',str(path/'source/tools/serve_notify/child.py'),'--help'],capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr.decode())

    def test_legacy_host_and_notify_snapshot_entries_remain_usable(self):
        prior=pkg.read(pkg.ROOT/'.q4t-work/serve-host-observation-20261104/projection-corrected/plan.json')
        host_state=Path(prior['observer']['state']);front=prior['notification'];path=Path(front['package'])
        result=subprocess.run([sys.executable,'-I','-B',str(host_state/'source/tools/serve_host/observer.py'),'status',
            '--state',str(host_state),'--policy-sha256',prior['observer']['policy_sha256']],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        result=subprocess.run([sys.executable,'-I','-B',str(path/'source/tools/serve_notify/notify.py'),'check',
            '--package',str(path),'--manifest-sha256',front['manifest_sha256']],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode())


if __name__=='__main__':unittest.main()
