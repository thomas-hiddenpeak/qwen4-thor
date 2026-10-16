import json
from pathlib import Path
from cfg import constants
cases=[
 ('spill_reload',['adrp x0, 1000 <p>','str x0, [sp, #16]','ldr x2, [sp, #16]','ret'],4096),
 ('disjoint_store',['adrp x0, 1000 <p>','str x0, [sp, #16]','str w3, [sp, #32]','ldr x2, [sp, #16]','ret'],4096),
 ('overlap_store',['adrp x0, 1000 <p>','str x0, [sp, #16]','str w3, [sp, #20]','ldr x2, [sp, #16]','ret'],None),
 ('call_kills',['adrp x0, 1000 <p>','str x0, [sp, #16]','bl 8000 <callee>','ldr x2, [sp, #16]','ret'],None),
 ('unknown_store',['adrp x0, 1000 <p>','str x0, [sp, #16]','str x3, [x4]','ldr x2, [sp, #16]','ret'],None),
 ('sp_change',['adrp x0, 1000 <p>','str x0, [sp, #16]','sub sp, sp, #0x10','ldr x2, [sp, #16]','ret'],None),
]
results=[]
for name,seq,expected in cases:
 row={'instructions':seq,'address':0,'size':len(seq)*4}
 actual=constants(row)[len(seq)-1].get('x2',(None,))[0]
 assert actual==expected,(name,actual,expected)
 results.append({'name':name,'passed':True})
Path(__file__).with_name('checks.json').write_text(json.dumps(results,indent=2)+'\n')
print('6 conservative stack propagation checks passed')
