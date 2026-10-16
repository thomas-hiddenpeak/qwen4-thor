"""Read completed per-request files after guard shutdown race; never rerun."""
from pathlib import Path
import hashlib,json,collections
root=Path(__file__).resolve().parent
read=lambda p:json.loads(p.read_text())
lease=read(root/'lease/exit.json');assert lease['status']=='ENV_ABORT'
events=[json.loads(x) for x in (root/'lease/events.jsonl').read_text().splitlines()]
conflicts=[e for e in events if e.get('conflicts')];assert len(conflicts)==1
last=root/'http/06-A';pid=int((last/'server.pid').read_text())
assert conflicts[0]['conflicts']==[{'pid':pid,'nvidia_compute_context':True}]
last_events={e['event']:e for e in map(json.loads,(last/'events.jsonl').read_text().splitlines())}
assert last_events['client_after']['monotonic_seconds']<conflicts[0]['monotonic']<last_events['server_after_shutdown']['monotonic_seconds']
rows=[];checks={};ident=read(root/'derivation.json');manifest={}
for d in sorted((root/'http').iterdir()):
 v=read(d/'exit.json');assert v['server']==0 and v['completed']==1 and v['http_output_checks_passed'] and not v['failure'] and not v['cleanup_failure'];checks[d.name]=v
 r=read(d/'results.json')[0];variant=d.name.split('-')[1]
 assert r['outputs']==['57f3581ddd0a090715729d11e1374a85afdd6b62b648e700cda1e96eb8a4c8ca']
 assert r['prompt_sha256']=='f9156ff021e47b8cfeeb2e18e7b307608acec209095a8c8d991c12408153aeb4'
 assert hashlib.sha256((root/variant/'q4t').read_bytes()).hexdigest()==ident['variants'][variant]['sha256']
 rows.append({'name':d.name,'variant':variant,'binary_sha256':ident['variants'][variant]['sha256'],'metrics':r['metrics'][0],'output':r['outputs'][0],'prompt':r['prompt_sha256']})
 for n in ['server.log','results.json','exit.json','events.jsonl','server-command.json']:
  p=d/n;manifest[str(p)]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
assert [r['variant'] for r in rows]==read(root/'protocol.json')['order']
values={v:[r['metrics']['ttft'] for r in rows if r['variant']==v] for v in ['A','B','C']};ranges={v:abs(x[1]-x[0]) for v,x in values.items()};R=max(ranges.values());blocks=[]
for k in range(2):
 v={x:values[x][k] for x in values};g=v['B']-v['A'];effect=v['B']-v['C'];res=v['C']-v['A'];blocks.append({'original_gap_s':g,'registration_effect_s':effect,'residual_s':res,'support_rule':g>0 and effect>R and abs(res)<=R})
review={'same_identity_ranges_s':ranges,'R_s':R,'blocks':blocks,'mechanistic_support':False,'performance_acceptance':False,'phase_status':'ENV_ABORT preserved; offline reconstruction of six completed HTTP files','scope':'No completed-phase claim, no retry. Frozen numerical criterion also fails independently of guard failure.'}
for n in ['telemetry.jsonl','lease/events.jsonl','lease/exit.json','driver.log']:
 p=root/n;manifest[str(p)]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
for n,obj in [('recovered-observations.json',rows),('recovered-review.json',review),('completion-audit.json',{'http_exits':checks,'guard_status':lease,'reported_conflict':conflicts,'last_request_events':last_events,'lease_event_counts':dict(collections.Counter(e['event'] for e in events))}),('raw-manifest.json',manifest)]:
 (root/n).write_text(json.dumps(obj,indent=2)+'\n')
print(json.dumps(review,indent=2))
