import json,os,sys,unittest
from pathlib import Path
root=Path.cwd();sys.path.insert(0,str(root));os.environ['Q4T_TEST_OUTPUT_DIR']=sys.argv[1]
from tools.serve_recovery import test_native_ingress,test_native_access
binary=Path(os.environ['Q4T_HTTP_INGRESS']);test_native_ingress.INGRESS=binary;test_native_access.INGRESS=binary
names=json.loads((root/'.q4t-work/serve-http-20261109/native-session-required.json').read_text())
suite=unittest.defaultTestLoader.loadTestsFromNames(names)
assert suite.countTestCases()==25
result=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() and not result.skipped and result.testsRun==25 else 1)
