"""Frozen host observation validity and descriptive comparisons; no acceptance."""
import json,re
from pathlib import Path
root=Path(__file__).resolve().parent
read=lambda p:json.loads(p.read_text())
protocol=read(root/'protocol.json');rows={};issues=[]
fields=['wall_ns','cpu_ns','runqueue_ns','voluntary','involuntary','minflt','majflt','cycles','instructions','branch_misses','enabled_ns','running_ns']
for i,variant in enumerate(protocol['order'],1):
 name=f'{i:02}-{variant}';d=root/'http'/name;exit=read(d/'exit.json')
 assert exit['server']==0 and exit['completed']==1 and exit['http_output_checks_passed'] and not exit['failure'] and not exit['cleanup_failure']
 text=(d/'server.log').read_text();host=[{k:int(v) for k,v in (x.split('=') for x in line.split()[1:])} for line in text.splitlines() if line.startswith('[host-interval]')]
 layer=[(int(t),float(total)) for t,total in re.findall(r'\[layer-timing\] T=(\d+).*?total=([0-9.]+)ms',text) if int(t)>1]
 assert len(host)==25 and [h['seq'] for h in host]==list(range(25))
 assert len({h['tid'] for h in host})==1
 assert [h['T'] for h in host]==[t for t,_ in layer]==[8192]*24+[8181]
 assert all(h['pmu_error']==h['sched_error']==h['usage_rc']==0 and h['pmu_nr']==3 for h in host)
 intervals=[]
 for j,(a,b) in enumerate(zip(host,host[1:]),2):
  delta={k:b[k]-a[k] for k in fields};assert all(v>=0 for v in delta.values()),(name,j,delta)
  ratio=delta['running_ns']/delta['enabled_ns'] if delta['enabled_ns'] else 0
  if ratio<.99:issues.append({'case':name,'chunk':j,'reason':'PMU running/enabled <0.99','ratio':ratio})
  intervals.append({'chunk':j,**delta,'pmu_coverage':ratio,'layer_event_ms':layer[j-1][1]})
 if max(h['capture_ns'] for h in host)>1000000 or sum(h['capture_ns'] for h in host)>5000000:issues.append({'case':name,'reason':'capture overhead validity threshold'})
 rows[name]={'variant':variant,'init_ns':host[0]['init_ns'],'total_capture_ns':sum(h['capture_ns'] for h in host),'totals':{k:sum(x[k] for x in intervals) for k in fields+['layer_event_ms']},'intervals':intervals}
lease=read(root/'lease/exit.json')
if lease['status']!='COMPLETED':issues.append({'reason':'guard did not complete','status':lease['status']})
comparisons={}
for k in fields+['layer_event_ms']:
 a=[rows[x]['totals'][k] for x in ['01-A','04-A']];b=[rows[x]['totals'][k] for x in ['02-B','03-B']];R=max(abs(a[1]-a[0]),abs(b[1]-b[0]));d=[b[i]-a[i] for i in range(2)]
 comparisons[k]={'same_identity_R':R,'paired_B_minus_A':d,'resolved_positive_observable':not issues and all(x>R for x in d)}
result={'runs':rows,'validity_issues':issues,'comparisons':comparisons,'causal_proof':False,'performance_acceptance':False,'scope':'Chunks 2..25. CPU includes CUDA spin; PMU excludes kernel; residual is not GPU busy. No sampling expansion.'}
(root/'host-review.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'validity_issues':issues,'comparisons':comparisons},indent=2))
