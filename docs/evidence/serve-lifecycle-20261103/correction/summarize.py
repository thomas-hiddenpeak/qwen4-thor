"""Offline aggregation: preserve original failure, validate every actual wire."""
from collections import Counter
import io,json,re,sys
from pathlib import Path
ROOT=next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists());sys.path.insert(0,str(ROOT))
from tools.serve_lifecycle import repair as r,stage,verify,observe
from tools.serve_pressure.observe import status_fields
from chat_client import read_json,read_sse,RemoteError
from jsonschema import Draft202012Validator
h=r.h
for directory in (stage.PACKAGE,stage.OUTPUT,r.PACKAGE,r.OUTPUT):h.verify_seal(directory)
for directory in ('business','health'):
 h.require(h.read(r.OUTPUT/directory/'result.json')['status']=='PASS_BOUNDED_LIFECYCLE_REPAIR','repair gate')
h.require(h.read(r.OUTPUT/'lease/exit.json')['status']=='COMPLETED','repair guard')
validator=Draft202012Validator(h.read(ROOT/'docs/serve-transport-v1.schema.json'))
wires=[]
def success(path):
 raw=path.read_bytes()
 if raw.startswith((b'data:',b':')):
  payloads=[line[6:] for line in raw.splitlines() if line.startswith(b'data: ')]
  h.require(payloads[-1]==b'[DONE]' and payloads.count(b'[DONE]')==1,'success terminal')
  values=[json.loads(line) for line in payloads[:-1]];read_sse(io.BytesIO(raw))
 else:values=[json.loads(raw)];read_json(io.BytesIO(raw))
 for value in values:validator.validate(value)
 h.require(all('error' not in v for v in values),'error claimed as success')
 return dict(file=h.identity(path),envelopes=len(values),complete=True,success=True)
for cycle in sorted((stage.OUTPUT/'business').glob('cycle-*')):
 paths=sorted(cycle.glob('*-wire.bin'));h.require(len(paths)==5,'cycle raw wire inventory')
 wires.extend(success(path) for path in paths)
h.require(len(wires)==1200,'hour success wire inventory')
for label,code in [('active','server_shutdown'),('queued','service_unavailable')]:
 path=stage.OUTPUT/f'business/graceful-inflight/{label}-wire.bin';raw=path.read_bytes()
 values=[json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b'data: {')]
 for value in values:validator.validate(value)
 h.require(values[-1]['error']['code']==code and raw.count(b'data: [DONE]')==1 and raw.rstrip().endswith(b'data: [DONE]'),'original failed wire changed')
 try:read_sse(io.BytesIO(raw))
 except RemoteError as error:h.require(error.code==code,'original client classification')
 else:raise RuntimeError('original failed call became success')
 wires.append(dict(file=h.identity(path),envelopes=len(values),complete=True,success=False,
                   error_code=code,original_failure=True,accepted_shutdown=(code=='server_shutdown')))
for directory in ('business','health'):
 recorded=h.read(r.OUTPUT/directory/'wire-schema-result.json')
 for record in recorded['complete_records']:
  path=h.check_identity(record['file'])
  if record['success']:value=success(path)
  else:
   value=verify.terminal(path.read_bytes(),graceful=True,validator=validator)
   value.update(file=h.identity(path),complete=True)
  h.require(value['envelopes']==record['envelopes'],'schema record drift');wires.append(value)
 for record in recorded['interrupted_records']:
  if record['outcome']!='REQUEST_OUTCOME_UNKNOWN':continue
  path=h.check_identity(record['file']);value=verify.terminal(path.read_bytes(),graceful=False,validator=validator)
  h.require(value['envelopes']==record['envelopes'],'crash schema record drift')
  value.update(file=h.identity(path),complete=False);wires.append(value)
negative=[]
for path in sorted((r.OUTPUT/'business').glob('after-*/old-*-cancel-response.json')):
 value=h.read(path);body=json.loads(value['raw']);validator.validate(body)
 h.require(value['status']==404 and body['error']['code']=='request_not_found','old request ID')
 negative.append(h.identity(path))
h.require(len(wires)==1218 and len(negative)==4 and sum(row['success'] for row in wires)==1212,'cumulative wire counts')
schema=dict(passed=True,generation_wires=len(wires),complete_successes=1212,
 original_failed_attempts=2,corrected_graceful_errors=2,unknown_crash_prefixes=2,
 envelopes=sum(row['envelopes'] for row in wires),negative_responses=4,records=wires,negatives=negative,no_new_http_or_model_requests=True)
