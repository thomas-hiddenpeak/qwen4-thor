"""Resource policy, package binding and supervisory failure contracts on CPU."""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import sys
import unittest
from unittest.mock import patch

from tools.serve_service import resources as r, package as pkg, supervisor as sup
from tools.serve_service import test_service as old
from tools.serve_clients import test_packages as clients


class ResourceContracts(unittest.TestCase):
    setUp = old.Case.setUp
    fixture = old.PackageTests.fixture
    observation = old.Case.observation

    def test_policy_rejects_incomplete_fractional_boolean_and_unbounded_values(self):
        for args in ((None,64,8),(32,None,8),(64,32,8),(0,64,8),(32,64,True),
            (32.5,64,8),(32,64,4097),(32,128*1024+1,8)):
            with self.subTest(args=args), self.assertRaises(RuntimeError): r.policy(*args)
        valid=r.policy(32,64,8); self.assertEqual(r.validate(valid),valid)
        for key,value in (('memory_max_bytes',64*1024**2+1),('swap_max_bytes',1),
            ('physical_ram_limit',True),('oom_group',1),('control_max_bytes',0)):
            altered=dict(valid,**{key:value})
            with self.assertRaises(RuntimeError): r.validate(altered)

    def test_counter_parser_rejects_duplicate_negative_fraction_and_missing_fields(self):
        self.assertEqual(r.counters('oom 0\noom_kill 2\n'),dict(oom=0,oom_kill=2))
        for raw in ('oom 0\noom 1','oom -1','oom 1.5','oom 1 2','oom 1e9'):
            with self.subTest(raw=raw),self.assertRaises((ValueError,RuntimeError)):r.counters(raw)

    def test_oom_and_tasks_are_terminal_but_high_or_reclaimed_max_are_not(self):
        base={'memory.events':dict(high=100,max=100,oom=0,oom_kill=0),'pids.events':dict(max=0)}
        self.assertIsNone(r.violation(base))
        for key in ('oom','oom_kill'):
            v=deepcopy(base);v['memory.events'][key]=1
            self.assertEqual(r.violation(v),'host_resource_oom')
        base['pids.events']['max']=1
        self.assertEqual(r.violation(base),'host_resource_tasks')

    def test_foreign_scope_delegation_and_ancestor_rejected(self):
        unit='q4t-host-'+32*'a'+'.scope';token='owner'
        props=dict(Id=unit,LoadState='loaded',ActiveState='active',Description=token,
            Delegate='yes',DelegateControllers='memory pids',OOMPolicy='continue',InvocationID=32*'b')
        r.scope_matches(props,unit,token)
        for key,value in (('Description','other'),('Delegate','no'),('OOMPolicy','kill'),
            ('DelegateControllers','pids'),('InvocationID',''),('ActiveState','inactive')):
            with self.assertRaises(RuntimeError):r.scope_matches(dict(props,**{key:value}),unit,token)
        for path in ('/system.slice/'+unit,'/user.slice/../'+unit,'/user.slice/user-0.slice/user@0.service/'+unit):
            with self.assertRaises(RuntimeError):r.cgroup_path(path,unit)

    def test_v5_package_binds_policy_dependencies_and_client_parser_without_execution(self):
        binary,release,model=self.fixture()
        receipt=pkg.prepare(binary,release,model,self.root/'bounded',host_resources=r.policy(32,64,8))
        manifest=pkg.verify(receipt['package'],receipt['manifest_sha256'])
        self.assertEqual(manifest['version'],5)
        self.assertIn('source/tools/serve_service/resources.py',manifest['files'])
        self.assertFalse(manifest['host_resources']['physical_ram_limit'])
        self.assertNotIn('--host-memory-max-mib',pkg.command(receipt['package'],manifest))
        changed=deepcopy(manifest);changed['resource_dependencies']['busctl']['sha256']='0'*64
        target=Path(receipt['package'])/'manifest.json';pkg.save(target,changed)
        with self.assertRaisesRegex(RuntimeError,'manager changed'):pkg.verify(target.parent,pkg.sha(target))

    def test_v5_with_client_policy_keeps_both_boundaries(self):
        binary,release,model=self.fixture();path=self.root/'clients.json'
        pkg.save(path,clients.policy())
        receipt=pkg.prepare(binary,release,model,self.root/'bounded',client_policy=path,
            host_resources=r.policy(32,64,8))
        manifest=pkg.verify(receipt['package'],receipt['manifest_sha256'])
        self.assertEqual(manifest['version'],5)
        self.assertIn('client_policy',manifest)
        self.assertIn('--client-policy-file',pkg.command(receipt['package'],manifest))

    def test_legacy_package_cannot_claim_protection_and_fixed_packages_still_verify(self):
        binary,release,model=self.fixture();receipt=pkg.prepare(binary,release,model,self.root/'legacy')
        target=Path(receipt['package'])/'manifest.json';v=pkg.read(target)
        v['host_resources']=r.policy(32,64,8);pkg.save(target,v)
        with self.assertRaisesRegex(RuntimeError,'legacy package'):pkg.verify(target.parent,pkg.sha(target))
        fixed=pkg.ROOT/'.q4t-work/serve-clients-managed-20261104/backend-package'
        self.assertEqual(pkg.verify(fixed,pkg.sha(fixed/'manifest.json'))['version'],4)

    def supervise(self, case):
        output=self.root/'runtime';output.mkdir()
        cfg=pkg.settings(18998,3);cfg.update(poll_seconds=.01,startup_seconds=.5,stop_seconds=.1,kill_seconds=.2)
        manifest=dict(settings=cfg,repository=str(pkg.ROOT),protocol_sha256='protocol',files={'q4t':'binary'},
            host_resources=r.policy(32,64,8))
        command=[sys.executable,'-c','import time;time.sleep(30)']
        class Fake:
            def __init__(self,*args):
                self.selected=args[0];self.latest={};self.error=None;self.closed=False;self.checks=0
                if case=='init':raise RuntimeError('delegation absent')
            def check(self):
                self.checks+=1
                if self.checks>=2:
                    self.error='host_resource_oom' if case=='oom' else 'host_resource_observation_unavailable'
                    raise RuntimeError(self.error)
            def close(self):self.closed=True
        def observe(pid,port):
            if case=='oom':sup.signal_owner(sup.process_identity(pid),signal.SIGKILL)
            return self.observation(pid)
        with patch.object(pkg,'verify',return_value=manifest),patch.object(pkg,'command',return_value=command),\
            patch.object(sup,'backend_command',return_value=command),patch.object(r,'Envelope',Fake):
            code=sup.run(self.root,'fixture',output,observe=observe,
                read_memory=lambda:dict(total_bytes=8*1024**3,available_bytes=4*1024**3))
        return code,pkg.read(output/'status.json')

    def test_oom_signal_does_not_use_configured_restart_budget(self):
        code,state=self.supervise('oom')
        self.assertEqual((code,state['attempts'],state['restarts']),(1,1,0))
        self.assertEqual(state['error'],'host_resource_oom')
        self.assertTrue(state['cleanup_confirmed'])

    def test_missing_delegation_refuses_before_backend_start(self):
        code,state=self.supervise('init')
        self.assertEqual((code,state['attempts'],state['restarts']),(1,0,0))
        self.assertTrue(state['cleanup_confirmed'])

    def test_unavailable_observation_stops_owned_backend_without_restart(self):
        code,state=self.supervise('observe')
        self.assertEqual((code,state['attempts'],state['restarts']),(1,1,0))
        self.assertEqual(state['error'],'host_resource_observation_unavailable')
        self.assertTrue(state['cleanup_confirmed'])
