import json,subprocess,time
from pathlib import Path
base=Path('.q4t-work/serve-http-20261109');out=base/'final-build-01';out.mkdir()
cmd=['cmake','--build','.q4t-work/serve-custom-tools-20261108/engine-build','--target','q4t','q4t_ingress','q4t_guardian','q4t_cuda_probe','--parallel','4']
(out/'command.json').write_text(json.dumps(cmd,indent=2)+'\n');start=time.monotonic();code=125
try:
 with (out/'stdout.txt').open('wb') as stdout,(out/'stderr.txt').open('wb') as stderr:code=subprocess.run(cmd,stdout=stdout,stderr=stderr,timeout=300).returncode
finally:
 stdout=(out/'stdout.txt').read_text();stderr=(out/'stderr.txt').read_text();result=dict(exit_code=code,seconds=time.monotonic()-start,warnings=(stdout+stderr).lower().count('warning:'))
 (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(result);print(stdout[-1800:]);print(stderr[-1800:])
