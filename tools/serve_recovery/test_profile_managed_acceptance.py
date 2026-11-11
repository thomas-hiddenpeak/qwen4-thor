"""Actual CPU helper lifetime and sealed-production import controls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools.serve_recovery import bundle
from tools.serve_recovery import profile_managed_acceptance as stage


class ManagedDriverTests(unittest.TestCase):
    def setUp(self):
        root=Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',stage.ROOT/'build/profile-driver-tests'))
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root);self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)

    def helper(self, closed):
        reader,writer=os.pipe2(os.O_CLOEXEC)
        config=dict(target_mib=2,floor_mib=1,max_mib=8,chunk_mib=1,seconds=3)
        if closed:os.close(writer)
        try:
            result=subprocess.run([sys.executable,'-I','-B',str(Path(stage.__file__).resolve()),
                'pressure','--output',str(self.path/'pressure.json'),
                '--release-fd',str(reader),'--pressure-config',json.dumps(config)],
                pass_fds=(reader,),capture_output=True,text=True,timeout=7)
        finally:
            os.close(reader)
            if not closed:os.close(writer)
        self.assertEqual(result.stderr,'')
        return result.returncode,json.loads((self.path/'pressure.json').read_text())

    def test_actual_pressure_helper_owner_eof_releases_without_allocation(self):
        code,value=self.helper(True)
        self.assertEqual(code,0)
        self.assertEqual(value['reason'],'released_by_owner')
        self.assertEqual(value['allocated_bytes'],0)
        self.assertTrue(value['all_owned_pages_released'])

    def test_actual_pressure_helper_cannot_exceed_allocation_budget(self):
        code,value=self.helper(False)
        self.assertEqual(code,2)
        self.assertEqual(value['reason'],'allocation_bound')
        self.assertEqual(value['allocated_bytes'],8*1024**2)
        self.assertTrue(value['all_owned_pages_released'])
        self.assertTrue(all(o['available_bytes'] > 2*1024**2 for o in value['observations']))

    def test_managed_driver_loads_sealed_controller_and_profile_runtime(self):
        for name in bundle.SOURCES:
            target=self.path/'source'/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(stage.ROOT/name,target)
        code='''import runpy,sys
from pathlib import Path
scope=runpy.run_path(sys.argv[1])
from tools.release.release import child_subreaper
scope['LEGACY']['load_modules']({'package':{'package':sys.argv[2]}})
from tools.serve_recovery import controller,profile_controller,profile_runtime
for module in (controller,profile_controller,profile_runtime):
 assert Path(module.__file__).resolve().is_relative_to(Path(sys.argv[2])/'source')
assert scope['LEGACY']['verify'].__globals__['limits_for']('adaptive_profiles') == scope['LIMITS']
print('PASS_SEALED_PROFILE_RUNTIME')
'''
        result=subprocess.run([sys.executable,'-I','-B','-c',code,
            str(Path(stage.__file__).resolve()),str(self.path)],
            capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,'PASS_SEALED_PROFILE_RUNTIME\n')


if __name__=='__main__':unittest.main()
