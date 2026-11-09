import json,os,sys,unittest
from pathlib import Path
root=Path.cwd();sys.path.insert(0,str(root));os.environ['Q4T_TEST_OUTPUT_DIR']=sys.argv[1]
from tools.serve_recovery import test_native_ingress,test_native_access
binary=Path(os.environ['Q4T_HTTP_INGRESS']);test_native_ingress.INGRESS=binary;test_native_access.INGRESS=binary
plan=json.loads((root/'.q4t-work/serve-http-20261109/proxy-plan.json').read_bytes())
suite=unittest.defaultTestLoader.loadTestsFromNames(plan['python']);assert suite.countTestCases()==19
r=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if r.wasSuccessful() and not r.skipped and r.testsRun==19 else 1)
