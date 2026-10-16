import json,math,re
from pathlib import Path
root=Path(__file__).resolve().parent
read=lambda p:json.loads(p.read_text())
expected={'layer-timing':['embed','lin','full','head','total'],
'sub-timing':['ple','grread','attn','wrar','moe','write','total','lin_inproj','lin_state','lin_outproj','full_qkv','full_core','full_oproj'],
'moe-subtiming':['prep_gu','gemm_gu','prep_dn','gemm_dn','combine','total'],
'hc-mix-subtiming':['norm','down_gemm','silu','up_gemm','gate','inject','combine','total']}
runs={};issues=[]
for i,v in enumerate(read(root/'protocol.json')['order'],1):
 name=f'{i:02}-{v}';d=root/'http'/name;ex=read(d/'exit.json');assert ex['server']==0 and ex['completed']==1 and ex['http_output_checks_passed'] and not ex['failure'] and not ex['cleanup_failure']
 text=(d/'server.log').read_text();assert 'sync failed:' not in text
 families={key:[] for key in expected}
 for line in text.splitlines():
  m=re.match(r'\[([^]]+)\] T=(\d+) ',line)
  if not m or m[1] not in families or int(m[2])<=1:continue
  fields={k:float(x) for k,x in re.findall(r'([a-z_]+)=([\d.]+)',line)}
  assert set(fields)==set(expected[m[1]]),(m[1],fields)
  assert all(math.isfinite(x) and x>=0 for x in fields.values())
  if m[1]=='sub-timing':assert abs(sum(fields[k] for k in ['ple','grread','attn','wrar','moe','write'])-fields['total'])<=.1
  families[m[1]].append({'T':int(m[2]),**fields})
 for key,rows in families.items():assert [r['T'] for r in rows]==[8192]*24+[8181],(name,key,len(rows))
 totals={family+'.'+k:sum(r[k] for r in rows[1:]) for family,rows in families.items() for k in expected[family]}
 runs[name]={'families':families,'totals_ms_chunks2_25':totals}
lease=read(root/'lease/exit.json')
if lease['status']!='COMPLETED':issues.append({'reason':'guard did not complete'})
comparisons={}
for k in runs['01-A']['totals_ms_chunks2_25']:
 a=[runs[n]['totals_ms_chunks2_25'][k] for n in ['01-A','04-A']];b=[runs[n]['totals_ms_chunks2_25'][k] for n in ['02-B','03-B']];R=max(abs(a[1]-a[0]),abs(b[1]-b[0]));ds=[b[i]-a[i] for i in range(2)]
 comparisons[k]={'paired_B_minus_A_ms':ds,'same_identity_R_ms':R,'threshold_ms':max(R,1),'resolved_positive_observable':not issues and all(x>max(R,1) for x in ds)}
result={'runs':runs,'validity_issues':issues,'comparisons':comparisons,'causal_proof':False,'performance_acceptance':False}
(root/'subtiming-review.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'validity_issues':issues,'comparisons':comparisons},indent=2))
