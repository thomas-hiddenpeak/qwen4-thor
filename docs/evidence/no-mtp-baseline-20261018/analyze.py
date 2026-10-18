"""Offline analysis only; never invokes inference or HTTP."""
from pathlib import Path
import json,hashlib
ROOT=Path(__file__).resolve().parent
lengths=[1024,4096,8192,45056,204800]
load=lambda p:json.loads((ROOT/p).read_text())
lease=load('lease/exit.json');phase=load('phase-exit.json')
assert lease['status']=='COMPLETED' and lease['child_returncode']==0
assert phase['status']=='BASELINE_RECORDED_DESCRIPTIVE' and phase['completed']==['Q','A0','A1']
assert phase['reserved_requests']==41 and phase['server_starts']==3
quality=load('Q/results.json');assert len(quality)==11 and all(r['exact_match'] and r['length_match'] for r in quality)
records=[];per_request=[]
for n in lengths:
    item={'nominal_input':n}
    for name in ['A0','A1']:
        rows=load(f'{name}/context-{n}/responses.json');assert len(rows)==3
        assert all(r['actual_output']==256 and r['finish']==['length'] and r['success'] and r['response_id_valid'] for r in rows)
        t=sum(r['ttft'] for r in rows)/3
        d=sum(r['actual_output']-1 for r in rows)/sum(r['latency']-r['ttft'] for r in rows)
        item[name]={'ttft_seconds':t,'decode_tps':d,'actual_input':rows[0]['actual_input'],'ttft_samples_seconds':[r['ttft'] for r in rows],'decode_samples_tps':[(r['actual_output']-1)/(r['latency']-r['ttft']) for r in rows]}
        for idx,r in enumerate(rows):per_request.append({'service':name,'nominal_input':n,'request_within_tier':idx+1,'service_first_request':n==1024 and idx==0,**r})
    item['ttft_increase_pct']=100*(item['A1']['ttft_seconds']/item['A0']['ttft_seconds']-1)
    item['decode_decrease_pct']=100*(1-item['A1']['decode_tps']/item['A0']['decode_tps'])
    records.append(item)
summary={'status':'BASELINE_RECORDED_DESCRIPTIVE','quality_passed':11,'quality_total':11,'http_requests':41,'server_starts':3,'lease_status':lease['status'],'phase_seconds':phase['end_monotonic']-phase['start_monotonic'],'metrics':records,'uncertainty_intervals':None,'resolution_band':'NOT_ESTABLISHED','b1a':'PAUSED'}
(ROOT/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
(ROOT/'performance-requests.json').write_text(json.dumps(per_request,indent=2)+'\n')
lines=['| 名义档 | 实际输入 | A0 TTFT s | A1 TTFT s | TTFT增加% | A0 decode tok/s | A1 decode tok/s | decode下降% |','|---|---:|---:|---:|---:|---:|---:|---:|']
for r in records:
    a,b=r['A0'],r['A1']
    lines.append(f"| {r['nominal_input']} | {a['actual_input']} | {a['ttft_seconds']:.6f} | {b['ttft_seconds']:.6f} | {r['ttft_increase_pct']:+.4f} | {a['decode_tps']:.6f} | {b['decode_tps']:.6f} | {r['decode_decrease_pct']:+.4f} |")
(ROOT/'performance-table.md').write_text('\n'.join(lines)+'\n')
print(json.dumps(summary,indent=2))
