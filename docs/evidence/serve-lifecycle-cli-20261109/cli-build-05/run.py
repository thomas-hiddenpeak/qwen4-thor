import json,subprocess,sys,time
from pathlib import Path
out=Path('.q4t-work/serve-lifecycle-20261109/cli-build-05')
commands=[['cmake','-S','tests/host','-B','.q4t-work/serve-access-20261109/host','-DCMAKE_CXX_COMPILER=g++-14','-DCMAKE_BUILD_TYPE=Release','-DQ4T_ENABLE_XGRAMMAR=OFF','-DQ4T_ENABLE_NATIVE_TLS=OFF'],['cmake','--build','.q4t-work/serve-access-20261109/host','--target','q4t_service_cli_fixture','--parallel','4']]
(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n');records=[]
for i,command in enumerate(commands):
 start=time.monotonic()
 with (out/(str(i)+'.stdout.txt')).open('x') as stdout,(out/(str(i)+'.stderr.txt')).open('x') as stderr:r=subprocess.run(command,stdout=stdout,stderr=stderr)
 records.append(dict(command=command,returncode=r.returncode,elapsed_seconds=time.monotonic()-start))
 if r.returncode:break
(out/'result.json').write_text(json.dumps(records,indent=2)+'\n');print(json.dumps(records));sys.exit(records[-1]['returncode'])
