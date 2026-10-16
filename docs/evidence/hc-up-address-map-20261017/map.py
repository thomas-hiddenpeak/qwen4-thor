import json,collections
from pathlib import Path
r=Path('.q4t-work/hc-up-address-observer-20261017');alloc=json.loads((r/'allocations.json').read_text())
expected=[]
align=lambda n:(n+255)//256*256
for chunk,T in enumerate([8192]*24+[8181],1):
 for layer in range(48):
  if layer%4!=3:
   for name,width in [('qkv_raw',10240),('qkv',10240),('z',6144),('a',48),('beta',48),('y_ssm',6144)]:expected.append((chunk,layer,name,T*width*2))
  metadata=align(512*4)+align(512*T*4)+align(513*4)+align(T*10*4)
  expected.append((chunk,layer,'routing_metadata',metadata))
assert len(expected)==6600
mapped={}
for v in ['A','B']:
 rows=[x for x in alloc[v] if x['signature'][1]=='cudaMallocAsync'][:len(expected)]
 assert len(rows)==len(expected)
 mapped[v]=[]
 for x,(chunk,layer,name,size) in zip(rows,expected):
  assert x['signature'][2]==size,(v,x,chunk,layer,name,size)
  mapped[v].append(dict(x,chunk=chunk,layer=layer,buffer=name))
diff=[];bybuf=collections.Counter();bychunk=collections.Counter()
for a,b in zip(mapped['A'],mapped['B']):
 if a['ptr']!=b['ptr']:
  diff.append({'id':a['id'],'chunk':a['chunk'],'layer':a['layer'],'buffer':a['buffer'],'A_ptr':hex(a['ptr']),'B_ptr':hex(b['ptr']),'delta_bytes':b['ptr']-a['ptr']})
  bybuf[a['buffer']]+=1;bychunk[a['chunk']]+=1
assert len(diff)==858
out={'mapping':'Exact full 6600-allocation prefill sequence matches source shape/order, not just size lookup; no runtime stack capture','mapped_per_side':6600,'different_addresses':len(diff),'by_buffer':dict(bybuf),'by_chunk':dict(bychunk),'differences':diff,'first_difference':diff[0],'last_difference':diff[-1],'all_async_alloc_and_free_streams':'verify separately from raw records','causal_proof':False}
p=Path('.q4t-work/hc-up-address-map-20261017/result.json');p.write_text(json.dumps(out,indent=2)+'\n');print(json.dumps({k:v for k,v in out.items() if k!='differences'},indent=2))
