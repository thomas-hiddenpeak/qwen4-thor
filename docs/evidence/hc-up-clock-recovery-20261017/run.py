import fcntl,json,os,subprocess
from pathlib import Path
out=Path(__file__).resolve().parent;results=[]
for mode in ('normal','kill'):
 d=out/mode;d.mkdir();(d/'fake-clock').write_text('original\n')
 unit=f'q4t-clock-recovery-probe-{os.getpid()}-{mode}.service'
 cmd=['sudo','-n','systemd-run','--unit='+unit,'--wait','--collect','--property=User='+str(os.getuid()),'--property=Group='+str(os.getgid()),'--property=Type=exec','--property=NotifyAccess=main','--property=FileDescriptorStoreMax=1','--property=FileDescriptorStorePreserve=restart','--property=RuntimeMaxSec=15','--property=TimeoutStopSec=10','--property=ExecStopPost=/usr/bin/python3 '+str(out/'probe.py')+' cleanup '+mode+' '+str(d)+' '+unit,'/usr/bin/python3',str(out/'probe.py'),'main',mode,str(d),unit]
 r=subprocess.run(cmd,capture_output=True,text=True,timeout=40)
 (d/'command-result.json').write_text(json.dumps({'command':cmd,'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr},indent=2)+'\n')
 c=json.loads((d/'cleanup.json').read_text());assert c['lease_still_held'] and c['after']=='original\n'
 assert (r.returncode==0)==(mode=='normal')
 with (d/'lease').open('a+') as f:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
 results.append({'mode':mode,'returncode':r.returncode,'cleanup':c,'lease_released_after_cleanup':True})
(out/'result.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results,indent=2))
