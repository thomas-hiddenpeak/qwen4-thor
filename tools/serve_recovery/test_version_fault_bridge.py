"""Actual CPU process wait/signal propagation; no CUDA or model execution."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]


class BridgeTests(unittest.TestCase):
    def test_native_fatal_wait_propagates_only_when_enabled_and_not_stopping(self):
        script='''
import os,resource,signal,subprocess,sys
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
sys.path.insert(0,sys.argv[1])
from tools.serve_recovery.native_ingress import propagate_native_exit
sig=int(sys.argv[2]);stopped=sys.argv[3]=='1'
p=subprocess.Popen([sys.executable,'-c','import os,signal;os.kill(os.getpid(),'+str(sig)+')'])
p.wait(timeout=3)
assert p.returncode==-sig
propagate_native_exit(p,stopped)
'''
        for sig,stopped,enabled,expected in [(6,False,True,-6),(11,False,True,-11),
                (6,True,True,0),(15,False,True,0),(6,False,False,0)]:
            with self.subTest(signal=sig,stopped=stopped,enabled=enabled):
                result=subprocess.run([sys.executable,'-I','-B','-c',script,str(ROOT),str(sig),
                    '1' if stopped else '0'],env=dict(os.environ,Q4T_VERSION_RECOVERY='1' if enabled else '0'),
                    stdin=subprocess.DEVNULL,capture_output=True,timeout=5)
                self.assertEqual(result.returncode,expected,result.stderr.decode())


if __name__=='__main__':unittest.main()
