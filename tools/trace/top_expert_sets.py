"""Observed top-N set coverage and exact token-level overlap distribution.

Ranks use the same selected sample: descriptive only, not held-out cache hits.
"""
import argparse
import csv
import json
from pathlib import Path
import struct
import numpy as np
from analyze import frames, sha
from distribution import save


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--directory',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--population',default='authored:unique_input')
    ap.add_argument('--top-n',type=int,default=30)
    args=ap.parse_args();source=args.directory.resolve();out=args.output.resolve()
    root=Path(__file__).resolve().parents[2]
    if not 1<=args.top_n<=512:ap.error('top-n must be 1..512')
    if not any(out.is_relative_to(root/p) for p in ['build','.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    pops=json.loads((source/'populations.json').read_text())
    records=json.loads((source/'requests.json').read_text())
    counts=np.load(source/'population-counts.npz')[args.population+':counts']
    ids=pops[args.population]
    ranking=np.argsort(-counts,axis=2,kind='stable')[:,:,:args.top_n]
    selected=np.zeros((2,48,512),dtype=bool)
    np.put_along_axis(selected,ranking,True,axis=2)
    expected_rows=np.sum([records[i]['phase_rows'] for i in ids],axis=0)
    overlap=np.zeros((2,48,11),dtype=np.int64)
    # Re-read original, already validated traces; bind every input to saved SHA.
    for i in ids:
        record=records[i];path=root/record['trace']
        assert sha(path)==record['trace_sha256']
        events=frames(path);next(events);pending=[]
        for payload in events:
            kind=struct.unpack('<I',payload[:4])[0];body=payload[12:]
            if kind==2:
                _,stage,_,rows=struct.unpack('<QIQI',body);pending=[]
            elif kind==3:
                layer=struct.unpack('<I',body[:4])[0]
                experts=np.frombuffer(body[4:],dtype='<u2').reshape(rows,10)
                hits=selected[stage-1,layer][experts].sum(axis=1)
                pending.append((layer,np.bincount(hits.astype(np.int64),minlength=11)))
            elif kind==4:
                if body==b'\1\1\1':
                    for layer,h in pending:overlap[stage-1,layer]+=h
                pending=[]
        assert sha(path)==record['trace_sha256']
    out.mkdir(parents=True,exist_ok=False)
    layer_rows=[];expert_rows=[]
    text=f'# 同样本每层Top-{args.top_n}专家集合的出现统计\n\n'
    text+=f'样本：{args.population}，{len(ids)}个输入。每阶段/层独立排名，零基层号。\n'
    text+='选择覆盖率=落入集合的选择数/(路由次数×10)；平均命中数=选择覆盖率×10。\n'
    text+='至少一个/全部十个均直接从每次实际top-10计数，不假设专家独立。\n'
    text+='集合用同一批样本事后选取；这是分布描述，不是新请求缓存命中预测。\n'
    for s,phase in enumerate(['prefill','decode']):
        text+=f'\n## {phase}\n\n| 层 | 选择覆盖率 | 每次top-10平均命中个数 | 至少1个概率 | 全部10个概率 |\n|---:|---:|---:|---:|---:|\n'
        for layer in range(48):
            h=overlap[s,layer];n=int(h.sum());assert n==expected_rows[s]
            hits=int(np.dot(h,np.arange(11)))
            assert hits==int(counts[s,layer][selected[s,layer]].sum())
            assert int(counts[s,layer].sum())==n*10
            row=dict(phase=phase,layer=layer,top_n=args.top_n,routing_events=n,
                selected_events=hits,selection_coverage=hits/(n*10) if n else None,
                expected_hits=hits/n if n else None,
                probability_any=1-int(h[0])/n if n else None,
                probability_all=int(h[10])/n if n else None,
                **{'events_hit_'+str(k):int(h[k]) for k in range(11)})
            layer_rows.append(row)
            for rank,e in enumerate(ranking[s,layer],1):
                expert_rows.append(dict(phase=phase,layer=layer,rank=rank,expert=int(e),
                    selected_events=int(counts[s,layer,e]),routing_events=n,
                    probability=float(counts[s,layer,e]/n) if n else None))
            text+=f"| {layer} | {100*row['selection_coverage']:.2f}% | {row['expected_hits']:.3f} | {100*row['probability_any']:.2f}% | {100*row['probability_all']:.2f}% |\n" if n else f'| {layer} | 未定义 | 未定义 | 未定义 | 未定义 |\n'
    for name,rows in [('layers.csv',layer_rows),('experts.csv',expert_rows)]:
        with (out/name).open('x') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    (out/'layers.md').write_text(text)
    save(out/'summary.json',[dict(phase=phase,**{k:float(np.mean([r[k] for r in layer_rows if r['phase']==phase and r[k] is not None])) for k in ['selection_coverage','expected_hits','probability_any','probability_all']}) for phase in ['prefill','decode']])
    save(out/'verification.json',dict(passed=True,inputs=len(ids),phase_rows=expected_rows.tolist(),
        exact_overlap_matches_histograms=True,tool_sha256=sha(Path(__file__)),
        source_sha256={f:sha(source/f) for f in ['requests.json','populations.json','population-counts.npz']}))


if __name__=='__main__':main()
