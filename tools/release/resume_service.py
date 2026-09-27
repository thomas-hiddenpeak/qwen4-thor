"""Explicit service-only repair after a failed final qualification step.

Require unchanged runtime/config; never turn the original failed record green.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--qualification',type=Path,required=True)
    ap.add_argument('--nginx',type=Path,required=True)
    args=ap.parse_args();q=args.qualification.resolve()
    original=json.loads((q/'results.json').read_text())
    assert len(original)==10 and all(x['exit']==0 for x in original[:-1])
    assert original[-1]['step']=='service-trial' and original[-1]['exit']!=0
    old=q/'package';new=q/'package-v2';out=q/'service-trial-v2'
    command=[sys.executable,str(ROOT/'tools/release/release.py'),'package','--binary',str(old/'q4t'),'--cache',str(old/'CMakeCache.txt'),'--output',str(new)]
    with (q/'package-v2.log').open('w') as log:
        subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True)
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    identity={name:sha(old/name) for name in ['q4t','release.py','data.conf.in','control.conf.in','CMakeCache.txt']}
    assert all(sha(new/name)==h for name,h in identity.items()),'runtime/config changed; a service-only rerun is insufficient'
    fixtures=q/'fixtures'
    quality=fixtures/'quality' if fixtures.exists() else ROOT/'.q4t-work/e2e/listen-quality-20260927'
    performance=fixtures/'performance' if fixtures.exists() else ROOT/'.q4t-work/e2e/listen-performance-20260927'
    cmd=[sys.executable,str(ROOT/'tools/release/qualify_service.py'),'--package',str(new),'--previous',str(q/'previous'),'--nginx',str(args.nginx.resolve()),'--quality-run',str(quality),'--performance-run',str(performance),'--output',str(out)]
    recovery={'original_failure':str(q/'service-trial.log'),'scope':'explicit service-harness repair; original failure retained; unchanged runtime and serving configuration',
              'runtime_files':identity,'package':'package-v2','service_trial':'service-trial-v2','command':cmd,'exit':None}
    (q/'recovery.json').write_text(json.dumps(recovery,indent=2))
    with (q/'service-trial-v2.log').open('w') as log:
        code=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT).returncode
    recovery['exit']=code
    (q/'recovery.json').write_text(json.dumps(recovery,indent=2))
    if code:sys.exit(code)
    (q/'completed-recovery.json').write_text(json.dumps({'functional_pass':True,'binary_sha256':identity['q4t'],'original_failure_retained':True},indent=2))
    print('Service-only repair: PASS',flush=True)


if __name__=='__main__':main()
