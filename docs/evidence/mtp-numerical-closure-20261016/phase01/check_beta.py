import json,struct,numpy as np
from pathlib import Path
r=Path(__file__).resolve().parent;d=r/'layers'
m=Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
key='model.language_model.layers.0.linear_attn.in_proj_b.weight'
idx=json.loads((m/'model.safetensors.index.json').read_text());p=m/idx['weight_map'][key]
with p.open('rb') as f:
 n=struct.unpack('<Q',f.read(8))[0];h=json.loads(f.read(n));meta=h[key]
assert meta['dtype']=='BF16'
w=np.memmap(p,mode='r',dtype='<u2',offset=8+n+meta['data_offsets'][0],shape=tuple(meta['shape']))
def fp(a):return (np.asarray(a,dtype=np.uint32)<<np.uint32(16)).view(np.float32).astype(np.float64)
x=np.fromfile(d/'partial1_m0.x.bin',dtype='<u2')
a=np.fromfile(d/'partial1_m0.beta.bin',dtype='<u2');b=np.fromfile(d/'ref8_m0.beta.bin',dtype='<u2').reshape(8,-1)[6]
results=[]
for i in np.flatnonzero(a!=b):
 products=fp(w[i])*fp(x)
 exact=np.sum(products.astype(np.longdouble),dtype=np.longdouble)
 va=float(fp(a[i]));vb=float(fp(b[i]));mid=(va+vb)/2
 results.append({'head':int(i),'shape':meta['shape'],'partial':va,'reference':vb,'partial_bits':hex(int(a[i])),'reference_bits':hex(int(b[i])),'longdouble_dot':str(exact),'bf16_midpoint':mid,'distance_to_midpoint':str(exact-np.longdouble(mid)),'nearest_of_two':'partial' if abs(exact-va)<abs(exact-vb) else 'reference','sum_abs_products':float(np.abs(products).sum())})
(r/'beta-reference.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results,indent=2))
