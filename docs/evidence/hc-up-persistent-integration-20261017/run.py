import os,json,subprocess
from pathlib import Path
root=Path(__file__).resolve().parent
repo=root.parents[1]
manifest=repo/'docs/evidence/hc-up-persistent-control-20261017/sizes.txt'
probe=repo/'.q4t-work/hc-up-persistent-control-20261017/probe.so'
for step in (0,1):
 env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
 env.update(LD_PRELOAD=str(probe),Q4T_DIAG_PERSISTENT_MODE='shared',
            Q4T_DIAG_PERSISTENT_SHIFT=str(512*step),
            Q4T_DIAG_PERSISTENT_MANIFEST=str(manifest))
 with (root/f'child-{step}.log').open('w') as log:
  p=subprocess.run([str(root/'check'),str(manifest),str(step)],env=env,
                   pass_fds=(73,74,75),stdout=log,stderr=subprocess.STDOUT)
 text=(root/f'child-{step}.log').read_text()
 assert p.returncode==0,text
 assert 'startup_complete=1541' in text and 'cleanup_complete=3' in text
 rows=[json.loads(line) for line in text.splitlines() if line.startswith('{')]
 assert len(rows)==3 and all(r['step']==step for r in rows)
 assert [r['verified_bytes'] for r in rows]==[167772160,167772160,2348023808]
 assert all(r['cross_process_verified']==bool(step) for r in rows)
 print(json.dumps({'child':step,'full_bytes_verified':sum(r['verified_bytes'] for r in rows),'cleanup':True}),flush=True)
