"""Summarize validated distribution arrays; no cache or throughput metrics."""
import argparse
import csv
import json
from pathlib import Path
import numpy as np
from distribution import distribution, top_set, save


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--directory',type=Path,required=True)
    args=ap.parse_args();p=args.directory.resolve()
    root=Path(__file__).resolve().parents[2]
    if not any(p.is_relative_to(root/x) for x in ['build','.q4t-work']):
        ap.error('directory must be under build/ or .q4t-work/')
    out=p/'readout';out.mkdir(exist_ok=False)
    records=json.loads((p/'requests.json').read_text())
    pops=json.loads((p/'populations.json').read_text())
    arrays=np.load(p/'population-counts.npz')
    request=np.load(p/'request-counts.npz')['counts']
    stability=json.loads((p/'stability.json').read_text())
    with (p/'layers.csv').open() as f: layers=list(csv.DictReader(f))
    summary=[]
    for pop in [k for k in pops if not k.startswith('batch:')]:
        for phase in ['prefill','decode']:
            for weight in ['route','equal_request']:
                rows=[r for r in layers if (r['population'],r['phase'],r['weighting'])==(pop,phase,weight)]
                row=dict(population=pop,phase=phase,weighting=weight,requests=len(pops[pop]))
                for key in ['active','top10','top32','top64','top128','top256','entropy_bits','effective_experts']:
                    vals=[float(r[key]) for r in rows if r[key]!='']
                    row[key]=dict(mean=float(np.mean(vals)),min=min(vals),max=max(vals)) if vals else None
                summary.append(row)
    save(out/'summary.json',summary)
    # Independently reconstruct every stored population, including request weights.
    for pop,ids in pops.items():
        np.testing.assert_array_equal(arrays[pop+':counts'],sum((request[i] for i in ids)))
        expected=np.zeros((2,48,512));denom=np.zeros((2,48,1))
        for i in ids:
            for s in range(2):
                for layer in range(48):
                    n=int(request[i,s,layer].sum())
                    if n:
                        expected[s,layer]+=request[i,s,layer]/n;denom[s,layer]+=1
        np.testing.assert_allclose(arrays[pop+':equal_request'],np.divide(expected,denom,out=np.zeros_like(expected),where=denom>0))
    # Cross-request top sets; layer IDs never mix.
    ids=pops['authored:unique_input']
    sets={(i,s,l):top_set(request[i,s,l],64) for i in ids for s in range(2) for l in range(48)}
    pairs=[]
    for x,i in enumerate(ids):
        for j in ids[x+1:]:
            for s,phase in enumerate(['prefill','decode']):
                values=[len(sets[i,s,l]&sets[j,s,l])/64 for l in range(48)
                        if len(sets[i,s,l])==len(sets[j,s,l])==64]
                pairs.append(dict(a=i,b=j,phase=phase,same_scenario=records[i]['scenario']==records[j]['scenario'],
                    defined_layers=len(values),top64_overlap=float(np.mean(values)) if values else None))
    save(out/'request-pairs.json',pairs)
    def mean(values):
        values=[v for v in values if v is not None]
        return float(np.mean(values)) if values else None
    notable={}
    for s,phase in enumerate(['prefill','decode']):
        count=arrays['authored:unique_input:counts'][s]
        pres=arrays['authored:unique_input:request_presence'][s]
        notable[phase]=dict(
            mean_experts_in_all_requests=float(np.mean((pres==len(ids)).sum(axis=1))),
            mean_experts_in_80_percent_requests=float(np.mean((pres>=np.ceil(.8*len(ids))).sum(axis=1))),
            mean_experts_in_half_requests=float(np.mean((pres>=len(ids)/2).sum(axis=1))),
            absent_layer_expert_pairs=int((count==0).sum()),
            half_overlap=mean([r['top64_half_overlap'] for r in stability['within_request'] if r['phase']==phase]),
            request_pair_overlap=mean([r['top64_overlap'] for r in pairs if r['phase']==phase]),
            same_scenario_pair_overlap=mean([r['top64_overlap'] for r in pairs if r['phase']==phase and r['same_scenario']]),
            different_scenario_pair_overlap=mean([r['top64_overlap'] for r in pairs if r['phase']==phase and not r['same_scenario']]),
            multi_sample_scenario_overlap=mean([r['top64_overlap'] for r in stability['between_scenarios'] if r['phase']==phase and len(pops[r['a']])>=4 and len(pops[r['b']])>=4]))
    notable['prefill_decode_top64_overlap']=mean([r['top64_overlap'] for r in stability['prefill_decode'] if r['population']=='authored:unique_input'])
    notable['prefill_decode_top128_overlap']=mean([r['top128_overlap'] for r in stability['prefill_decode'] if r['population']=='authored:unique_input'])
    save(out/'findings.json',notable)
    text='# MoE 专家ID分布统计\n\n统计按层进行；Top-N是同样本描述性集中度，不是缓存命中。以下比例为48层均值。\n\n'
    text+='| 样本组（输入去重） | 阶段 | Top10 | Top32 | Top64 | Top128 | Top256 | 活跃专家均值 |\n|---|---|---:|---:|---:|---:|---:|---:|\n'
    for r in summary:
        if r['population'] in ['authored:unique_input','pilot:unique_input','quality:unique_input','performance:unique_input'] and r['weighting']=='route':
            text+='| '+r['population']+' | '+r['phase']+' | '+' | '.join(f"{100*r[k]['mean']:.2f}%" for k in ['top10','top32','top64','top128','top256'])+f" | {r['active']['mean']:.2f} |\n"
    text+='\n## 分场景（至少4个输入，每场景独立排名）\n\n| 场景 | 阶段 | Top64 | Top128 | Top256 |\n|---|---|---:|---:|---:|\n'
    for r in summary:
        if r['population'].startswith('scenario:') and r['requests']>=4 and r['weighting']=='route':
            text+='| '+r['population'][9:]+' | '+r['phase']+' | '+' | '.join(f"{100*r[k]['mean']:.2f}%" for k in ['top64','top128','top256'])+' |\n'
    (out/'tables.md').write_text(text)
    save(out/'verification.json',dict(populations_checked=len(pops),weighted_and_equal_request_recomputed=True,request_pairs=len(pairs)))
    print(json.dumps(notable,indent=2))


if __name__=='__main__':
    main()
