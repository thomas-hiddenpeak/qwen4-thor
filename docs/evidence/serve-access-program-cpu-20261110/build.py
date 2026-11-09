import json,subprocess,time,sys
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
w=root/'.q4t-work/serve-access-programs-20261110'
name=sys.argv[1];target=sys.argv[2];targets=sys.argv[3:]
argv=['cmake','--build',target,'--target',*targets,'--parallel','4']
t=time.monotonic()
with (w/(name+'.stdout')).open('x') as out,(w/(name+'.stderr')).open('x') as err:
 r=subprocess.run(argv,cwd=root,stdout=out,stderr=err)
v=dict(argv=argv,exit_code=r.returncode,seconds=time.monotonic()-t)
(w/(name+'.json')).write_text(json.dumps(v,indent=2)+'\n')
print(json.dumps(v),flush=True)
raise SystemExit(r.returncode)
