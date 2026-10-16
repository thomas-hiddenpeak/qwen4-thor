import hashlib,json,math,statistics
from pathlib import Path
r=Path(__file__).resolve().parent
base=r.parents[1]/'mtp-numerical-closure-20261016/http'
summary={'baseline_commit':'28a3783350b0c4c3d73fd3d483b5eebe881d2bdb','candidate_runtime_commit':'81c3335','binary_sha256':(r/'binary.sha256').read_text().strip(),'quality':{},'performance':{},'numeric':{},'reused':['model_prefill_batch: M3/5/7/15, new specialization not entered','PLE independent ownership contracts: implementation unchanged']}
for side in ['on','off']:
    p=r/side/'results.json'
    if not p.exists():continue
    old=json.loads((base/side/'results.json').read_text());new=json.loads(p.read_text())
    rows=[]
    for a,b in zip(new,old):
        at=[x['decode_tps'] for x in a['metrics']];bt=[x['decode_tps'] for x in b['metrics']]
        af=[x['ttft'] for x in a['metrics']];bf=[x['ttft'] for x in b['metrics']]
        rows.append({'length':a['length'],'baseline_decode_tps':statistics.harmonic_mean(bt),'candidate_decode_tps':statistics.harmonic_mean(at),'gain_percent':100*(statistics.harmonic_mean(at)/statistics.harmonic_mean(bt)-1),'baseline_ttft':statistics.mean(bf),'candidate_ttft':statistics.mean(af),'same_outputs':a['outputs']==b['outputs'],'same_prompt':a['prompt_sha256']==b['prompt_sha256'],'deterministic':a['deterministic'],'decode_clear_regression':max(at)<min(bt),'ttft_clear_regression':min(af)>max(bf),'baseline_metrics':b['metrics'],'candidate_metrics':a['metrics']})
    summary['performance'][side]=rows
    if len(rows)==5:
        summary[side+'_geomean_gain_percent']=100*(math.prod(1+x['gain_percent']/100 for x in rows)**0.2-1)
    p=r/('quality-'+side)/'results.json'
    if p.exists():
        d=json.loads(p.read_text());summary['quality'][side]={'count':len(d),'exact':sum(x['exact_match'] for x in d),'length':sum(x['length_match'] for x in d)}
for name in ['affected','numerics','diagnostic']:
    p=r/(name+'-exit.json')
    summary['numeric'][name]=json.loads(p.read_text()) if p.exists() else 'pending'
summary['accepted']=False
summary['acceptance_note']='Requires explicit review of all gates; this script does not infer acceptance.'
(r/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n')
print(json.dumps({k:v for k,v in summary.items() if k!='performance'},indent=2,ensure_ascii=False))
