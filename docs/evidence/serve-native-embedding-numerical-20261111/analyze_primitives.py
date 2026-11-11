import json,numpy as np, torch
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor/.q4t-work/native-embedding-20261111'); base=root/'diagnostic-01'; native=base/'native/case-0'; ref=base/'reference/case-0'
def bits(path):return np.fromfile(path,dtype='<u2')
def floats(raw):return (raw.astype('<u4')<<16).view('<f4')
def read(path):return floats(bits(path))
file=root/'assets/model.safetensors';raw=np.memmap(file,mode='r',dtype='u1');hbytes=int.from_bytes(raw[:8],'little');header=json.loads(bytes(raw[8:8+hbytes]));data=8+hbytes
def weight(name):
 h=header[name];a,b=h['data_offsets'];return floats(raw[data+a:data+b].view('<u2')).reshape(h['shape'])
def bfround(value):return torch.from_numpy(np.asarray(value,dtype='float32')).bfloat16().float().numpy()
x=torch.from_numpy(read(native/'layer-00.bf16').reshape(17,1024));w=torch.from_numpy(weight('layers.0.input_layernorm.weight'))
x=(w*(x*torch.rsqrt(x.pow(2).mean(-1,keepdim=True)+1e-6)).bfloat16()).bfloat16().float().numpy()
report=[]
for name,matrix,source in [('q','self_attn.q_proj',x),('k','self_attn.k_proj',x),('v','self_attn.v_proj',x),('up','mlp.up_proj',read(native/'post_norm.bf16').reshape(17,1024)),('projected','mlp.down_proj',read(native/'gate.bf16').reshape(17,3072))]:
 w=weight('layers.0.'+matrix+'.weight').astype('float64');a=source.astype('float64'); exact=a@w.T; gamma=a.shape[1]*2**-24/(1-a.shape[1]*2**-24); bound=gamma*(abs(a)@abs(w).T)
 raw_y=bits(native/(name+'.bf16')).reshape(exact.shape);y=floats(raw_y).astype('float64');left=floats((raw_y-1).astype('uint16')).astype('float64');right=floats((raw_y+1).astype('uint16')).astype('float64');low=(np.minimum(left,right)+y)/2;high=(np.maximum(left,right)+y)/2
 violated=(exact<low-bound)|(exact>high+bound);reference=read(ref/(name+'.bf16')).reshape(exact.shape)
 item=dict(name=name,values=y.size,fp32_accumulate_rounding_bound_violations=int(violated.sum()),native_vs_reference_differences=int((y!=reference).sum()),native_vs_nearest_fp64_bf16=int((y!=bfround(exact)).sum()),max_abs_error=float(abs(y-exact).max()),max_fp32_error_bound=float(bound.max()));report.append(item);print(item,flush=True)
for name,heads in [('q',16),('k',8)]:
 value=torch.from_numpy(read(native/(name+'.bf16')).reshape(17,heads,128));w=torch.from_numpy(weight('layers.0.self_attn.'+name+'_norm.weight'))
 norm=(w*(value*torch.rsqrt(value.pow(2).mean(-1,keepdim=True)+1e-6)).bfloat16()).bfloat16()
 rope=torch.from_numpy(read(native/'rope.bf16').reshape(17,128)).bfloat16();cos=torch.cat([rope[:,:64],rope[:,:64]],-1)[:,None,:];sin=torch.cat([rope[:,64:],rope[:,64:]],-1)[:,None,:]
 result=(norm*cos + torch.cat([-norm[...,64:],norm[...,:64]],-1)*sin).float().numpy().transpose(1,0,2)
 actual=read(native/('head_'+name+'.bf16')).reshape(heads,32,128)[:,:17,:];item=dict(name='norm_rope_'+name,different=int((result!=actual).sum()),maximum_error=float(abs(result-actual).max()));report.append(item);print(item,flush=True)
(base/'rounding-analysis.json').write_text(json.dumps(report,indent=2)+'\n')
