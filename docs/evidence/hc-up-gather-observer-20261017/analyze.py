import collections,json,re
from pathlib import Path
root=Path(__file__).resolve().parent
read=lambda p:json.loads(p.read_text())
protocol=read(root/'protocol.json');runs={};issues=[]
for i,variant in enumerate(protocol['order'],1):
 name=f'{i:02}-{variant}';d=root/'http'/name;ex=read(d/'exit.json')
 assert ex['server']==0 and ex['completed']==1 and ex['http_output_checks_passed'] and not ex['failure'] and not ex['cleanup_failure']
 ev={e['event']:e for e in map(json.loads,(d/'events.jsonl').read_text().splitlines())}
 lo=ev['client_before']['monotonic_seconds']*1e9;hi=ev['client_after']['monotonic_seconds']*1e9
 gathers=[];layers=[];excluded=[]
 for line_no,line in enumerate((d/'server.log').read_text().splitlines()):
  if line.startswith('[gather-interval]'):
   row={k:int(v) for k,v in (x.split('=') for x in line.split()[1:])};row['line']=line_no
   (gathers if lo<=row['end_wall_ns']<=hi else excluded).append(row)
  match=re.search(r'\[layer-timing\] T=(\d+).*?total=([0-9.]+)ms',line)
  if match and int(match[1])>1:layers.append((line_no,int(match[1]),float(match[2])))
 assert len(gathers)==len(layers)==25 and len({x['tid'] for x in gathers})==1
 assert [x['bytes'] for x in gathers]==[20971520]*24+[20943360]
 assert [x[1] for x in layers]==[8192]*24+[8181]
 intervals=[]
 for j,(g,l) in enumerate(zip(gathers,layers)):
  assert (layers[j-1][0] if j else -1)<g['line']<l[0]
  if j==0:continue
  assert g['prior_sync']==1 and g['io_calls']>0 and g['io_errors']==g['copy_rc']==0
  assert g['io_wall_ns']<=g['wall_ns'] and g['io_cpu_ns']<=g['cpu_ns']+10000
  intervals.append({'chunk':j+1,**g,'remainder_wall_ns':g['wall_ns']-g['io_wall_ns'],'remainder_cpu_ns':g['cpu_ns']-g['io_cpu_ns'],'layer_event_ms':l[2]})
 fields=['wall_ns','cpu_ns','io_wall_ns','io_cpu_ns','remainder_wall_ns','remainder_cpu_ns','layer_event_ms']
 runs[name]={'variant':variant,'excluded_outside_request':excluded,'first_chunk_excluded':gathers[0],'intervals':intervals,'totals':{k:sum(x[k] for x in intervals) for k in fields}}
reference=runs['01-A']['intervals']
for name,run in runs.items():
 if [(x['io_calls'],x['requested_completions']) for x in run['intervals']]!=[(x['io_calls'],x['requested_completions']) for x in reference]:issues.append({'case':name,'reason':'IO calls/completion counts differ'})
lease=read(root/'lease/exit.json')
if lease['status']!='COMPLETED':issues.append({'reason':'guard did not complete'})
comparisons={}
for k in fields:
 a=[runs[n]['totals'][k] for n in ['01-A','04-A']];b=[runs[n]['totals'][k] for n in ['02-B','03-B']];R=max(abs(a[1]-a[0]),abs(b[1]-b[0]));ds=[b[i]-a[i] for i in range(2)]
 comparisons[k]={'paired_B_minus_A':ds,'same_identity_R':R,'resolved_positive_observable':not issues and all(x>R for x in ds)}
result={'runs':runs,'validity_issues':issues,'comparisons':comparisons,'causal_proof':False,'performance_acceptance':False}
(root/'gather-review.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'validity_issues':issues,'comparisons':comparisons},indent=2))
