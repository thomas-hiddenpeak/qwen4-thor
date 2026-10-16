"""Pair identical all-solo operation families by chunk and ordinal."""
import bisect
import json
from pathlib import Path
import sqlite3

out=Path(__file__).resolve().parent
source=out.parent/'hc-up-clean-trace-v2-20261017'
activity=json.loads((source/'device-audit.json').read_text())
concurrency=json.loads((out/'result.json').read_text())
patterns={'gdn':('GatedDeltaNetRegKernel<',36),
          'conv':('CausalConv1dWithCkptKernel(',36),
          'combine':('CombineWithGateKernel(',48)}
records={}
for v in ['A','B']:
    c=sqlite3.connect(f'file:{source/v/"context-204800/trace.sqlite"}?mode=ro',uri=True)
    names=dict(c.execute('select id,value from StringIds'))
    edges=[x['start_ns'] for x in activity[v]['chunks']]
    end=activity[v]['chunks'][-1]['end_ns']
    records[v]={}
    for family,(pattern,per_chunk) in patterns.items():
        ids=[n for n,s in names.items() if pattern in s];assert len(ids)==1
        name=names[ids[0]]
        stats=concurrency[v]['fully_solo'][name]
        assert stats['count']==concurrency[v]['operation_counts'][name]==25*per_chunk
        chunks=[[] for _ in edges]
        for a,b in c.execute('select start,end from CUPTI_ACTIVITY_KIND_KERNEL where demangledName=? and start>=? and end<=? order by start',(ids[0],edges[0],end)):
            chunks[bisect.bisect_right(edges,a)-1].append(b-a)
        assert all(len(x)==per_chunk for x in chunks)
        records[v][family]=chunks
    c.close()
result={}
for family,(_,per_chunk) in patterns.items():
    a,b=records['A'][family],records['B'][family]
    delta=[[y-x for x,y in zip(aa,bb)] for aa,bb in zip(a,b)]
    result[family]={'paired_calls':25*per_chunk,
                    'positive_pairs':sum(d>0 for row in delta for d in row),
                    'negative_pairs':sum(d<0 for row in delta for d in row),
                    'chunk_delta_ns':[sum(row) for row in delta],
                    'layer_ordinal_delta_ns':[sum(row[i] for row in delta) for i in range(per_chunk)],
                    'delta_ns':sum(sum(row) for row in delta),
                    'raw_A_ns':a,'raw_B_ns':b,'paired_delta_ns':delta}
(out/'solo-pairs.json').write_text(json.dumps(result,indent=2)+'\n')
for family,x in result.items():
    print(family,{k:v for k,v in x.items() if not k.startswith(('raw','paired_delta'))})
