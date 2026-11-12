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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_recovery import bundle
import runpy
stage = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name('tool_managed_acceptance_v2.py'))))


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

    def test_partial_business_bad_environment_or_missing_lease_cannot_certify(self):
        limits=stage.LIMITS['tool_retained']
        plan=dict(scenario='tool_retained',limits=limits,
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
            ('seconds',301),('package',dict(package='/other')),('scenario','tool_version')]:
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
        stage.LEGACY['Budget'](output,initialize=True,limits=stage.LIMITS['tool_retained'])
        plan=self.path/'plan.json'
        plan.write_text(json.dumps(dict(scenario='tool_retained',
            output=str(self.path),limits=stage.LIMITS['tool_retained'])))
        result=subprocess.run([sys.executable,'-I','-B',stage.__file__,'probe','--plan',str(plan)],
            capture_output=True,text=True,timeout=5)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('probes acceptance budget exhausted',result.stderr)
        self.assertNotIn('load_modules',result.stderr)
        self.assertEqual(json.loads((output/'counts.json').read_text())['counts']['probes'],0)

    def test_actual_wire_and_mutations_of_identity_result_or_stream(self):
        wire=(Path(__file__).parent/'business-03/function-call.wire').read_bytes()
        value=stage.parse_response(wire, False)
        call,args=stage.check_call(value)
        for key,wrong in [('namespace','catalog'),('name','other'),('arguments','{}'),('async',False),('caller',None)]:
            changed=copy.deepcopy(value)
            item=next(x for x in changed['output'] if x['type']=='function_call')
            item[key]=wrong
            with self.assertRaises(RuntimeError):stage.check_call(changed)
        changed=copy.deepcopy(value);changed['metadata']={'leak':'q4t_ns_private'}
        with self.assertRaises(RuntimeError):stage.parse_response(json.dumps(changed).encode(),False)
        changed=copy.deepcopy(value);changed['status']='incomplete'
        with self.assertRaises((RuntimeError,ValueError)):
            stage.parse_response(json.dumps(changed).encode(),False)
        missing=copy.deepcopy(value)
        missing['output']=[x for x in missing['output'] if x['type']!='message']
        with self.assertRaises(RuntimeError):stage.check_call(missing)
        stream=(Path(__file__).parent/'business-03/custom-call.wire').read_bytes()
        self.assertEqual(stage.parse_response(stream,True)['status'],'completed')
        rows=stage.requests()
        self.assertLessEqual(rows['tool']['max_output_tokens'],256)
        self.assertEqual(rows['follow']['tools'],[])
        self.assertEqual(rows['follow']['tool_choice'],'none')
        output=rows['tool']['tools'][0]['tools'][0]['output_schema']
        self.assertEqual(output['properties']['ok'],{'const':True})

    def test_actual_stored_projection_preserves_every_identity_and_text(self):
        path=Path(__file__).parent/'managed-retained-02/execution'
        created=json.loads((path/'namespace-call.wire').read_text())
        stored=json.loads((path/'stored-call.wire').read_text())
        stage.check_stored(created, stored)
        for field,value in [('call_id','wrong'),('async',False),('namespace','other')]:
            changed=copy.deepcopy(stored);changed['output'][0][field]=value
            with self.assertRaises(RuntimeError):stage.check_stored(created,changed)
        changed=copy.deepcopy(stored);changed['output'][1]['content'][0]['text']='different'
        with self.assertRaises(RuntimeError):stage.check_stored(created,changed)
        changed=copy.deepcopy(created);changed['output'][1]['content'][0]['logprobs']=[{}]
        with self.assertRaises(RuntimeError):stage.check_stored(changed,stored)



if __name__=='__main__':unittest.main()
