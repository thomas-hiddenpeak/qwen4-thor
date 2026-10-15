from pathlib import Path
import numpy as np,json
r=Path(__file__).resolve().parent;d=r/'prefill-layers';out=[]
for b,(start,n) in enumerate([(0,5),(5,3),(8,7)]):
 for p in sorted(d.glob(f'ref{b}*bin')):
  q=d/p.name.replace(f'ref{b}','packed',1)
  if not q.exists() or p.suffix!='.bin':continue
  dtype='<i4' if '.eid.' in p.name else '<f4' if '.rw.' in p.name else '<u2'
  a=np.fromfile(p,dtype=dtype);v=np.fromfile(q,dtype=dtype)
  if a.size%n or v.size!=15*(a.size//n):continue
  v=v.reshape(15,-1)[start:start+n].ravel()
  af=(a.astype(np.uint32)<<16).view(np.float32).astype(float) if dtype=='<u2' else a.astype(float)
  vf=(v.astype(np.uint32)<<16).view(np.float32).astype(float) if dtype=='<u2' else v.astype(float)
  diff=int(np.count_nonzero(a!=v));rel=float(np.linalg.norm(af-vf)/max(np.linalg.norm(af),1e-30))
  out.append(dict(sequence=b,tensor=p.name,diff=diff,l2=rel))
  if diff:print(p.name,diff,round(rel,7))
(r/'prefill-comparison.json').write_text(json.dumps(out,indent=2)+'\n')
