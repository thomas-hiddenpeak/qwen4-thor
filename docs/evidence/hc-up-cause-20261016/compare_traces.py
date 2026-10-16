import sqlite3,json,collections,hashlib
from pathlib import Path
r=Path(__file__).resolve().parent
allrows={}; reports={}
for side in ['A','B']:
 db=sqlite3.connect(f'file:{r/side/"context-204800/trace.sqlite"}?mode=ro',uri=True)
 names=dict(db.execute('select id,value from StringIds'))
 boundary=db.execute('select end from CUPTI_ACTIVITY_KIND_MEMCPY where bytes=496640 and copyKind=2').fetchall();assert len(boundary)==1; boundary=boundary[0][0]
 rows=db.execute('select start,end,demangledName,gridX,gridY,gridZ,blockX,blockY,blockZ,staticSharedMemory,dynamicSharedMemory from CUPTI_ACTIVITY_KIND_KERNEL where end<=? order by start',(boundary,)).fetchall()
 groups=collections.defaultdict(lambda:[0,0]); sequence=[]
 for a,b,n,*cfg in rows:
  key=json.dumps([names[n],*cfg]);groups[key][0]+=1; groups[key][1]+=(b-a)/1e6;sequence.append(key)
 allrows[side]=sequence
 reports[side]={'boundary':boundary,'count':len(rows),'signature_sha256':hashlib.sha256('\n'.join(sequence).encode()).hexdigest(),'hc_up_launches':sum('HcUpFourRows' in k for k in sequence),'groups':groups}
 db.close()
a,b=reports['A']['groups'],reports['B']['groups']; keys=a.keys()|b.keys()
result={'A':{k:v for k,v in reports['A'].items() if k!='groups'},'B':{k:v for k,v in reports['B'].items() if k!='groups'},'sequence_equal':allrows['A']==allrows['B'],'count_differences':{k:[a.get(k),b.get(k)] for k in keys if a.get(k,[0])[0]!=b.get(k,[0])[0]},'kernel_deltas':sorted([{'signature':k,'A':a.get(k,[0,0]),'B':b.get(k,[0,0]),'delta_ms':b.get(k,[0,0])[1]-a.get(k,[0,0])[1]} for k in keys],key=lambda v:-abs(v['delta_ms']))}
(r/'trace-comparison.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='kernel_deltas'},indent=2));print(json.dumps(result['kernel_deltas'][:8],indent=2))
