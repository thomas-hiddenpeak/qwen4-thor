"""Reuse process activity union decomposition on the frozen clean pair."""
import bisect,collections,datetime,json,sqlite3
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor');out=Path(__file__).resolve().parent
result={}
lease=json.loads((out/'lease/exit.json').read_text())
assert lease['status']=='COMPLETED' and lease['child_returncode']==0
for side in ['A','B']:
 path=out/side/'context-204800/trace.sqlite'
 db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
 db.execute('pragma temp_store=MEMORY')
 names=dict(db.execute('select id,value from StringIds'))
 end=db.execute('select end from CUPTI_ACTIVITY_KIND_MEMCPY where bytes=496640 and copyKind=2 order by start limit 1').fetchone()[0]
 embed=[n for n,v in names.items() if 'EmbedLookupKernel(' in v and v.startswith('q4t::')];assert len(embed)==1
 starts=[r[0] for r in db.execute('select start from CUPTI_ACTIVITY_KIND_KERNEL where demangledName=? and start<? order by start',(embed[0],end))];assert len(starts)==25
 edges=starts+[end];chunks=[{'chunk':i+1,'start_ns':a,'end_ns':b,'busy_ns':0,'gaps_ns':0,'kernels':0,'kernel_sum_ns':0,'kernel_family_sum_ns':collections.Counter(),'largest_gaps':[]} for i,(a,b) in enumerate(zip(edges,edges[1:]))]
 lasts=starts.copy();gaps=[]
 sql='select start,end,correlationId,demangledName,0 from CUPTI_ACTIVITY_KIND_KERNEL where end<=? union all select start,end,correlationId,-1,1 from CUPTI_ACTIVITY_KIND_MEMCPY where end<=? union all select start,end,correlationId,-2,2 from CUPTI_ACTIVITY_KIND_MEMSET where end<=? order by start'
 for a,b,c,n,kind in db.execute(sql,(end,end,end)):
  if b<=edges[0]:continue
  index=max(0,bisect.bisect_right(starts,a)-1)
  if kind==0:
   chunk=chunks[index];chunk['kernels']+=1;chunk['kernel_sum_ns']+=b-a
   name=names[n]
   family='nvjet' if name.startswith('nvjet') else 'cutlass' if 'cutlass' in name else 'q4t' if 'q4t::' in name else 'other'
   chunk['kernel_family_sum_ns'][family]+=b-a
  for i in range(index,25):
   lo=max(a,edges[i]);hi=min(b,edges[i+1])
   if hi<=lo:break
   if lo>lasts[i]:
    gap={'chunk':i+1,'start_ns':lasts[i],'end_ns':lo,'duration_ns':lo-lasts[i],'next_correlation':c,'next_kind':kind,'next_name':names[n] if kind==0 else str(kind)}
    gaps.append(gap);chunks[i]['gaps_ns']+=lo-lasts[i]
   chunks[i]['busy_ns']+=max(0,hi-max(lo,lasts[i]));lasts[i]=max(lasts[i],hi)
   if b<=edges[i+1]:break
 for i,ch in enumerate(chunks):
  if lasts[i]<edges[i+1]:
   gap={'chunk':i+1,'start_ns':lasts[i],'end_ns':edges[i+1],'duration_ns':edges[i+1]-lasts[i],'next_correlation':None,'next_kind':None,'next_name':'chunk boundary'};gaps.append(gap);ch['gaps_ns']+=gap['duration_ns']
  assert ch['busy_ns']+ch['gaps_ns']==ch['end_ns']-ch['start_ns']
 selected={g['next_correlation'] for g in gaps if g['next_correlation'] is not None}
 apis={}
 for a,b,c,n in db.execute('select start,end,correlationId,nameId from CUPTI_ACTIVITY_KIND_RUNTIME where start<?',(end,)):
  if c in selected:
   assert c not in apis,('duplicate correlation',side,c)
   apis[c]=(a,b,n)
 for g in gaps:
  api=apis.get(g['next_correlation'])
  if api:
   a,b,n=api;g['next_api']={'start_ns':a,'end_ns':b,'name':names[n]}
   g['before_next_api_entry_ns']=max(0,min(g['end_ns'],a)-g['start_ns'])
  else:g['before_next_api_entry_ns']=0
 for ch in chunks:
  gs=[g for g in gaps if g['chunk']==ch['chunk']]
  ch['gap_count']=len(gs);ch['before_next_api_entry_ns']=sum(g['before_next_api_entry_ns'] for g in gs)
  ch['unmatched_gap_ns']=sum(g['duration_ns'] for g in gs if 'next_api' not in g)
  ch['largest_gaps']=sorted(gs,key=lambda g:g['duration_ns'],reverse=True)[:3]
 origin=db.execute('select utcEpochNs from TARGET_INFO_SESSION_START_TIME').fetchone()[0]
 summaries={}
 for label,cs in [('all',chunks),('chunks2_25',chunks[1:])]:
  summaries[label]={k:sum(x[k] for x in cs) for k in ['busy_ns','gaps_ns','kernels','kernel_sum_ns','before_next_api_entry_ns','unmatched_gap_ns']}
  summaries[label]['wall_ns']=sum(x['end_ns']-x['start_ns'] for x in cs)
 result[side]={'source':str(path),'origin_utc_ns':origin,'chunks':chunks,'summary':summaries}
 print(side,json.dumps(summaries),flush=True)
 db.close()
result['scope']='Process-scoped activity union, not whole-device idle. Fresh exclusive capture; trace instrumentation alters execution. Kernel sums double-count concurrency. API chronology is not causal attribution.'
(out/'device-audit.json').write_text(json.dumps(result,indent=2)+'\n')
