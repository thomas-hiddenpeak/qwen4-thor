"""Read-only window and recorded profiler-overhead audit of existing traces."""
import bisect,json,sqlite3
from pathlib import Path
OUT=Path(__file__).resolve().parent
SOURCE=Path('/home/rm01/models/dev/qwen4-thor/.q4t-work/hc-up-clean-trace-v2-20261017')
old=json.loads((SOURCE/'device-audit.json').read_text())
def union(rows):
 result=[]
 for a,b in sorted(rows):
  if result and a<=result[-1][1]:result[-1]=(result[-1][0],max(b,result[-1][1]))
  else:result.append((a,b))
 return result
def intersection(a,b,spans,ends):
 i=bisect.bisect_right(ends,a);total=0
 while i<len(spans) and spans[i][0]<b:
  total+=max(0,min(b,spans[i][1])-max(a,spans[i][0]));i+=1
 return total
report={}
for v in 'AB':
 c=sqlite3.connect(f'file:{SOURCE/v/"context-204800/trace.sqlite"}?mode=ro',uri=True)
 lo=old[v]['chunks'][0]['start_ns'];hi=old[v]['chunks'][-1]['end_ns']
 names=dict(c.execute('select id,value from StringIds'));r={'window_ns':[lo,hi],'activities':{},'overhead':{}}
 for kind in ('KERNEL','MEMCPY','MEMSET'):
  t='CUPTI_ACTIVITY_KIND_'+kind
  r['activities'][kind]={'cross_start':c.execute(f'select count(*) from {t} where start<? and end>?',(lo,lo)).fetchone()[0], 'cross_end':c.execute(f'select count(*) from {t} where start<? and end>?',(hi,hi)).fetchone()[0], 'nonpositive':c.execute(f'select count(*) from {t} where start<? and end>? and end<=start',(hi,lo)).fetchone()[0], 'identities':list(c.execute(f'select distinct globalPid,deviceId,contextId from {t} where start<? and end>?',(hi,lo)))}
 spans=[]
 for t in ('PROFILER_OVERHEAD','CUPTI_ACTIVITY_KIND_OVERHEAD'):
  rows=list(c.execute(f'select start,end,overheadType from {t} where start<? and end>?',(hi,lo)))
  r['overhead'][t]={'count':len(rows),'by_type':{}}
  for typ in sorted(set(x[2] for x in rows)):
   clipped=[(max(a,lo),min(b,hi)) for a,b,k in rows if k==typ]
   r['overhead'][t]['by_type'][typ]={'count':len(clipped),'sum_ns':sum(b-a for a,b in clipped),'union_ns':sum(b-a for a,b in union(clipped))}
  spans.extend((max(a,lo),min(b,hi)) for a,b,k in rows)
 spans=union(spans);ends=[b for a,b in spans];r['recorded_overhead_union_ns']=sum(b-a for a,b in spans)
 r['families']={}
 for pattern in ('GatedDeltaNetRegKernel<','CausalConv1dWithCkptKernel(','CombineWithGateKernel('):
  ids=[n for n,s in names.items() if pattern in s];assert len(ids)==1
  rows=list(c.execute('select start,end from CUPTI_ACTIVITY_KIND_KERNEL where start>=? and end<=? and demangledName=? order by start',(lo,hi,ids[0])))
  hit=[intersection(a,b,spans,ends) for a,b in rows]
  r['families'][pattern]={'count':len(rows),'recorded_overhead_intersecting_calls':sum(x>0 for x in hit),'intersection_ns':sum(hit),'kernel_duration_sum_ns':sum(b-a for a,b in rows)}
 r['diagnostics']=list(c.execute('select timestamp,timestampType,severity,text from DIAGNOSTIC_EVENT'))
 r['timestamp_sources']=list(c.execute('select * from ENUM_DIAGNOSTIC_TIMESTAMP_SOURCE'))
 r['overhead_types']=list(c.execute('select * from ENUM_CUPTI_OVERHEAD_TYPE'))
 report[v]=r;c.close()
report['limits']=['Recorded profiler host intervals only; temporal overlap is not proof of device contention','No overhead subtraction from kernel durations or unprofiled TTFT','No new GPU requests; one existing instrumented A/B pair cannot establish uninstrumented causality']
(OUT/'result.json').write_text(json.dumps(report,indent=2)+'\n')
for v in 'AB':print(v,json.dumps({k:val for k,val in report[v].items() if k not in ('diagnostics','overhead_types')}))
