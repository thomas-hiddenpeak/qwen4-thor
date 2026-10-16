import json,os,subprocess,sys,time
from pathlib import Path
root=Path(__file__).resolve().parent
sys.path.insert(0,str(Path.cwd()/'tools/evalscope'))
import gpu_exclusive
if sys.argv[-1]=='child':
 data={'pid':os.getpid(),'ppid':os.getppid(),'pgid':os.getpgid(0),'sid':os.getsid(0),'tag':os.environ.get('Q4T_OWNERSHIP_DIAGNOSTIC')}
 (root/'child.json').write_text(json.dumps(data,indent=2)+'\n')
 time.sleep(3)
else:
 session='q4t-cpu-ownership-'+str(os.getpid())
 env=dict(os.environ,Q4T_OWNERSHIP_DIAGNOSTIC=session)
 cmd=['nsys','launch','--session-new='+session,'--trace=none','--discard-environment=true',sys.executable,str(Path(__file__).resolve()),'child']
 p=None
 try:
  with (root/'launch.log').open('w') as log:
   p=subprocess.Popen(cmd,env=env,stdout=log,stderr=subprocess.STDOUT)
   deadline=time.monotonic()+10
   while not (root/'child.json').exists():
    if time.monotonic()>deadline:raise TimeoutError('child never started')
    time.sleep(.05)
   child=json.loads((root/'child.json').read_text());records=gpu_exclusive.processes()
   chain=[];pid=child['pid']
   while pid in records and pid not in [x['pid'] for x in chain]:
    rec=records[pid];chain.append(dict(pid=pid,**rec));pid=rec.get('ppid')
   (root/'ancestry.json').write_text(json.dumps({'driver':{'pid':os.getpid(),'pgid':os.getpgid(0),'sid':os.getsid(0)},'session':session,'child':child,'chain':chain},indent=2)+'\n')
   from unittest.mock import patch
   history={}
   with patch.object(gpu_exclusive,'nvidia_pids',return_value={child['pid']}):
    own=gpu_exclusive.inspect(os.getpgid(0),history)
   assert not own['conflicts'], own
   with patch.object(gpu_exclusive,'nvidia_pids',return_value={child['pid'],1}):
    foreign=gpu_exclusive.inspect(os.getpgid(0),history)
   assert {r['pid'] for r in foreign['conflicts']}=={1},foreign
   (root/'ownership-check.json').write_text(json.dumps({'owned':own,'foreign_control':foreign,'target_identity':history[child['pid']],'hardware_counter_used':False},indent=2)+'\n')
 finally:
  with (root/'shutdown.log').open('w') as log:subprocess.run(['nsys','shutdown','--session='+session,'--kill=sigterm'],stdout=log,stderr=subprocess.STDOUT,check=False)
  if p is not None:p.wait(timeout=10)
