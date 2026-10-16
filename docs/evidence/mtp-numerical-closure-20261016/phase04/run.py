import subprocess,json
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
out=root/'.q4t-work/mtp-numerical-closure-20261016/http'
prior=root/'.q4t-work/mtp-merge-e2e-20261015'
common=[str(root/'tools/evalscope/.venv/bin/python3'),'tools/evalscope/run_acceptance.py','--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--startup-timeout','300','--allow-unqualified-binary']
jobs=[('off',['--mode','performance','--fixtures',str(prior/'off'),'--port','8730']),('on',['--mode','performance','--fixtures',str(prior/'on'),'--mtp-default','--port','8731']),('quality-off',['--mode','quality','--port','8730']),('quality-on',['--mode','quality','--mtp-default','--port','8731'])]
for name,args in jobs:
    cmd=common+['--output',str(out/name)]+args
    with (out/(name+'-driver.log')).open('w') as log:
        result=subprocess.run(cmd,cwd=root,stdout=log,stderr=subprocess.STDOUT)
    (out/(name+'-exit.json')).write_text(json.dumps({'returncode':result.returncode,'command':cmd},indent=2)+'\n')
    if result.returncode and not name.startswith('quality'): raise SystemExit(result.returncode)
