import json,math,re
from pathlib import Path
root=Path(__file__).resolve().parent
read=lambda p:json.loads(p.read_text())
from parse_reports import EXPECTED as expected, parse
runs={};issues=[]
for i,v in enumerate(read(root/'protocol.json')['order'],1):
 name=f'{i:02}-{v}';d=root/'http'/name;ex=read(d/'exit.json');assert ex['server']==0 and ex['completed']==1 and ex['http_output_checks_passed'] and not ex['failure'] and not ex['cleanup_failure']
 families=parse((d/'server.log').read_text())
 totals={family+'.'+k:sum(r[k] for r in rows[1:]) for family,rows in families.items() for k in expected[family]}
 runs[name]={'families':families,'totals_ms_chunks2_25':totals}
lease=read(root/'lease/exit.json')
if lease['status']!='COMPLETED':issues.append({'reason':'guard did not complete'})
resume_lease=read(root/'lease-resume/exit.json')
if resume_lease['status']!='COMPLETED':issues.append({'reason':'resume guard did not complete'})
comparisons={}
for k in runs['01-A']['totals_ms_chunks2_25']:
 a=[runs[n]['totals_ms_chunks2_25'][k] for n in ['01-A','04-A']];b=[runs[n]['totals_ms_chunks2_25'][k] for n in ['02-B','03-B']];R=max(abs(a[1]-a[0]),abs(b[1]-b[0]));ds=[b[i]-a[i] for i in range(2)]
 comparisons[k]={'paired_B_minus_A_ms':ds,'same_identity_R_ms':R,'threshold_ms':max(R,1),'segmented_threshold_met':all(x>max(R,1) for x in ds),'resolved_positive_observable':False}
result={'runs':runs,'validity_issues':issues,'comparisons':comparisons,'continuous_four_request_lease':False,'full_protocol_passed':False,'resume_lease':resume_lease,'causal_proof':False,'performance_acceptance':False}
(root/'subtiming-resumed-review.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'validity_issues':issues,'comparisons':comparisons},indent=2))
