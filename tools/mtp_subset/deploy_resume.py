"""Deploy the accepted, sealed subset candidate; invoke under its GPU lease."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import time

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base',type=Path,required=True)
parser.add_argument('--rollback',action='store_true')
parser.add_argument('--rollback-output',type=Path)
args=parser.parse_args()
b=args.base.resolve();r=b/'source';root=b.parents[1];p=b/'delivery-package-v2';out=r/'.q4t-work/deployment-v2'
spec=importlib.util.spec_from_file_location('delivery_helpers',r/'tools/verify/startup_delivery/deploy.py')
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
if args.rollback:
 assert args.rollback_output is not None,'rollback requires a fresh evidence directory'
 rollback_out=args.rollback_output.resolve()
 assert rollback_out.is_relative_to(r/'.q4t-work') and rollback_out!=out
 h.require_guard(rollback_out)
else:
 assert args.rollback_output is None
 h.require_guard(out)
h.verify_seal(p)
old=json.loads((p/'previous/q4t.release.json').read_text())['binary_sha256']
new=json.loads((p/'candidate/q4t.release.json').read_text())['binary_sha256']
assert old=='5a456351ac7230607e69b6d03e24aace3efb4c7ba521bf0a348a9b92d6c21191'
assert new=='75e85bac509b793b9b69612539809a17b64d45febe1b408e019641fe01c2eb2d'
marker=root/'build/mtp-subset-recovery.json'
def restore():
 for name in ['q4t','q4t.release.json']:
  dest=root/'build'/name;oldsha=h.sha(p/'previous'/name);newsha=h.sha(p/'candidate'/name)
  assert h.sha(dest) in [oldsha,newsha],'unknown destination; preserve recovery marker'
  if h.sha(dest)!=oldsha:h.replace_checked(p/'previous'/name,dest,newsha)
 h.verify_pair(root/'build/q4t',root/'build/q4t.release.json',old)
 if marker.exists():marker.unlink();h.fsync_directory(marker.parent)
if args.rollback:
 restore()
 h.save(rollback_out/'rollback.json',dict(status='ROLLED_BACK',binary_sha256=old,package=str(p),finished=time.time()))
 print('ROLLED_BACK');raise SystemExit(0)
assert not marker.exists()
h.verify_pair(root/'build/q4t',root/'build/q4t.release.json',old)
for name in ['q4t','q4t.release.json']:assert h.sha(root/'build'/name)==h.sha(p/'previous'/name)
assert not (out/'deployment.json').exists()
h.save(marker,dict(package=str(p),output=str(out),previous=old,candidate=new,rollback_command=['python3',str(Path(__file__).resolve()),'--base',str(b),'--rollback']))
h.fsync_directory(marker.parent)
state=dict(status='INSTALLING',health=[],started=time.time())
def interrupted(signum,_frame):raise KeyboardInterrupt('signal '+str(signum))
signal.signal(signal.SIGTERM,interrupted)
try:
 for name in ['q4t','q4t.release.json']:
  h.replace_checked(p/'candidate'/name,root/'build'/name,h.sha(p/'previous'/name))
 h.verify_pair(root/'build/q4t',root/'build/q4t.release.json',new)
 plan=json.loads((p/'health-plan.json').read_text());deadline=time.monotonic()+plan['timeout_seconds']
 env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
 for step in plan['steps']:
  with (out/(step['name']+'-driver.log')).open('x') as log:
   subprocess.run(step['command'],cwd=r,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=max(1,deadline-time.monotonic()))
  d=out/step['name'];exit_state=json.loads((d/'exit.json').read_text());rows=json.loads((d/'results.json').read_text())
  assert exit_state['server']==0 and exit_state['http_output_checks_passed'] and len(rows)==1 and rows[0]['exact_match']
  log=(d/'server.log').read_text();final=[json.loads(l.split('] ',1)[1]) for l in log.splitlines() if l.startswith('[startup-reservation] ') and '"event":"finalized"' in l]
  assert len(final)==1 and final[0]['status']=='PASS' and final[0]['release_unknown']==0
  assert 'retained after failed drain' not in log and 'MTP init failed' not in log
  state['health'].append(dict(name=step['name'],exact_match=True,finalized=final[0]))
 h.verify_seal(p);h.verify_pair(root/'build/q4t',root/'build/q4t.release.json',new)
 state['status']='DEPLOYED_HEALTH_CHECKED'
 marker.unlink();h.fsync_directory(marker.parent)
except BaseException as error:
 state.update(status='FAILED',error=repr(error))
 try:restore();state['rollback']='RESTORED_5A'
 except BaseException as failure:state.update(status='RECOVERY_REQUIRED',rollback_error=repr(failure))
 raise
finally:
 state['finished']=time.time();h.save(out/'deployment.json',state)
