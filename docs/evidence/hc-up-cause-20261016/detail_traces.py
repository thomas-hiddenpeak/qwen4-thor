import json,sqlite3,hashlib,collections
from pathlib import Path
r=Path(__file__).resolve().parent; result={}
for side in ['A','B']:
 db=sqlite3.connect(f'file:{r/side/"context-204800/trace.sqlite"}?mode=ro',uri=True)
 names=dict(db.execute('select id,value from StringIds')); end=db.execute('select end from CUPTI_ACTIVITY_KIND_MEMCPY where bytes=496640 and copyKind=2').fetchone()[0]
 streams=collections.defaultdict(lambda:hashlib.sha256());counts=collections.Counter()
 for n,*cfg,s in db.execute('select demangledName,gridX,gridY,gridZ,blockX,blockY,blockZ,staticSharedMemory,dynamicSharedMemory,streamId from CUPTI_ACTIVITY_KIND_KERNEL where end<=? order by start',(end,)):
  streams[s].update((json.dumps([names[n],*cfg])+'\n').encode());counts[s]+=1
 api=[{'start_s':a/1e9,'duration_ms':(b-a)/1e6,'name':names[n]} for a,b,n in db.execute('select start,end,nameId from CUPTI_ACTIVITY_KIND_RUNTIME where end<=? order by end-start desc limit 15',(end,))]
 intervals=db.execute('select start,end from CUPTI_ACTIVITY_KIND_KERNEL where end<=? union all select start,end from CUPTI_ACTIVITY_KIND_MEMCPY where end<=? union all select start,end from CUPTI_ACTIVITY_KIND_MEMSET where end<=? order by start',(end,end,end))
 last=None;gaps=[]
 for a,b in intervals:
  if last is not None and a>last:gaps.append((a-last,last,a))
  last=max(last or b,b)
 result[side]={'streams':{s:{'count':counts[s],'hash':h.hexdigest()} for s,h in streams.items()},'longest_api':api,'longest_gaps':[{'duration_ms':d/1e6,'start_s':a/1e9,'end_s':b/1e9} for d,a,b in sorted(gaps,reverse=True)[:15]]}
 db.close()
(r/'trace-details.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
