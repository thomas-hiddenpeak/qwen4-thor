"""Per-layer descriptive minimal top-N at explicit targets, not cache sizing."""
import argparse
import csv
import json
from pathlib import Path
import struct
import numpy as np
from analyze import frames, sha
from distribution import save


def first_n(curve, target):
    return int(np.searchsorted(curve, target, side='left'))+1


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--directory',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--population',default='authored:unique_input')
    args=ap.parse_args();source=args.directory.resolve();out=args.output.resolve()
    root=Path(__file__).resolve().parents[2]
    if not any(out.is_relative_to(root/p) for p in ['build','.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    records=json.loads((source/'requests.json').read_text())
    pops=json.loads((source/'populations.json').read_text());ids=pops[args.population]
    counts=np.load(source/'population-counts.npz')[args.population+':counts']
    order=np.argsort(-counts,axis=2,kind='stable')
    ranks=np.empty_like(order)
    np.put_along_axis(ranks,order,np.broadcast_to(np.arange(1,513),order.shape),axis=2)
    maximum=np.zeros((2,48,513),dtype=np.int64)
    minimum=np.zeros_like(maximum)
    phase_rows=np.sum([records[i]['phase_rows'] for i in ids],axis=0)
    for i in ids:
        record=records[i];path=root/record['trace'];assert sha(path)==record['trace_sha256']
        events=frames(path);next(events);pending=[]
        for payload in events:
            kind=struct.unpack('<I',payload[:4])[0];body=payload[12:]
            if kind==2:
                _,stage,_,rows=struct.unpack('<QIQI',body);pending=[]
            elif kind==3:
                layer=struct.unpack('<I',body[:4])[0]
                experts=np.frombuffer(body[4:],dtype='<u2').reshape(rows,10)
                r=ranks[stage-1,layer][experts]
                pending.append((layer,np.bincount(r.max(axis=1),minlength=513),
                                np.bincount(r.min(axis=1),minlength=513)))
            elif kind==4:
                if body==b'\1\1\1':
                    for layer,hi,lo in pending:
                        maximum[stage-1,layer]+=hi;minimum[stage-1,layer]+=lo
                pending=[]
        assert sha(path)==record['trace_sha256']
    out.mkdir(parents=True,exist_ok=False)
    curves=[];targets=[]
    for s,phase in enumerate(['prefill','decode']):
        for layer in range(48):
            denom=int(phase_rows[s]);assert maximum[s,layer].sum()==minimum[s,layer].sum()==denom
            assert counts[s,layer].sum()==denom*10
            if not denom:raise ValueError('selected phase has no routing events')
            coverage=np.cumsum(counts[s,layer][order[s,layer]])/(denom*10)
            all_p=np.cumsum(maximum[s,layer])[1:]/denom
            any_p=np.cumsum(minimum[s,layer])[1:]/denom
            assert coverage[-1]==all_p[-1]==any_p[-1]==1
            assert np.all(all_p<=coverage+1e-12) and np.all(coverage<=any_p+1e-12)
            assert all(np.all(np.diff(c)>=0) for c in [coverage,all_p,any_p])
            row=dict(phase=phase,layer=layer,routing_events=denom)
            for metric,curve,thresholds in [('coverage',coverage,[.8,.9,.95,.99]),('all10',all_p,[.5,.8,.9,.95])]:
                for t in thresholds:
                    n=first_n(curve,t)
                    assert curve[n-1]>=t and (n==1 or curve[n-2]<t)
                    row[f'{metric}_{round(t*100)}_min_n']=n
            targets.append(row)
            for n in range(1,513):
                curves.append(dict(phase=phase,layer=layer,n=n,selection_coverage=float(coverage[n-1]),
                    expected_hits=float(coverage[n-1]*10),probability_all=float(all_p[n-1]),probability_any=float(any_p[n-1])))
    for name,rows in [('curves.csv',curves),('targets.csv',targets)]:
        with (out/name).open('x') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    text='# 分层Top-N最小值（指定目标，不是运行时最优容量）\n\n'
    text+=f'{args.population}，{len(ids)}输入，同样本分别按阶段和层排名，ID升序破同分。\n'
    text+='完整扫描N=1..512。prefill按token路由，不是块内并集；名单为事后描述，不作新请求保证。\n'
    for phase in ['prefill','decode']:
        text+=f'\n## {phase}\n\n| 层 | 选择覆盖80% | 90% | 95% | 99% | 全10个概率50% | 80% | 90% | 95% |\n|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n'
        for row in targets:
            if row['phase']==phase:text+='| '+str(row['layer'])+' | '+' | '.join(str(v) for k,v in row.items() if k.endswith('_min_n'))+' |\n'
    (out/'targets.md').write_text(text)
    np.savez_compressed(out/'rank-counts.npz',order=order,maximum_rank_histogram=maximum,minimum_rank_histogram=minimum)
    save(out/'verification.json',dict(passed=True,inputs=len(ids),phase_rows=phase_rows.tolist(),
        full_curves_monotonic=True,target_minimality_checked=True,tool_sha256=sha(Path(__file__)),
        source_sha256={f:sha(source/f) for f in ['requests.json','populations.json','population-counts.npz']}))


if __name__=='__main__':main()
