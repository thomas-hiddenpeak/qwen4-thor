import hashlib,json,os,subprocess,sys,time
from pathlib import Path
root=Path.cwd();base=root/'.q4t-work/serve-http-20261109'
mode=sys.argv[1];out=base/(mode+'-cpu-01');out.mkdir()
files=subprocess.check_output(['git','ls-files','include','src','cmake'],text=True).splitlines()
files += ['CMakeLists.txt','tests/server_http_session_test.cpp','tests/server_http_framing_test.cpp','tests/server_http_response_test.cpp','tests/server_http_observation_test.cpp','tools/serve_http/test_keepalive.py','tools/serve_recovery/test_native_ingress.py','tools/serve_recovery/test_native_access.py','tools/serve_recovery/test_gateway_nginx.py']
(out/'sources.json').write_text(json.dumps({name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sorted(set(files))},indent=2)+'\n')
if mode=='session':
 binary=root/'.q4t-work/serve-access-20261109/host/q4t_host_tests'
 command=[str(binary),'--required-list',str(base/'session-required.txt')];count=29
else:
 binary=root/'.q4t-work/serve-custom-tools-20261108/engine-build/q4t-ingress'
 command=[sys.executable,'-B',str(base/'run_native_child.py'),str(out/'work')];count=25
(out/'command.json').write_text(json.dumps(command,indent=2)+'\n')
start=time.monotonic();code=125
try:
 with (out/'tests.log').open('wb') as log:
  code=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=180,
   env=dict(os.environ,Q4T_HTTP_INGRESS=str(binary),PYTHONDONTWRITEBYTECODE='1')).returncode
finally:
 result=dict(exit_code=code,seconds=time.monotonic()-start,count=count,gpu_operations=0,binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
 (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result));print((out/'tests.log').read_text()[-6500:])
