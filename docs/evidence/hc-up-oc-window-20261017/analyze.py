import hashlib,json
from pathlib import Path
OUT=Path(__file__).resolve().parent
p=json.loads((OUT/'protocol.json').read_text());lease=json.loads((OUT/'lease/exit.json').read_text())
assert lease['status']=='COMPLETED' and lease['child_returncode']==0
for path,digest in p['sha256'].items():assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
rows=[]
for v in p['order']:
 d=OUT/'http'/v;c=json.loads((d/'context-204800/run-1/oc/counter.json').read_text());ex=json.loads((d/'exit.json').read_text());result=json.loads((d/'results.json').read_text())
 assert c['complete'] and not c['error'] and len(c['rows'])==2
 assert ex['server']==0 and ex['completed']==1 and ex['http_output_checks_passed'] and ex['diagnostic_oc']
 assert result[0]['outputs']==[p['expected_output']] and result[0]['prompt_sha256']==p['expected_prompt']
 a,b=c['rows'];assert (a['boot_id'],a['device'])==(b['boot_id'],b['device'])
 for i in range(1,4):
  k=f'oc{i}';assert a['values'][k+'_throt_en']==b['values'][k+'_throt_en']
  assert c['delta'][k]==b['values'][k+'_event_cnt']-a['values'][k+'_event_cnt']>=0
 assert all(c['boundary'][k]<=p['max_each_slack_ns'] for k in ('leading_slack_ns','trailing_slack_ns'))
 rows.append({'variant':v,'delta':c['delta'],'boundary':c['boundary'],'enabled_alarms':[f'oc{i}' for i in range(1,4) if a['values'][f'oc{i}_throt_en']],'boot_id':a['boot_id'],'device':a['device']})
assert rows[0]['boot_id']==rows[1]['boot_id'] and rows[0]['device']==rows[1]['device']
events=[json.loads(s) for s in (OUT/'lease/events.jsonl').read_text().splitlines()];monitors=[e for e in events if e['event']=='monitor'];assert monitors and all(not e['conflicts'] for e in monitors)
positive=any(row['delta'][k]>0 for row in rows for k in row['enabled_alarms'])
r={'valid':True,'rows':rows,'guard_monitors':len(monitors),'positive_enabled_OC_delta':positive,'diagnostic_TTFT_ns':{row['variant']:row['boundary']['first_output_ns']-row['boundary']['request_start_ns'] for row in rows},'diagnostic_B_minus_A_TTFT_ns':(rows[1]['boundary']['first_output_ns']-rows[1]['boundary']['request_start_ns'])-(rows[0]['boundary']['first_output_ns']-rows[0]['boundary']['request_start_ns']),'decision':'Observed hardware alarm events; requires separate causal mechanism study, not root cause' if positive else 'Stop OC observation family: neither observed HTTP window contains a counted event; no historical exclusion','performance_acceptance':False,'limits':['Sequential system-wide counter reads include endpoint slack and possibly early decode','No throttle duration or event timestamp; count cannot be converted to TTFT penalty','Original historical requests have no matching OC endpoints']}
(OUT/'analysis.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r,indent=2))
