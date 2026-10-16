import collections,hashlib,json
from pathlib import Path
from parse_addresses import parse
r=Path(__file__).resolve().parent
sides={};normalized={};allocs={};identities={}
for v in ['A','B']:
 d=r/'http'/('01-A' if v=='A' else '02-B')
 ex=json.loads((d/'exit.json').read_text());assert ex['server']==0 and ex['completed']==1 and ex['http_output_checks_passed'] and not ex['failure'] and not ex['cleanup_failure']
 rows=parse((d/'server.log').read_text());assert all(x['rc']==0 for x in rows)
 events={x['event']:x for x in map(json.loads,(d/'events.jsonl').read_text().splitlines())}
 start=events['client_before']['monotonic_seconds']*1e9;end=events['case_completed']['monotonic_seconds']*1e9
 assert any(x['begin_ns']<events['server_ready']['monotonic_seconds']*1e9 for x in rows)
 assert any(start<=x['begin_ns']<=end for x in rows)
 streams={0:0};live={};generation=collections.Counter();bases={};norm=[];alloc=[];unmatched=[];overlaps=[];phasecounts=collections.Counter()
 for x in rows:
  phase='startup' if x['begin_ns']<start else 'request' if x['begin_ns']<=end else 'shutdown'
  phasecounts[phase]+=1
  stream=streams.setdefault(x['stream'],len(streams));domain='host' if 'Host' in x['api'] else 'device';key=(domain,x['ptr'])
  isalloc='Malloc' in x['api'] or 'Alloc' in x['api']
  signature=[phase,x['api'],x['bytes'],x['flags'],stream]
  if isalloc:
   if key in live:overlaps.append({'id':x['id'],'prior':live[key]})
   bases.setdefault(domain,x['ptr']);generation[key]+=1;live[key]=x['id']
   rec={'id':x['id'],'signature':signature,'ptr':x['ptr'],'relative_to_first_domain':x['ptr']-bases[domain],'address_generation':generation[key],'alignment_mod':{str(a):x['ptr']%a for a in [256,4096,65536,2097152]}}
   alloc.append(rec)
  else:
   prior=live.pop(key,None)
   if x['ptr'] and prior is None:unmatched.append(x['id'])
   signature.append(prior)
  norm.append(signature)
 sides[v]={'records':len(rows),'api_counts':dict(collections.Counter(x['api'] for x in rows)),'phase_counts':dict(phasecounts),'unmatched_free_ids':unmatched,'live_at_end':len(live),'overlapping_live_allocations':overlaps,'allocation_count':len(alloc),'reused_address_allocations':sum(a['address_generation']>1 for a in alloc),'normalized_sha256':hashlib.sha256(json.dumps(norm).encode()).hexdigest()}
 normalized[v]=norm;allocs[v]=alloc
 for f in ['server.log','exit.json','results.json','events.jsonl','address-coverage.json']:identities[str(d/f)]=hashlib.sha256((d/f).read_bytes()).hexdigest()
matching=normalized['A']==normalized['B'];comparisons={}
if matching:
 for field in ['ptr','relative_to_first_domain','alignment_mod','address_generation']:
  diff=[{'A':a,'B':b} for a,b in zip(allocs['A'],allocs['B']) if a[field]!=b[field]]
  comparisons[field]={'differing_allocations':len(diff),'first_10':diff[:10]}
lease=json.loads((r/'lease/exit.json').read_text())
result={'sides':sides,'normalized_sequence_equal':matching,'aligned_address_comparisons':comparisons,'lease':lease,'source_sha256':identities,'limitations':['request includes decode','async free matches enqueue not GPU release','virtual addresses are not physical placements','cross-thread entry order can differ without different work','logging perturbs timing; no performance or causal acceptance'],'performance_acceptance':False,'causal_proof':False}
(r/'address-review.json').write_text(json.dumps(result,indent=2)+'\n')
(r/'normalized-sequences.json').write_text(json.dumps(normalized)+'\n')
(r/'allocations.json').write_text(json.dumps(allocs)+'\n')
print(json.dumps({k:v for k,v in result.items() if k not in ['source_sha256','aligned_address_comparisons']},indent=2));print({k:v['differing_allocations'] for k,v in comparisons.items()})
