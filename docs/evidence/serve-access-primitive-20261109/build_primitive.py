import hashlib,json,subprocess,time
from pathlib import Path
base=Path('.q4t-work/serve-access-20261109');out=base/'build-02';out.mkdir()
commands=[['cmake','-S','tests/host','-B',str(base/'host'),'-DCMAKE_CXX_COMPILER=g++-14','-DCMAKE_BUILD_TYPE=Release','-DQ4T_ENABLE_XGRAMMAR=OFF','-DQ4T_ENABLE_NATIVE_TLS=OFF'],['cmake','--build',str(base/'host'),'--target','q4t_ingress_access_tests','--parallel','4']]
(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n');start=time.monotonic();rc=0
with (out/'stdout.txt').open('wb') as stdout,(out/'stderr.txt').open('wb') as stderr:
 for cmd in commands:
  p=subprocess.run(cmd,stdout=stdout,stderr=stderr);rc=p.returncode
  if rc:break
(out/'result.json').write_text(json.dumps(dict(exit_code=rc,seconds=time.monotonic()-start),indent=2)+'\n')
files=['include/q4t/service/ingress_access.h','src/service/ingress_access.cpp','tests/ingress_access_test.cpp','cmake/NativeIngress.cmake','src/trace/sha256.cpp']
(out/'sources.json').write_text(json.dumps({n:hashlib.sha256(Path(n).read_bytes()).hexdigest() for n in files},indent=2)+'\n')
print((out/'result.json').read_text());print((out/'stderr.txt').read_text()[-3000:])
