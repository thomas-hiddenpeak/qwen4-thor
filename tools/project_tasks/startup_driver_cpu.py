"""Driver ownership, exact prior reader and public declaration boundary."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock,patch

from tools.project_tasks import startup_stage as s,protocol_driver_cpu as old
from tools.project_tasks import transport,test_errors,test_tasks


class Driver(old.Driver):
    def test_actual_11j_profile_boundary_and_binding(self):
        output,_=self.fixture();runtime=output/'historical'
        s.history.extract_runtime(9,runtime)
        private=runtime/'.q4t-work/private';private.mkdir(mode=0o700,parents=True)
        ca=private/'ca.pem';ca.write_bytes(test_errors.CA.read_bytes())
        value={k:v for k,v in test_tasks.BINDING.items() if k not in ('ca_sha256','key_sha256')}
        value.update(api_key='cpu-profile-boundary-'+'x'*40,ca_file=str(ca),
                     protocol_sha256=s.review.BEFORE,template_sha256=s.review.TEMPLATE)
        profile=private/'profile.json';s.pkg.save(profile,value)
        expected=transport.profile(profile)[1]
        script='from tools.project_tasks import transport;import json,sys;print(json.dumps(transport.profile(sys.argv[1])[1]))'
        child=subprocess.run([sys.executable,'-B','-c',script,str(profile)],cwd=runtime,
            capture_output=True,text=True,timeout=5)
        self.assertEqual(child.returncode,0,child.stderr);self.assertEqual(json.loads(child.stdout),expected)
        external=output/'outside.json';s.pkg.save(external,value)
        child=subprocess.run([sys.executable,'-B','-c',script,str(external)],cwd=runtime,
            capture_output=True,text=True,timeout=5)
        self.assertNotEqual(child.returncode,0);self.assertIn('use build or .q4t-work',child.stderr)

    def test_unreviewed_public_capability_change_rejected(self):
        output,_=self.fixture()
        before=dict(protocol_source_sha256=s.review.BEFORE,tools=dict(safe=True))
        s.pkg.save(output/'capabilities-0.json',before)
        service=Mock();service.request.return_value=dict(before,
            protocol_source_sha256=s.review.AFTER,tools=dict(safe=False))
        run=s.Run(dict(versions=[dict(protocol=s.review.BEFORE),dict(protocol=s.review.AFTER)]))
        with patch.object(s,'OUT',output),patch.object(s.prior.Run,'launch',return_value=service):
            with self.assertRaisesRegex(RuntimeError,'unreviewed public capability'):run.launch(1)

    def test_fixed_absolute_guard_and_driver_entry(self):
        argv=s.guard_command();sep=argv.index('--')
        self.assertEqual(Path(argv[2]),s.ROOT/'tools/evalscope/gpu_exclusive.py')
        self.assertEqual(argv[3:sep],['--output',str(s.OUT/'lease')])
        self.assertEqual(argv[sep+3:],[str(s.ROOT/'tools/project_tasks/startup_stage.py'),'run'])
        self.assertTrue(Path(argv[2]).is_absolute() and Path(argv[sep+3]).is_absolute())


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(Driver(n) for n in loader.getTestCaseNames(Driver)
        if n!='test_real_11g_profile_and_ca_must_be_inside_historical_root')
