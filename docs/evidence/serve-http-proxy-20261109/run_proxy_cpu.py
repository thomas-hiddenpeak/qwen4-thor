import hashlib,json,os,subprocess,sys,time
from pathlib import Path
root=Path.cwd();base=root/'.q4t-work/serve-http-20261109';mode=sys.argv[1]
out=base/(mode+'-cpu-01');out.mkdir()
plan=json.loads((base/'proxy-plan.json').read_bytes())
checker=root/'.q4t-work/serve-access-20261109/host/q4t_ingress_proxy_tests'
ingress=root/'.q4t-work/serve-custom-tools-20261108/engine-build/q4t-ingress'
if mode=='proxy':cmd=[str(checker),'--tests',*plan['cpp']];count=8;binary=checker
else:cmd=[sys.executable,'-B',str(base/'run_proxy_child.py'),str(out/'work')];count=19;binary=ingress
files=subprocess.check_output(['git','ls-files','include','src','cmake'],text=True).splitlines()
files+=['include/q4t/service/ingress_proxy.h','src/service/ingress_proxy.cpp','tests/ingress_proxy_test.cpp','tools/serve_http/test_proxy.py','tools/serve_http/test_keepalive.py','tools/serve_gateway/gateway.py','tools/serve_recovery/gateway_config.py','tools/serve_recovery/bundle.py','tools/serve_recovery/test_native_ingress.py','tools/serve_recovery/test_native_access.py','tools/serve_recovery/test_gateway_nginx.py']
(out/'sources.json').write_text(json.dumps({n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in sorted(set(files))},indent=2)+'\n')
(out/'command.json').write_text(json.dumps(cmd,indent=2)+'\n')
env=dict(os.environ,Q4T_HTTP_INGRESS=str(ingress),Q4T_HTTP_PROXY_CHECKER=str(checker),PYTHONDONTWRITEBYTECODE='1')
start=time.monotonic();code=125
try:
 with (out/'tests.log').open('wb') as log:code=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=150,env=env).returncode
finally:
 result=dict(exit_code=code,seconds=time.monotonic()-start,count=count,gpu_operations=0,binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
 (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result));print((out/'tests.log').read_text()[-5000:])
