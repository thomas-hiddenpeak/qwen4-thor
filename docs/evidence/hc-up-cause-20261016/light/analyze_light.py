import json,re
from pathlib import Path
r=Path(__file__).resolve().parent; result={}
for name in ['01-A','02-B']:
 text=(r/name/'server.log').read_text().split('serving on port',1)[1]
 chunks=[];alloc=[]
 for line in text.splitlines():
  m=re.search(r'\[alloc-probe\] start_ns=(\d+) duration_ns=(\d+) bytes=(\d+) rc=(\d+)',line)
  if m:alloc.append(dict(zip(['start_ns','duration_ns','bytes','rc'],map(int,m.groups()))))
  m=re.search(r'\[layer-timing\] T=(\d+) embed=([\d.]+)ms lin=([\d.]+)ms full=([\d.]+)ms head=([\d.]+)ms total=([\d.]+)ms',line)
  if m:
   v=list(map(float,m.groups()));
   if int(v[0])==1:break
   chunks.append({'T':int(v[0]),'embed_ms':v[1],'lin_ms':v[2],'full_ms':v[3],'head_ms':v[4],'total_ms':v[5],'allocation_calls':len(alloc),'allocation_ms':sum(x['duration_ns'] for x in alloc)/1e6,'max_allocation_ms':max((x['duration_ns'] for x in alloc),default=0)/1e6,'allocation_sizes':[x['bytes'] for x in alloc],'errors':sum(x['rc']!=0 for x in alloc)});alloc=[]
   if len(chunks)==25:break
 assert [x['T'] for x in chunks]==[8192]*24+[8181]
 result[name]={'chunks':chunks,'totals':{key:sum(x[key] for x in chunks) for key in ['total_ms','lin_ms','full_ms','embed_ms','head_ms','allocation_calls','allocation_ms','errors']}}
a=result['01-A'];b=result['02-B'];result['allocation_size_sequence_equal']=all(x['allocation_sizes']==y['allocation_sizes'] for x,y in zip(a['chunks'],b['chunks']))
result['deltas']={k:b['totals'][k]-v for k,v in a['totals'].items()}
result['chunk_deltas_ms']=[y['total_ms']-x['total_ms'] for x,y in zip(a['chunks'],b['chunks'])]
(r/'review.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k not in ['01-A','02-B']},indent=2));print(a['totals']);print(b['totals'])
