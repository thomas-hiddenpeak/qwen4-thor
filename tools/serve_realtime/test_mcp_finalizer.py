"""Real sealed namespace bootstrap in a fresh process; no model execution."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class FinalizerTests(unittest.TestCase):
    def test_cli_wrapper_pins_sealed_reader_before_any_production_import(self):
        with tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_OUTPUT_DIR']) as tmp:
            root = Path(tmp)
            source = root/'package/source'
            for name in ('serve_service', 'serve_recovery'):
                (source/'tools'/name).mkdir(parents=True)
            for name in ('bundle', 'child', 'control', 'journal', 'service'):
                (source/'tools/serve_recovery'/(name+'.py')).write_text('')
            (source/'tools/serve_service/package.py').write_text(
                'import json\nfrom pathlib import Path\n'
                'def read(path): return json.loads(Path(path).read_bytes())\n')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0));port=sock.getsockname()[1]
            (root/'execution').mkdir()
            (root/'execution/mcp-peer-cleanup.json').write_text(json.dumps(
                dict(closed=True, port=port)))
            plan=root/'plan.json'
            plan.write_text(json.dumps(dict(package=dict(package=str(root/'package')),
                output=str(root), mcp_recovery=dict(fixture=dict(port=port)))))
            script='''
import runpy,sys
from pathlib import Path
root,plan,preload=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]=='1'
sys.path.insert(0,str(root))
driver=runpy.run_path(str(root/'tools/serve_realtime/mcp_service_acceptance.py'))
if preload:
    from tools.serve_service import package
driver['BASE']['finalize']=lambda path:dict(delegated=str(path))
try:
    result=driver['finalize'](plan)
except RuntimeError as error:
    assert preload and 'already loaded outside sealed source' in str(error),str(error)
else:
    assert not preload
    assert result==dict(delegated=str(plan))
    from tools.serve_service import package
    assert Path(package.__file__).is_relative_to(plan.parent/'package/source')
'''
            for preload in (False, True):
                with self.subTest(preloaded_checkout=preload):
                    result=subprocess.run([sys.executable,'-I','-B','-c',script,
                        str(ROOT),str(plan),str(int(preload))],capture_output=True,
                        timeout=10,check=False)
                    self.assertEqual(result.returncode,0,result.stderr.decode())


if __name__ == '__main__': unittest.main()
