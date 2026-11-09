import hashlib,json,os,sys,time,unittest
from pathlib import Path
root=Path.cwd();sys.path.insert(0,str(root));sys.dont_write_bytecode=True
out=root/'.q4t-work/serve-lifecycle-20261109/cli-capacity-fault-01'
plan=json.loads((out/'plan.json').read_text())
files=['tools/serve_recovery/controller.py','tools/serve_recovery/test_production.py','tools/serve_gateway/tls.py','src/service/service_cli.cpp','include/q4t/service/service_cli.h','cmake/ServiceCLI.cmake','src/main.cpp','CMakeLists.txt','tests/host/CMakeLists.txt','tests/service_cli_fixture.cpp','tools/serve_recovery/build_tools.py','tools/serve_recovery/lifecycle.py','tools/serve_recovery/bundle.py','tools/serve_recovery/service.py','tools/serve_recovery/deployment.py','tools/serve_service/package.py','tools/serve_recovery/test_lifecycle.py','tools/serve_recovery/test_native_bundle.py']
(out/'sources.json').write_text(json.dumps({n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in files},indent=2)+'\n')
started=time.monotonic()
with (out/'unittest.log').open('x') as log:
 result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(plan['tests']))
value=dict(status='PASS' if result.wasSuccessful() else 'FAIL',tests_run=result.testsRun,failures=[dict(test=str(t),traceback=s) for t,s in result.failures],errors=[dict(test=str(t),traceback=s) for t,s in result.errors],elapsed_seconds=time.monotonic()-started,gpu_starts=0)
(out/'result.json').write_text(json.dumps(value,indent=2)+'\n');print(json.dumps(value));sys.exit(0 if result.wasSuccessful() else 1)
