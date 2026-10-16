import hashlib,json
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
raw=(ROOT/'.q4t-work/hc-up-address-observer-20261017/allocations.json').read_bytes();r=json.loads(raw)
a=[x for x in r['A'] if x['signature'][:2]==['startup','cudaMalloc']];b=[x for x in r['B'] if x['signature'][:2]==['startup','cudaMalloc']]
sizes=[x['signature'][2] for x in a];assert sizes==[x['signature'][2] for x in b]
# Source ordered nine weight allocations followed by two max_seq=1 states.
pattern=[10240*2560*2,6144*2560*2,48*2560*2,48*2560*2,10240*4*2,2560*6144*2,128*2,48*2,48*2,48*128*128*4,10240*3*2]
starts=[i for i in range(len(sizes)-len(pattern)+1) if sizes[i:i+len(pattern)]==pattern];assert len(starts)==36
roles={'conv1d':4,'A_log':7,'dt_bias':8,'ssm_state':9,'conv_state':10};items=[]
for ordinal,start in enumerate(starts):
 for role,off in roles.items():
  i=start+off;x,y=a[i],b[i]
  items.append({'linear_layer_ordinal':ordinal,'role':role,'cudaMalloc_index':i,'requested_bytes':sizes[i],'A_va':x['ptr'],'B_va':y['ptr'],'VA_equal':x['ptr']==y['ptr']})
summary={role:{'count':36,'changed_ordinals':[x['linear_layer_ordinal'] for x in items if x['role']==role and not x['VA_equal']],'bytes_per_layer':pattern[off]} for role,off in roles.items()}
report={'allocation_source_sha256':hashlib.sha256(raw).hexdigest(),'source_hashes':{str(p):hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in map(Path,['src/model/linear_attention.cu','src/model/decoder_layer.cu'])},'matching_method':'Unique full contiguous eleven-allocation source size pattern, in linear-layer order; inferred roles, no runtime role labels','summary':summary,'items':items,'limits':['Old address observer epoch, not clean-trace or VMM epoch','VA equality not physical/backing/cache identity','Source access pattern not a device stall or byte-traffic measurement','No causal exclusion or new GPU request']}
(OUT/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(summary,indent=2))
