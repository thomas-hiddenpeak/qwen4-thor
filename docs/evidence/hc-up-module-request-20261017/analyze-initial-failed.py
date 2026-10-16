import hashlib,json,re
from pathlib import Path
OUT=Path(__file__).resolve().parent
p=json.loads((OUT/'protocol.json').read_text())
for path,sha in p['sha256'].items():assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==sha,path
lease=json.loads((OUT/'lease/exit.json').read_text());assert lease['status']=='COMPLETED' and lease['child_returncode']==0
assert json.loads((OUT/'runner-exit.json').read_text())['returncode']==0
exit_record=json.loads((OUT/'http/exit.json').read_text())
assert exit_record['server']==0 and exit_record['http_output_checks_passed']
rows=json.loads((OUT/'http/results.json').read_text())
assert len(rows)==1 and rows[0]['length']==204800 and rows[0]['outputs']==[p['expected_output']]
s=(OUT/'http/server.log').read_text();lines=s.splitlines()
assert s.count('[module-load] enabled=1')==1 and 'INVALID' not in s
pattern=re.compile(r'\[module-load\] sequence=(\d+) module_id=(\d+) context=(\S+) bytes=(\d+) sha256=(\w+)')
records=[]
ready=next(i for i,line in enumerate(lines) if 'serving on port' in line)
request_begin=next(i for i,line in enumerate(lines) if 'phase=request_begin' in line)
request_end=next(i for i,line in enumerate(lines) if 'phase=request_end' in line)
assert ready<request_begin<request_end
for i,line in enumerate(lines):
 m=pattern.fullmatch(line)
 if m:records.append({'line':i+1,'sequence':int(m[1]),'module_id':int(m[2]),'context':m[3],'bytes':int(m[4]),'sha256':m[5],'during_request':request_begin<i<request_end})
assert sorted(r['sequence'] for r in records)==list(range(len(records)))
assert re.findall(r'\[module-load\] complete=(\d+)',s)==[str(len(records))]
static={r['sha256']:r for r in p['static_images']}
known=[r for r in records if r['sha256'] in static]
assert all(r['bytes']==static[r['sha256']]['bytes'] for r in known)
coverage=any(r['during_request'] for r in known)
hc=[r for r in records if r['sha256']==p['HC_unique_sha256']]
report={'validity_passed':True,'module_records':len(records),'known_application_records':len(known),'positive_request_coverage':coverage,'HC_image_observed':bool(hc),'HC_records':hc,'records':records,'http_output':p['expected_output'],'performance_acceptance':False,'root_cause_proven':False,'decision':('inconclusive: no known application module observed during request' if not coverage else 'HC image appeared; loading alone is not causality' if hc else 'stop extra HC image-loading explanation under observed off path'),'limits':p['limits']}
(OUT/'review.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='records'},indent=2))
