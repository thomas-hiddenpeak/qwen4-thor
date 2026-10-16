"""Validate complete captured routes, then compare every ordered expert ID."""
import collections,hashlib,itertools,json,sys
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'tools/trace'))
from offload_trace import validate_request,iter_layers
p=json.loads((OUT/'protocol.json').read_text());lease=json.loads((OUT/'lease/exit.json').read_text())
assert lease['status']=='COMPLETED' and lease['child_returncode']==0
checker=ROOT/'build/q4t_router_trace_check';assert hashlib.sha256(checker.read_bytes()).hexdigest()==p['sha256'][str(checker)]
validation={};paths={}
for v in 'AB':
 candidates=list((OUT/'routes'/v).glob('request-*.bin'));assert len(candidates)==1;paths[v]=candidates[0]
 validation[v]=validate_request(paths[v],checker=checker,source_binary=p['binaries'][v])
 s=validation[v]['summary'];assert s['prefill_rows']==204789 and s['output_tokens']==256
 (OUT/f'{v}-validation.json').write_text(json.dumps(validation[v],indent=2)+'\n')
assert validation['A']['summary']==validation['B']['summary']
assert paths['A'].with_suffix('.tokens').read_bytes()==paths['B'].with_suffix('.tokens').read_bytes()
counts=collections.Counter();first=None;digests={v:hashlib.sha256() for v in 'AB'};forwards={v:set() for v in ('prefill','decode')};changed=[]
for a,b in itertools.zip_longest(iter_layers(paths['A'],validation['A']),iter_layers(paths['B'],validation['B'])):
 assert a is not None and b is not None
 keys=('forward_id','stage','position','rows','layer','topk')
 assert all(a[k]==b[k] for k in keys)
 phase=a['phase'];forwards[phase].add(a['forward_id'])
 aa,bb=a['topk_ids'],b['topk_ids'];assert len(aa)==len(bb)
 counts[phase+'_ids']+=len(aa);counts[phase+'_layer_frames']+=1
 digests['A'].update(aa.tobytes());digests['B'].update(bb.tobytes())
 if aa==bb:continue
 diff=[i for i,(x,y) in enumerate(zip(aa,bb)) if x!=y];counts[phase+'_different_slots']+=len(diff)
 tokens=sorted(set(i//a['topk'] for i in diff));counts[phase+'_different_token_layer_rows']+=len(tokens)
 counts[phase+'_different_expert_sets']+=sum(set(aa[t*a['topk']:(t+1)*a['topk']])!=set(bb[t*a['topk']:(t+1)*a['topk']]) for t in tokens)
 changed.append({k:a[k] for k in keys}|{'different_slots':len(diff),'different_rows':len(tokens)})
 if first is None:
  i=diff[0];first={k:a[k] for k in keys}|{'token_position':a['position']+i//a['topk'],'slot':i%a['topk'],'A':aa[i],'B':bb[i]}
assert len(forwards['prefill'])==25
result={'valid':True,'complete_request_compared':True,'equal_all_ordered_ids':first is None,'counts':dict(counts),'forwards':{k:len(v) for k,v in forwards.items()},'ordered_ID_sha256':{v:h.hexdigest() for v,h in digests.items()},'first_difference':first,'changed_layer_frames':changed,'limits':['Capture adds copies and worker IO; no original uninstrumented route proof','No routing scores or physical weight access measurements','Capture TTFT excluded from performance acceptance and causal attribution']}
(OUT/'comparison.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='changed_layer_frames'},indent=2))
