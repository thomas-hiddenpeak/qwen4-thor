import json,hashlib
from pathlib import Path

r=Path(__file__).resolve().parent
p=json.loads((r/'protocol.json').read_text());runs=[];sources={}
for i,(v,mode) in enumerate(p['order'],1):
 d=r/'http'/f'{i:02}-{v}-{mode}'
 ex=json.loads((d/'exit.json').read_text());assert ex['server']==0 and ex['completed']==1 and ex['http_output_checks_passed'] and not ex['failure'] and not ex['cleanup_failure']
 coverage=json.loads((d/'server-address-space.json').read_text());assert coverage['aslr_disabled']==(mode=='no-aslr');result=json.loads((d/'results.json').read_text())[0]
 runs.append({'name':d.name,'variant':v,'mode':mode,'coverage':coverage,'ttft':result['metrics'][0]['ttft'],'decode_tps':result['metrics'][0]['decode_tps'],'output':result['outputs'],'prompt':result['prompt_sha256']})
 for n in ['server.log','exit.json','results.json','events.jsonl','server-address-space.json','server-maps.txt']:sources[str(d/n)]=hashlib.sha256((d/n).read_bytes()).hexdigest()
assert len({tuple(x['output']) for x in runs})==1 and len({x['prompt'] for x in runs})==1
R=max(abs(a['ttft']-b['ttft']) for a in runs for b in runs if (a['variant'],a['mode'])==(b['variant'],b['mode']))
pairs=[]
for begin in [0,4]:
 block={(x['variant'],x['mode']):x['ttft'] for x in runs[begin:begin+4]}
 control=block['B','inherit']-block['A','inherit'];fixed=block['B','no-aslr']-block['A','no-aslr']
 pairs.append({'control_B_minus_A_s':control,'fixed_B_minus_A_s':fixed,'interaction_s':control-fixed})
mapping_issues=[]
for v in ['A','B']:
 for mode in ['inherit','no-aslr']:
  pair=[x for x in runs if x['variant']==v and x['mode']==mode]
  equal=pair[0]['coverage']['executable_mappings']==pair[1]['coverage']['executable_mappings']
  if equal!=(mode=='no-aslr'):mapping_issues.append({'variant':v,'mode':mode,'equal':equal})
lease=json.loads((r/'lease/exit.json').read_text());gate=not mapping_issues and lease['status']=='COMPLETED' and all(x['control_B_minus_A_s']>max(R,.001) and x['interaction_s']>max(2*R,.002) for x in pairs)
result={'mapping_issues':mapping_issues,'runs':runs,'R_s':R,'pairs':pairs,'frozen_interaction_gate_passed':gate,'lease':lease,'historical_causation_proven':False,'performance_acceptance':False,'sources':sources}
(r/'interaction-review.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k not in ['sources','runs']},indent=2))
