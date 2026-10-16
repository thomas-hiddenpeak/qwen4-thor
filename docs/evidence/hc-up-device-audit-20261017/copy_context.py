import json,sqlite3
from pathlib import Path
out=Path(__file__).resolve().parent;r=json.loads((out/'device-audit.json').read_text());result={}
for side in ['A','B']:
 db=sqlite3.connect('file:'+r[side]['source']+'?mode=ro',uri=True);names=dict(db.execute('select id,value from StringIds'));rows=[]
 for chunk in [2,13,25]:
  g=r[side]['chunks'][chunk-1]['largest_gaps'][0];c=db.execute('select start,end,streamId,bytes,copyKind,srcKind,dstKind from CUPTI_ACTIVITY_KIND_MEMCPY where correlationId=?',(g['next_correlation'],)).fetchone();assert c
  prev=db.execute('select start,end,nameId from CUPTI_ACTIVITY_KIND_RUNTIME where start<? order by start desc limit 1',(g['next_api']['start_ns'],)).fetchone()
  following=db.execute('select start,end,demangledName from CUPTI_ACTIVITY_KIND_KERNEL where streamId=? and start>=? order by start limit 1',(c[2],c[1])).fetchone()
  rows.append({'chunk':chunk,'gap':g,'copy':dict(zip(['start_ns','end_ns','stream','bytes','copy_kind','source_kind','dest_kind'],c)),'previous_api':{'start_ns':prev[0],'end_ns':prev[1],'name':names[prev[2]]},'following_kernel':{'start_ns':following[0],'end_ns':following[1],'name':names[following[2]]}})
 result[side]=rows
(out/'copy-context.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({s:[x['following_kernel']['name'] for x in rows] for s,rows in result.items()},indent=2))
