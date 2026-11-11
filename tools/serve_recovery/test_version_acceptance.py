"""Driver boundaries using CPU children, synthetic scripts and actual flock."""
import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.serve_recovery import bundle
from tools.serve_recovery import version_acceptance as stage


class DriverTests(unittest.TestCase):
    def setUp(self):
        root=Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',stage.ROOT/'build/version-driver-tests'))
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root);self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.output=self.path/'execution';self.output.mkdir(mode=0o700)
        stage.LEGACY['Budget'](self.output,initialize=True,limits=stage.LIMITS)

    def package(self,name):
        package=self.path/name
        for source in bundle.SOURCES:
            target=package/'source'/source;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(stage.ROOT/source,target)
        return package

    def test_each_actor_imports_its_selected_sealed_payload_and_exact_version_budget(self):
        primary=self.package('primary');fallback=self.package('fallback')
        plan=dict(registration=dict(payloads={name:dict(descriptor=dict(
            package=str(package),manifest_sha256='a'*64))
            for name,package in [('primary',primary),('fallback',fallback)]}))
        script='''import json,runpy,sys
from pathlib import Path
s=runpy.run_path(sys.argv[1]);p=s['payload_plan'](json.loads(sys.argv[2]),sys.argv[3])
s['LEGACY']['load_modules'](p)
from tools.serve_recovery import controller,native_ingress,profile_runtime
for module in (controller,native_ingress,profile_runtime):
 assert Path(module.__file__).is_relative_to(Path(p['package']['package'])/'source')
assert s['LEGACY']['verify'].__globals__['limits_for']('version_recovery')==s['LIMITS']
print('PASS_SELECTED_PAYLOAD')
'''
        for name in ('primary','fallback'):
            result=subprocess.run([sys.executable,'-I','-B','-c',script,str(Path(stage.__file__).resolve()),
                json.dumps(plan),name],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout,'PASS_SELECTED_PAYLOAD\n')
        with self.assertRaises(ValueError):stage.payload_plan(plan,'unregistered')

    def test_owned_cpu_fault_handler_produces_actual_fatal_wait_without_core_pipeline(self):
        script='''import runpy,signal,sys
s=runpy.run_path(sys.argv[1])
signal.signal(signal.SIGUSR1,lambda *_:s['inject_fatal_signal']())
print('READY',flush=True)
signal.pause()
'''
        child=subprocess.Popen([sys.executable,'-I','-B','-c',script,
            str(Path(stage.__file__).resolve())],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            self.assertTrue(select.select([child.stdout],[],[],5)[0])
            self.assertEqual(child.stdout.readline(),b'READY\n')
            fd=os.pidfd_open(child.pid)
            try:signal.pidfd_send_signal(fd,signal.SIGUSR1)
            finally:os.close(fd)
            out,err=child.communicate(timeout=5)
            self.assertEqual(child.returncode,-signal.SIGABRT,err)
            self.assertEqual(out,b'')
        finally:
            if child.poll() is None:child.kill();child.wait(5)
            child.stdout.close();child.stderr.close()

    def test_probe_budget_exhaustion_precedes_any_module_load_or_device_exec(self):
        stage.LEGACY['Budget'](self.output,limits=stage.LIMITS).reserve('probes','already-used')
        with mock.patch.dict(stage.LEGACY,load_modules=mock.Mock(side_effect=AssertionError('device path'))):
            with self.assertRaisesRegex(RuntimeError,'budget exhausted'):
                stage.probe(dict(output=str(self.path)))
            stage.LEGACY['load_modules'].assert_not_called()

    def test_empty_environment_probe_verifies_real_inherited_lock_before_cpu_exec(self):
        package=self.package('root');(package/'bin').mkdir()
        marker=self.path/'cpu-exec-marker'
        binary=package/'bin/q4t-cuda-probe'
        binary.write_text('#!'+sys.executable+'\nfrom pathlib import Path\nPath('+repr(str(marker))+').write_text("CPU_ONLY")\n')
        binary.chmod(0o500)
        lockpath=self.path/'gpu-test.lock'
        with lockpath.open('w+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            bundle.engine.save(package/'manifest.json',dict(lock_path=str(lockpath),
                files={'bin/q4t-cuda-probe':bundle.engine.sha(binary)}))
            plan=self.path/'plan.json'
            bundle.engine.save(plan,dict(output=str(self.path),package=dict(package=str(package))))
            result=subprocess.run([sys.executable,'-I','-B',str(Path(stage.__file__).resolve()),
                'probe','--plan',str(plan)],pass_fds=(lock.fileno(),),env={},
                capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(marker.read_text(),'CPU_ONLY')
            self.assertEqual(json.loads((self.output/'counts.json').read_bytes())['counts']['probes'],1)
            with lockpath.open('r+') as other,self.assertRaises(BlockingIOError):
                fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)


if __name__=='__main__':unittest.main()
