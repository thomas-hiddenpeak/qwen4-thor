import hashlib,json,os,subprocess,time
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
out=root/'.q4t-work/native-embedding-20261111/diagnostic-01'
plan=json.loads((out/'plan.json').read_text())
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
for path,expected in plan['sources'].items():assert sha(root/path)==expected
assert sha(Path(plan['binary']))==plan['binary_sha256']
lease=int(os.environ['Q4T_MANAGED_LEASE_FD']);os.fstat(lease)
commands=[('reference',[str(root/'.q4t-work/refenv/bin/python'),str(root/'tools/native_embedding/reference.py'),'--assets',str(out.parent/'assets'),'--plan',str(out/'plan.json'),'--output',str(out/'reference')]),('native',[plan['binary'],str(out.parent/'assets'),str(out/'plan.json'),str(out/'native')])]
record={'status':'FAILED','commands':[]};started=time.monotonic()
try:
 for name,cmd in commands:
  with (out/(name+'.stdout')).open('x') as o,(out/(name+'.stderr')).open('x') as e:
   before=time.monotonic();r=subprocess.run(cmd,cwd=root,pass_fds=(lease,),stdout=o,stderr=e,timeout=180-(time.monotonic()-started),env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1'))
  record['commands'].append(dict(name=name,argv=cmd,returncode=r.returncode,seconds=time.monotonic()-before))
  assert r.returncode==0,name
 record['status']='CAPTURED_NOT_ACCEPTANCE'
finally:
 record['seconds']=time.monotonic()-started
 (out/'result.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record),flush=True)