h.save(r.BASE/'offline-schema.json',schema)
resources,instances,diagnostics={},{},{}
for prefix,directory in [('original',stage.OUTPUT/'business'),('correction',r.OUTPUT/'business'),('installed',r.OUTPUT/'health')]:
 for folder in sorted(directory.glob('model-*')):
  key=prefix+'/'+folder.name;maximum=dict(parent_rss=0,parent_fds=0,parent_threads=0,memcg_current=0,memcg_peak=0,memcg_anon=0,observed_child_rss=0)
  count=worker_samples=unknown_rss=0;minimum_available=None;first=last=None
  for line in (folder/'continuous.jsonl').open():
   row=json.loads(line);h.require('observer_error' not in row,'observer error');count+=1
   first=row['started'] if first is None else first;last=row['ended'];obs=row['resources']['cgroup']['observations']
   observe.prior.check_oom(obs['memory.events']['value'])
   values=dict(parent_rss=row['parent']['VmRSS'],parent_fds=row['parent']['fds'],parent_threads=row['parent']['Threads'],
    memcg_current=obs['memory.current']['value'],memcg_peak=obs['memory.peak']['value'],memcg_anon=obs['memory.stat']['value']['anon'])
   for name,value in values.items():maximum[name]=max(maximum[name],value)
   available=row['memory']['MemAvailable'];minimum_available=available if minimum_available is None else min(minimum_available,available)
   for pid,status in row['process_memory']['processes'].items():
    if pid==str(row['resources']['binding']['root_pid']):continue
    worker_samples+=1;rss=status_fields(status).get('VmRSS')
    if rss is None:unknown_rss+=1
    else:maximum['observed_child_rss']=max(maximum['observed_child_rss'],rss)
  resources[key]=dict(samples=count,span_seconds=last-first,maximum_separate_observations=maximum,
    minimum_global_MemAvailable=minimum_available,child_observations=worker_samples,child_rss_missing=unknown_rss,
    observations_not_additive=True,physical_ram_peak_proved=False)
  log=(folder/'server.log').read_text();ended=[s for s in log.splitlines() if s.startswith('[q4t][request] ') and 'event=end ' in s]
  reaped=re.findall(r'grammar worker reaped=(\d+) ',log);h.require(all(v=='1' for v in reaped),'worker reap')
  instances[key]=dict(command=h.read(folder/'command.json'),identity=h.read(folder/'scope/identity.json'),
    shutdown=h.read(folder/'shutdown.json'),parent_reaped_workers=len(reaped),
    request_end_outcomes=dict(Counter(re.search(r'outcome=(\S+)',s)[1] for s in ended)),
    request_end_reasons=dict(Counter(re.search(r'reason=(\S+)',s)[1] for s in ended)))
 for folder in sorted((directory/'diagnostics').iterdir()):
  value=verify.bundle.read(folder);diagnostics[prefix+'/'+folder.name]=dict(state=value['state'],
    version=value['observability_version'],identity=value['identity'],comparison=value['comparison'])
guards={name:h.read(out/'lease/exit.json') for name,out in [('original',stage.OUTPUT),('correction',r.OUTPUT)]}
conflicts=[]
for name,out in [('original',stage.OUTPUT),('correction',r.OUTPUT)]:
 for line in (out/'lease/events.jsonl').read_text().splitlines():
  row=json.loads(line)
  if row.get('conflicts') or row['event']=='ENV_ABORT':conflicts.append(dict(phase=name,event=row))
h.require(not conflicts,'environment abort cannot become pass')
summary=dict(status='ACCEPT_BOUNDED_LIFECYCLE_WITH_TARGETED_SHUTDOWN_REPAIR',
 original_source_commit=h.read(stage.PACKAGE/'plan.json')['source_commit'],
 repair_source_commit=h.read(r.PACKAGE/'plan.json')['source_commit'],
 original_failed_result=h.read(stage.OUTPUT/'business/result.json'),
 correction_business=h.read(r.OUTPUT/'business/result.json'),installed_health=h.read(r.OUTPUT/'health/result.json'),
 finalized=h.read(r.OUTPUT/'finalized.json'),guards=guards,environment_aborts=conflicts,
 cpu=dict(host=242,required=192,driver=24,native=24,initial_drift_failures_retained=True),
 runtime_sources=199,changed_runtime=['src/server/chat_admission.cpp'],reuse=h.read(r.PACKAGE/'reuse.json'),
 soak=h.read(stage.OUTPUT/'business/soak-result.json'),envelope=h.read(stage.OUTPUT/'business/resource-envelope.json'),
 instances=instances,diagnostics=diagnostics,resource_observations=resources,
 wire_schema={key:value for key,value in schema.items() if key not in ('records','negatives')},
 graceful_children=h.read(r.OUTPUT/'business/graceful-inflight/child-exit-observations.json'),
 crash_children=h.read(r.OUTPUT/'business/crash-inflight/child-exit-observations.json'),
 crash_recovery={name:h.read(r.OUTPUT/f'business/crash-inflight/{name}-recovery.json') for name in ('active','queued')},
 postflight=h.read(r.BASE/'postflight.json'),raw_records_are_not_redacted=True,goal_complete=False,
 original_elapsed_to_finalization=h.read(r.OUTPUT/'finalized.json')['time']-h.read(stage.OUTPUT/'begin.json')['started'])
h.save(r.BASE/'acceptance-summary.json',summary)
print(json.dumps(dict(status=summary['status'],counts=summary['installed_health']['counts'],seconds=summary['original_elapsed_to_finalization'],wires=len(wires),envelopes=schema['envelopes'],samples={k:v['samples'] for k,v in resources.items()})))
