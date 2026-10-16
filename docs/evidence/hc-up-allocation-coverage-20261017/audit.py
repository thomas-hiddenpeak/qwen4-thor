import hashlib,json,re
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
p=ROOT/'.q4t-work/hc-up-address-observer-20261017/allocations.json';raw=p.read_bytes();r=json.loads(raw)
probe=(ROOT/'tools/evalscope/persistent_layout_probe.cpp').read_text()
indices=list(map(int,re.search(r'kIndices\[\] = \{([^}]+)\}',probe)[1].split(',')))
sizes=list(map(int,re.search(r'kSizes\[\] = \{([^}]+)\}',probe)[1].split(',')))
a=[x for x in r['A'] if x['signature'][:2]==['startup','cudaMalloc']];b=[x for x in r['B'] if x['signature'][:2]==['startup','cudaMalloc']]
assert len(a)==len(b)==1541
assert [x['signature'] for x in a]==[x['signature'] for x in b]
assert [a[i]['signature'][2] for i in indices]==sizes
changed=[{'cudaMalloc_index':i,'record_id':x['id'],'bytes':x['signature'][2],'B_minus_A_va':y['ptr']-x['ptr'],'controlled_in_later_VMM':i in indices} for i,(x,y) in enumerate(zip(a,b)) if x['ptr']!=y['ptr']]
assert len(changed)==119 and all(x['B_minus_A_va']==-536870912 for x in changed)
report={'source_sha256':hashlib.sha256(raw).hexdigest(),'source_path':str(p),'probe_sha256':hashlib.sha256(probe.encode()).hexdigest(),'startup_cudaMalloc_calls':len(a),'sum_requested_bytes_not_live_footprint':sum(x['signature'][2] for x in a),'controlled_indices':indices,'controlled_requested_bytes':sum(sizes),'uncontrolled_requested_bytes':sum(x['signature'][2] for i,x in enumerate(a) if i not in indices),'old_observed_VA_changed_count':len(changed),'VA_changed_requested_bytes':sum(x['bytes'] for x in changed),'VA_changed_uncontrolled_count':sum(not x['controlled_in_later_VMM'] for x in changed),'VA_changed_uncontrolled_requested_bytes':sum(x['bytes'] for x in changed if not x['controlled_in_later_VMM']),'changed':changed,'limits':['Address observer and VMM intervention are distinct epochs; no claim same shifts persisted','Summed allocation requests are not physical resident size or live footprint','VA differences do not establish physical-placement differences or causality','Matching signatures not proof identical allocation timing, pages or cache history','No new GPU request']}
(OUT/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({k:v for k,v in report.items() if k not in ('changed','limits')},indent=2))
