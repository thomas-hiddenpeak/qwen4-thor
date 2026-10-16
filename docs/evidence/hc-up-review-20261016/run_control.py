import json,subprocess,hashlib
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor');r=Path(__file__).resolve().parent
base=root/'.q4t-work/mtp-numerical-closure-20261016/http/off'
binaries={'A':root/'.q4t-work/mtp-decode-20261016/q4t-main','B':root/'.q4t-work/mtp-decode-20261016/hc-up/q4t'}
expected={'A':'be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072','B':'f01a0ee63f626c352f66677952368d43db02c9231576a2b8ba1ca33a915ce573'}
for side,p in binaries.items():assert hashlib.sha256(p.read_bytes()).hexdigest()==expected[side]
results=[]
for i,side in enumerate('ABBAAB'):
    name=f'{i+1:02}-{side}';out=r/name
    cmd=[str(root/'tools/evalscope/.venv/bin/python3'),'tools/evalscope/run_acceptance.py','--binary',str(binaries[side]),'--mode','performance','--perf-lengths','204800','--perf-repeats','1','--fixtures',str(base),'--reference',str(base/'results.json'),'--output',str(out),'--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--startup-timeout','300','--allow-unqualified-binary','--port','8730']
    with (r/(name+'-driver.log')).open('w') as log: rc=subprocess.run(cmd,cwd=root,stdout=log,stderr=subprocess.STDOUT).returncode
    (r/(name+'-exit.json')).write_text(json.dumps({'returncode':rc,'command':cmd},indent=2)+'\n')
    if rc:raise SystemExit(rc)
    row=json.loads((out/'results.json').read_text())[0]
    results.append({'name':name,'side':side,'metrics':row['metrics'][0],'output':row['outputs'][0],'prompt':row['prompt_sha256']})
    (r/'control-results.json').write_text(json.dumps(results,indent=2)+'\n')
a=[x['metrics']['ttft'] for x in results if x['side']=='A'];b=[x['metrics']['ttft'] for x in results if x['side']=='B'];pairs=[]
for i in range(0,6,2):
    pair={x['side']:x['metrics']['ttft'] for x in results[i:i+2]};pairs.append(pair['B']-pair['A'])
review={'A_ttft':a,'B_ttft':b,'paired_B_minus_A':pairs,'consistent_positive':all(x>0 for x in pairs),'B_range_entirely_slower':min(b)>max(a),'original_regression_reproduced_or_unresolved':all(x>0 for x in pairs) or min(b)>max(a),'scope':'six fixed selected-tier requests, not a replacement full baseline'}
(r/'control-review.json').write_text(json.dumps(review,indent=2)+'\n')
print(json.dumps(review,indent=2),flush=True)
