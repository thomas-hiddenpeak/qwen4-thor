import json,os,subprocess,sys,time
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
w=root/'.q4t-work/serve-access-programs-20261110'
name=sys.argv[1];argv=sys.argv[2:];t=time.monotonic()
env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',Q4T_TEST_OUTPUT_DIR=str(w/'cpu-01'))
with (w/(name+'.stdout')).open('x') as out,(w/(name+'.stderr')).open('x') as err:
 r=subprocess.run(argv,cwd=root,env=env,stdout=out,stderr=err,timeout=1200)
v=dict(argv=argv,exit_code=r.returncode,seconds=time.monotonic()-t)
(w/(name+'.json')).write_text(json.dumps(v,indent=2)+'\n');print(json.dumps(v),flush=True)
raise SystemExit(r.returncode)
