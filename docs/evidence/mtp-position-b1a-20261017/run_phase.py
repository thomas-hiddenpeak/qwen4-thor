import subprocess,json,os
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
out=Path(__file__).resolve().parent
prior=root/'.q4t-work/mtp-numerical-closure-20261016/http'
common=[str(root/'tools/evalscope/.venv/bin/python3'),'tools/evalscope/run_acceptance.py','--binary',str(out/'q4t-candidate'),'--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--startup-timeout','300','--allow-unqualified-binary']
env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
for name,mode,mtp in [('quality-on','quality',True),('on','performance',True),('quality-off','quality',False),('off','performance',False)]:
    cmd=common+['--mode',mode,'--output',str(out/name),'--reference',str(prior/name/'results.json'),'--port','8740']
    if mtp:cmd+=['--mtp-default']
    if mode=='performance':cmd+=['--fixtures',str(prior/name)]
    with (out/(name+'-driver.log')).open('w') as log:
        result=subprocess.run(cmd,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
    (out/(name+'-exit.json')).write_text(json.dumps({'returncode':result.returncode,'command':cmd},indent=2)+'\n')
    if result.returncode:raise SystemExit(result.returncode)
# Preserve the complete phase lease while the frozen direct-test driver is
# prepared; a ready file names the next command. No GPU process runs meanwhile.
(out/'http-complete').write_text('passed\n')
import time
while not (out/'continue.json').exists():time.sleep(1)
cmd=json.loads((out/'continue.json').read_text())
raise SystemExit(subprocess.call(cmd,cwd=root,env=env))
