import hashlib, json, os, subprocess, sys, time
from pathlib import Path
root=Path.cwd()
work=root/'.q4t-work/serve-native-access-20261108'
name=sys.argv[1]
plan=json.loads((work/'cpu-plan-06.json').read_text())
for p,digest in plan['source_files'].items():
    assert hashlib.sha256((root/p).read_bytes()).hexdigest()==digest,p
binary=work/'release-build/q4t-ingress'
off=work/'release-build/q4t-ingress-no-tls'
common=[sys.executable,'-B']
commands={
'driver':common+['-m','unittest','-v','tools.serve_recovery.test_acceptance.BudgetTests.test_native_access_bounds_include_self_check_and_forbid_cuda_probes','tools.serve_recovery.test_acceptance.PackagedImportTests'],
'configuration':common+['-m','unittest','-v','tools.serve_recovery.test_native_bundle','tools.serve_recovery.test_gateway.BindingTests'],
'host': [str(work/'host-on/q4t_host_tests'),'--required-list',str(work/'http-required.txt')],
'access':common+['tools/serve_recovery/test_native_access.py','--ingress',str(binary),'--no-tls-ingress',str(off),'-v'],
'native':common+['tools/serve_recovery/test_native_ingress.py','--ingress',str(binary),'-v'],
'admission':common+['tools/serve_recovery/test_native_admission.py','--ingress',str(binary),'-v'],
'browser':common+['tools/serve_recovery/test_native_browser.py','--ingress',str(binary),'--browser','/home/rm01/.cache/ms-playwright/chromium_headless_shell-1223/chrome-linux/headless_shell','-v']}
output=work/'cpu-06'/name
output.mkdir(parents=True,exist_ok=False)
command=commands[name]
record={'command':command,'source_plan_sha256':hashlib.sha256((work/'cpu-plan-06.json').read_bytes()).hexdigest(),'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'no_tls_binary_sha256':hashlib.sha256(off.read_bytes()).hexdigest(),'gpu_starts':0,'model_requests':0,'started_unix':time.time()}
(output/'started.json').write_text(json.dumps(record,indent=2)+'\n')
start=time.monotonic()
with (output/'stdout.log').open('wb') as stdout,(output/'stderr.log').open('wb') as stderr:
    result=subprocess.run(command,cwd=root,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',Q4T_TEST_OUTPUT_DIR=str(output/'artifacts')),stdout=stdout,stderr=stderr,timeout=180)
record.update(exit_code=result.returncode,elapsed_seconds=time.monotonic()-start)
(output/'result.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps({'suite':name,'exit_code':result.returncode,'seconds':record['elapsed_seconds'],'output':str(output)}))
sys.exit(result.returncode)
