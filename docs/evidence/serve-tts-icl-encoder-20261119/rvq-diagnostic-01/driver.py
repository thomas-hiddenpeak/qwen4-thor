"""One long-case interior RVQ diagnosis; no qualification gate relaxation."""
import fcntl,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from tools.native_tts.encoder_acceptance import defaults,digest
import numpy as np
D=Path(__file__).resolve().parent
p=json.loads((D/'plan.json').read_text())
fd=int(os.environ['Q4T_MANAGED_LEASE_FD']);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
assert p['diagnostic_limits']==dict(seconds=300,native_model_loads=1,cpu_model_loads=1,cases=1,row=94,gpu_launches=1,http=0)
assert defaults()==p['default_before']
assert {n:digest(ROOT/n) for n in p['runtime_sources']}==p['runtime_sources']
assert {n:digest(Path(n)) for n in p['diagnostic_sources']}==p['diagnostic_sources']
assert not os.environ.get('PYTHONOPTIMIZE')
O=D/'run';O.mkdir(exist_ok=False);start=time.monotonic();r=dict(status='RUNNING',loads=[])
env=dict(os.environ,PYTHONPATH=str(ROOT/'.q4t-work/serve-audio-native-20261117/reference-deps')+':'+str(ROOT/'tools/native_tts'),PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',OMP_NUM_THREADS='8')
try:
 commands=[('native',[str(D/'q4t_encoder_diagnostic'),p['assets'],str(D/'plan.json'),str(O/'native')]),('reference',[str(ROOT/'.q4t-work/refenv/bin/python'),'-B',str(D/'reference.py'),'--assets',p['assets'],'--official-source',str(ROOT/'.q4t-work/serve-native-tts-20261118/source-01/official/qwen_tts/core/tokenizer_12hz'),'--plan',str(D/'plan.json'),'--output',str(O/'reference')])]
 for name,cmd in commands:
  r['loads'].append(name)
  with (O/(name+'.log')).open('wb') as log:
   child=subprocess.run(cmd,cwd=ROOT,env=env,pass_fds=(fd,),stdout=log,stderr=subprocess.STDOUT,timeout=290-(time.monotonic()-start))
  assert child.returncode==0,(name,child.returncode)
 # Same source graph and input must reproduce the saved codes exactly.
 old=Path(p['previous_run'])
 for side in ('native','reference'):
  assert digest(O/side/'case-2/codes.i32')==digest(old/side/'case-2/codes.i32')
  for f in (old/side/'case-2').glob('*.f32'):assert digest(f)==digest(O/side/'case-2'/f.name)
 file=Path(p['assets'])/'model.safetensors'
 with file.open('rb') as f:
  length=int.from_bytes(f.read(8),'little');header=json.loads(f.read(length))
 def tensor(name):
  item=header['encoder.quantizer.'+name];assert item['dtype']=='F32'
  return np.memmap(file,dtype='<f4',mode='r',offset=8+length+item['data_offsets'][0],shape=tuple(item['shape']))
 def book(group):
  base=('semantic_residual_vector_quantizer.layers.0' if group==0 else f'acoustic_residual_vector_quantizer.layers.{group-1}')+'.codebook.'
  return (tensor(base+'embed_sum')/np.maximum(tensor(base+'cluster_usage'),np.float32(1e-5))[:,None]).astype(np.float64)
 checks=[]
 for group in range(16):
  centroid=book(group);inputs=[];entry=dict(group=group)
  name=f'encoder-projection-{group}-2.f32' if group<2 else f'encoder-rvq-residual-{group-1}-2.f32'
  for side in ('native','reference'):
   x=np.fromfile(O/side/'case-2'/name,'<f4').astype(np.float64);assert x.shape==(256,) and np.isfinite(x).all();inputs.append(x)
   dist=np.sum((centroid-x)**2,axis=1);order=np.argsort(dist,kind='stable')[:3]
   code=int(np.fromfile(O/side/'case-2/codes.i32','<i4').reshape(-1,16)[94,group])
   entry[side]=dict(code=code,nearest=[int(i) for i in order],distances=[float(dist[i]) for i in order],selected_distance=float(dist[code]),selected_excess=float(dist[code]-dist[order[0]]))
  entry['input_relative_l2']=float(np.linalg.norm(inputs[0]-inputs[1])/np.linalg.norm(inputs[1]));checks.append(entry)
 r.update(status='DIAGNOSIS_COMPLETED_NOT_QUALIFICATION',checks=checks)
except Exception as e:r.update(status='FAILED',error=repr(e))
finally:
 r['seconds']=time.monotonic()-start;r['defaults_unchanged']=defaults()==p['default_before'];r['runtime_sources_unchanged']={n:digest(ROOT/n) for n in p['runtime_sources']}==p['runtime_sources']
 if not r['defaults_unchanged'] or not r['runtime_sources_unchanged']:r.update(status='FAILED',error='identity changed')
 r['artifacts']={str(f.relative_to(O)):digest(f) for f in sorted(O.rglob('*')) if f.is_file()};(O/'result.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps({k:r[k] for k in ('status','seconds','defaults_unchanged')}));raise SystemExit(0 if r['status'].startswith('DIAGNOSIS_') else 1)
