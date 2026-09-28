"""Empirical probability of each expert being selected in a layer's top-10.

Denominator is committed token routing events, NOT number of selected IDs.
"""
import argparse
import csv
import json
from pathlib import Path
import numpy as np
from analyze import sha
from distribution import save


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--directory',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--population',default='authored:unique_input')
    args=ap.parse_args();directory=args.directory.resolve();out=args.output.resolve()
    root=Path(__file__).resolve().parents[2]
    if not any(out.is_relative_to(root/p) for p in ['build','.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    pops=json.loads((directory/'populations.json').read_text())
    records=json.loads((directory/'requests.json').read_text())
    arrays=np.load(directory/'population-counts.npz')
    counts=arrays[args.population+':counts']
    ids=pops[args.population]
    rows=[sum(records[i]['phase_rows'][s] for i in ids) for s in range(2)]
    out.mkdir(parents=True,exist_ok=False)
    all_rows=[];top_rows=[];summary=[]
    document='# 每层专家进入top-10的经验概率\n\n'
    document+=f'样本组：{args.population}；{len(ids)}个输入。分母是该阶段实际token路由次数。\n'
    document+='每次选10个不同专家，因此同层512个概率之和为10（1000%），不是100%。\n'
    document+='按完整路由次数加权；Top10名单是统计后最常出现的10个专家，不表示每次固定选择它们。\n'
    for s,phase in enumerate(['prefill','decode']):
        document+=f'\n## {phase}：每层分母 {rows[s]:,} 次路由\n\n| 层（0起） | 最常出现的10个专家：ID（出现概率） |\n|---:|---|\n'
        for layer in range(48):
            values=counts[s,layer]
            assert int(values.sum())==10*rows[s]
            assert np.all(values<=rows[s])
            order=sorted(range(512),key=lambda e:(-values[e],e))
            rank={e:i+1 for i,e in enumerate(order)}
            for e in range(512):
                row=dict(phase=phase,layer=layer,expert=e,rank=rank[e],
                    selected_events=int(values[e]),routing_events=rows[s],
                    probability=float(values[e]/rows[s]) if rows[s] else None)
                all_rows.append(row)
                if rank[e]<=10:top_rows.append(row)
            listed='；'.join(f'{e} ({100*values[e]/rows[s]:.2f}%)' for e in order[:10]) if rows[s] else '无路由，概率未定义'
            document+=f'| {layer} | {listed} |\n'
            summary.append(dict(phase=phase,layer=layer,top_expert=order[0] if rows[s] else None,
                top_probability=float(values[order[0]]/rows[s]) if rows[s] else None,
                top10_expected_count=float(values[order[:10]].sum()/rows[s]) if rows[s] else None))
    top_rows.sort(key=lambda r:(r['phase'],r['layer'],r['rank']))
    for filename,data in [('all-experts.csv',all_rows),('top10-per-layer.csv',top_rows)]:
        with (out/filename).open('x') as f:
            writer=csv.DictWriter(f,fieldnames=list(data[0]));writer.writeheader();writer.writerows(data)
    (out/'top10-per-layer.md').write_text(document)
    save(out/'summary.json',summary)
    save(out/'verification.json',dict(passed=True,population=args.population,inputs=len(ids),
        phase_routing_events=rows,probability_sum_per_nonempty_layer=10,
        all_probabilities_within_zero_one=True,source_sha256={name:sha(directory/name)
        for name in ['population-counts.npz','requests.json','populations.json']},tool_sha256=sha(Path(__file__))))


if __name__=='__main__':
    main()
