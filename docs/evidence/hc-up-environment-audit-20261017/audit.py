import json,hashlib
from pathlib import Path
base=Path('.q4t-work'); rows=[]; sources={}
def load(p):
 sources[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
 return [json.loads(x) for x in p.read_text().splitlines()]
def counters(s):return {a:int(b) for a,b in (x.split() for x in s.splitlines())}
for phase in ['hc-up-host-observer-20261016','hc-up-gather-observer-20261017','hc-up-subtiming-20261017-v2']:
 r=base/phase
 telemetry=[]
 for f in r.glob('telemetry*.jsonl'):telemetry+=load(f)
 telemetry.sort(key=lambda x:x['monotonic'])
 for d in sorted((r/'http').iterdir()):
  events=load(d/'events.jsonl');ev={x['event']:x for x in events}
  start=ev['client_before']['monotonic_seconds'];end=ev['case_completed']['monotonic_seconds']
  t=[x for x in telemetry if start<=x['monotonic']<=end]
  assert len(t)>2
  first,last=t[0],t[-1];a=counters(first['values']['/proc/vmstat']);b=counters(last['values']['/proc/vmstat'])
  keys=['pgmajfault','pswpin','pswpout','pgscan_direct','pgscan_kswapd','compact_stall','compact_success','workingset_refault_file','pgpgin']
  freq={k:sorted({int(x['values'][k]) for x in t}) for k in first['values'] if 'cur_freq' in k}
  temp={k:[min(int(x['values'][k]) for x in t),max(int(x['values'][k]) for x in t)] for k in first['values'] if k.endswith('/temp')}
  rows.append({'phase':phase,'case':d.name,'samples':len(t),'window_s':end-start,'unobserved_edges_s':[first['monotonic']-start,end-last['monotonic']],'system_vmstat_delta':{k:b[k]-a[k] for k in keys},'frequency_values':freq,'temperature_millidegree_range':temp,'missing_observations':[k for k,v in first['values'].items() if isinstance(v,dict)]})
result={'scope':'Existing 5-second system telemetry within full HTTP request (prefill+decode), not per-process attribution or causal evidence; edges excluded; no interpolation','runs':rows,'source_sha256':sources}
out=base/'hc-up-environment-audit-20261017/result.json';out.write_text(json.dumps(result,indent=2)+'\n')
for r in rows:print(r['phase'],r['case'],r['system_vmstat_delta'],{k:v for k,v in r['frequency_values'].items() if len(v)>1})
