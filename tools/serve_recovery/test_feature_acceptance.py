"""Bounded driver negative controls; synthetic records never certify hardware."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from tools.serve_recovery import bundle, feature_acceptance as stage


class FeatureDriverTests(unittest.TestCase):
    def setUp(self):
        root=Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',stage.ROOT/'build/feature-driver-tests'))
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)

    def test_each_stage_enforces_its_own_exact_predispatch_budget(self):
        for scenario,limits in stage.LIMITS.items():
            output=self.path/scenario;output.mkdir()
            counter=stage.LEGACY['Budget'](output,initialize=True,limits=limits)
            self.assertEqual(stage.LEGACY['verify'].__globals__['limits_for'](scenario),limits)
            for kind in ('starts','chats','probes'):
                for _ in range(limits[kind]):counter.reserve(kind,'CPU_FIXTURE')
                with self.assertRaisesRegex(RuntimeError,'budget exhausted'):
                    counter.reserve(kind,'NO_DISPATCH')
            self.assertEqual(json.loads((output/'counts.json').read_text())['counts'],
                {k:v for k,v in limits.items() if k!='seconds'}|dict(controls=0))

    def test_plaintext_mixed_leaked_or_unfinished_refusal_cannot_pass(self):
        valid=SimpleNamespace(finish_reason='stop',message=dict(role='assistant',
            content=None,refusal='I cannot disclose the private marker.'))
        stage.check_refusal(valid)
        for update in [dict(content='I refuse.'),dict(refusal=''),dict(refusal='  '),
            dict(refusal=stage.MARKER),dict(refusal='<q4t_refusal>no</q4t_refusal>'),
            dict(tool_calls=[dict(id='call')]),dict(function_call=dict(name='function'))]:
            with self.subTest(update=update),self.assertRaises(RuntimeError):
                stage.check_refusal(SimpleNamespace(finish_reason='stop',
                    message=dict(valid.message,**update)))
        with self.assertRaises(RuntimeError):
            stage.check_refusal(SimpleNamespace(finish_reason='length',message=valid.message))
        request=stage.requests();request['refusal']['messages'].clear()
        self.assertEqual(len(stage.requests()['refusal']['messages']),2)

    def test_partial_business_bad_environment_or_missing_lease_cannot_certify(self):
        limits=stage.LIMITS['feature_retained']
        plan=dict(scenario='feature_retained',limits=limits,
            package=dict(package='/synthetic',manifest_sha256='a'*64),command=['CPU_ONLY'])
        result=dict(status='PASS_BOUNDED_FEATURE_SERVICE',failure=None,cleanup_failure=None,
            package=plan['package'],scenario=plan['scenario'],seconds=3,
            budget=dict(counts={k:v for k,v in limits.items() if k!='seconds'}))
        guard=dict(status='COMPLETED',child_returncode=0,compute_backend='nvml',
            lease_inherited_by_child=True,external_lease=False,command=plan['command'])
        queries=[dict(status='OK',returncode=0)]
        events=[dict(event='lease_acquired',pid=1,monotonic=10),
            dict(event='lease_released',pid=1,monotonic=14,status='COMPLETED')]
        self.assertEqual(stage.accepted_run(plan,result,guard,queries,events),
            dict(strict_lease_seconds=4,nvml_queries=1,env_abort=False))
        for key,value in [('status','ENV_ABORT'),('status','COMPLETED_WITH_MONITOR_GAPS'),
            ('child_returncode',1),('compute_backend','nvidia-smi'),
            ('lease_inherited_by_child',False),('external_lease',True),('command',['other'])]:
            with self.subTest(guard=key),self.assertRaises(RuntimeError):
                stage.accepted_run(plan,result,dict(guard,**{key:value}),queries,events)
        for invalid in [[],[dict(status='TIMEOUT',returncode=0)],[dict(status='OK',returncode=1)]]:
            with self.assertRaises(RuntimeError):stage.accepted_run(plan,result,guard,invalid,events)
        for invalid in [[],events[:1],events+events, [events[0],dict(events[1],monotonic=1000)]]:
            with self.assertRaises(RuntimeError):stage.accepted_run(plan,result,guard,queries,invalid)
        for key,value in [('status','FAILED'),('failure','interrupted'),('cleanup_failure','survivor'),
            ('seconds',301),('package',dict(package='/other')),('scenario','feature_version')]:
            with self.assertRaises(RuntimeError):
                stage.accepted_run(plan,dict(result,**{key:value}),guard,queries,events)
        for kind,value in [('starts',0),('chats',1),('probes',1),('controls',1501),('chats',True)]:
            bad=copy.deepcopy(result);bad['budget']['counts'][kind]=value
            with self.assertRaises(RuntimeError):stage.accepted_run(plan,bad,guard,queries,events)

    def test_selected_sealed_imports_and_owned_fault_wrapper_path(self):
        package=self.path/'payload'
        for name in bundle.SOURCES:
            target=package/'source'/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(stage.ROOT/name,target)
        script='''import runpy,sys
from pathlib import Path
s=runpy.run_path(sys.argv[1])
s['LEGACY']['load_modules'](dict(package=dict(package=sys.argv[2])))
from tools.serve_recovery import controller,native_ingress
for m in (controller,native_ingress):
 assert Path(m.__file__).is_relative_to(Path(sys.argv[2])/'source')
for f in (s['VERSION']['launcher'],s['MANAGED']['launcher'],s['VERSION']['Stage'].fatal_gateway):
 assert Path(f.__globals__['__file__']).resolve()==Path(sys.argv[1]).resolve()
print('PASS_CPU_SEALED_SOURCE')
'''
        result=subprocess.run([sys.executable,'-I','-B','-c',script,stage.__file__,str(package)],
            capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,'PASS_CPU_SEALED_SOURCE\n')

    def test_first_stage_probe_rejected_before_any_device_or_module_load(self):
        output=self.path/'execution';output.mkdir()
        stage.LEGACY['Budget'](output,initialize=True,limits=stage.LIMITS['feature_retained'])
        plan=self.path/'plan.json'
        plan.write_text(json.dumps(dict(scenario='feature_retained',
            output=str(self.path),limits=stage.LIMITS['feature_retained'])))
        result=subprocess.run([sys.executable,'-I','-B',stage.__file__,'probe','--plan',str(plan)],
            capture_output=True,text=True,timeout=5)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('probes acceptance budget exhausted',result.stderr)
        self.assertNotIn('load_modules',result.stderr)
        self.assertEqual(json.loads((output/'counts.json').read_text())['counts']['probes'],0)


if __name__=='__main__':unittest.main()
