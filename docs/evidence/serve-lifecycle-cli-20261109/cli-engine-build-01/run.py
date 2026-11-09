import json,subprocess,time,sys
from pathlib import Path
out=Path('.q4t-work/serve-lifecycle-20261109/cli-engine-build-01')
command=['cmake','--build','.q4t-work/serve-custom-tools-20261108/engine-build','--target','q4t','q4t_ingress','q4t_guardian','q4t_cuda_probe','--parallel','4']
(out/'command.json').write_text(json.dumps(command,indent=2)+'\n');started=time.monotonic()
with (out/'stdout.txt').open('x') as stdout,(out/'stderr.txt').open('x') as stderr:result=subprocess.run(command,stdout=stdout,stderr=stderr)
value=dict(returncode=result.returncode,elapsed_seconds=time.monotonic()-started)
(out/'result.json').write_text(json.dumps(value,indent=2)+'\n');print(json.dumps(value));sys.exit(result.returncode)
