"""Execute the frozen text-v1 checklist once for an already-built candidate.

Fail closed, retaining each failed step. No automatic rerun or ablation loop.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from prepare_fixtures import prepare

ROOT=Path(__file__).resolve().parents[2]
MODEL='/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--build',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--nginx',type=Path,required=True)
    ap.add_argument('--previous-binary',type=Path,required=True)
    ap.add_argument('--previous-cache',type=Path,required=True)
    args=ap.parse_args();build=args.build.resolve();out=args.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    fixtures=prepare(out/'fixtures')
    binary=build/'q4t';quality=fixtures/'quality';perf=fixtures/'performance'
    def cmd(script,*parts):return [sys.executable,str(ROOT/script),*map(str,parts)]
    steps=[
      ('host',cmd('tools/release/check_host_contracts.py','--test-binary',build/'q4t_host_tests','--output',out/'host')),
      ('input',cmd('tools/evalscope/run_input_boundary.py','--binary',binary,'--quality-run',quality,'--contract','--output',out/'input')),
      ('quality',cmd('tools/evalscope/run_acceptance.py','--binary',binary,'--mode','quality','--model-dir',MODEL,'--fixtures',quality/'inputs','--reference',quality/'results.json','--output',out/'quality')),
      ('templates',cmd('tools/evalscope/run_chat_template.py','--binary',binary,'--model-dir',MODEL,'--output',out/'templates')),
      ('performance',cmd('tools/evalscope/run_acceptance.py','--binary',binary,'--mode','performance','--model-dir',MODEL,'--fixtures',perf,'--reference',perf/'results.json','--output',out/'performance')),
      ('cancellation',cmd('tools/evalscope/run_request_cancellation.py','--binary',binary,'--quality-run',quality,'--performance-run',perf,'--output',out/'cancellation')),
      ('decode-fault',cmd('tools/evalscope/run_decode_failure.py','--binary',binary,'--fixture-plan',fixtures/'fault-plan.json','--output',out/'decode-fault')),
      ('package-previous',cmd('tools/release/release.py','package','--binary',args.previous_binary.resolve(),'--cache',args.previous_cache.resolve(),'--compatibility-snapshot','--output',out/'previous')),
      ('package',cmd('tools/release/release.py','package','--binary',binary,'--cache',build/'CMakeCache.txt','--output',out/'package')),
      ('service-trial',cmd('tools/release/qualify_service.py','--package',out/'package','--previous',out/'previous','--nginx',args.nginx.resolve(),'--quality-run',quality,'--performance-run',perf,'--output',out/'service-trial'))]
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    plan={'binary_sha256':sha(binary),'checklist_sha256':sha(ROOT/'docs/CONVERGENCE_2026-09-28.md'),'steps':steps,'started':time.time()}
    (out/'plan.json').write_text(json.dumps(plan,indent=2))
    shutil.copy2(__file__,out/'qualify.py')
    results=[]
    for name,command in steps:
        print(name+': starting',flush=True)
        start=time.monotonic()
        with (out/(name+'.log')).open('w') as log:
            code=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT).returncode
        results.append({'step':name,'exit':code,'seconds':time.monotonic()-start})
        (out/'results.json').write_text(json.dumps(results,indent=2))
        print(name+': '+('PASS' if code==0 else 'FAIL'),flush=True)
        if code:sys.exit(code)
    (out/'completed.json').write_text(json.dumps({'functional_pass':True,'performance_claim':'measured necessary repair cost; no strict non-regression claim','binary_sha256':sha(binary)},indent=2))


if __name__=='__main__':main()
