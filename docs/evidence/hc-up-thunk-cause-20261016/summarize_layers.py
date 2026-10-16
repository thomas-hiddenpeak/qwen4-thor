"""Offline descriptive summaries, never a post-hoc acceptance rule."""
import json,re
from pathlib import Path
root=Path(__file__).resolve().parent
fields=['embed','lin','full','head','total']
pat=re.compile(r'\[layer-timing\] T=(\d+) '+r' '.join(k+r'=([0-9.]+)ms' for k in fields))
rows={}
for d in sorted((root/'http').iterdir()):
 chunks=[{'T':int(m[0]),**{k:float(v) for k,v in zip(fields,m[1:])}} for m in pat.findall((d/'server.log').read_text()) if int(m[0])>1]
 assert len(chunks)==25 and [c['T'] for c in chunks]==[8192]*24+[8181]
 metric=json.loads((d/'results.json').read_text())[0]['metrics'][0]
 totals={k:sum(c[k] for c in chunks) for k in fields}
 rows[d.name]={'chunks':chunks,'sum_ms':totals,'http_ttft_ms':metric['ttft']*1000,'ttft_minus_event_sum_ms':metric['ttft']*1000-totals['total']}
pairs=[('03-B0','01-A0'),('06-B0','08-A0'),('03-B0','02-B1'),('06-B0','07-B1'),('07-B1','02-B1')]
deltas=[]
for b,a in pairs:
 x,y=rows[b],rows[a]
 deltas.append({'difference':b+' minus '+a,'ttft_ms':x['http_ttft_ms']-y['http_ttft_ms'],'event_sums_ms':{k:x['sum_ms'][k]-y['sum_ms'][k] for k in fields},'outside_events_ms':x['ttft_minus_event_sum_ms']-y['ttft_minus_event_sum_ms']})
result={'scope':'Descriptive only. Event intervals include host starvation and dependencies; groups include all layer operations, not isolated attention. Printed event rounding applies. No changed gate or new requests.','runs':rows,'selected_fixed_identity_deltas':deltas}
(root/'layer-summary.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(deltas,indent=2))
