"""Offline AB60 audit and fixed descriptive statistics; no inference."""
from pathlib import Path
import json,sqlite3
R=Path(__file__).resolve().parent
load=lambda p:json.loads((R/p).read_text())
lease=load('lease/exit.json');phase=load('phase-exit.json')
assert lease['status']=='COMPLETED' and lease['child_returncode']==0
assert phase['status']=='AB_OBSERVATIONS_RECORDED'
assert phase['completed']==['A-off','B-off','B-on','A-on']
assert phase['reserved_requests']==60 and phase['server_starts']==4
counts={};dbs=[]
for stage in phase['completed']:
    count=0
    for db in sorted((R/stage).rglob('benchmark_data.db')):
        with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as c:
            raw=c.execute('select success,prompt_tokens,completion_tokens,first_chunk_latency,latency from result order by start_time').fetchall()
        case=next(p for p in db.parents if (p/'responses.json').exists())
        rows=json.loads((case/'responses.json').read_text());assert len(raw)==len(rows)==3
        for x,y in zip(raw,rows):assert list(x)==[y['success'],y['actual_input'],y['actual_output'],y['ttft'],y['latency']]
        count+=len(rows);dbs.append({'path':str(db),'requests':len(rows)})
    assert count==15;counts[stage]=count
    assert load(stage+'/exit.json')['http_output_checks_passed']
    for side in ['before','after']:assert load(stage+'-'+side+'-identity.json')['status']=='PASS'
events=[json.loads(x) for x in (R/'lease/events.jsonl').read_text().splitlines()]
assert sum(x['event']=='lease_acquired' for x in events)==1
assert sum(x['event']=='lease_released' for x in events)==1
assert not any(x.get('conflicts') for x in events)
start=next(x for x in events if x['event']=='lease_acquired');end=next(x for x in events if x['event']=='lease_released')
assert end['monotonic']-start['monotonic']<14400
metrics=[]
for mode in ['off','on']:
    for n in [1024,4096,8192,45056,204800]:
        item={'mode':mode,'nominal_input':n}
        a,b=[load(f'{side}-{mode}/context-{n}/responses.json') for side in ['A','B']]
        for x,y in zip(a,b):
            assert all(x[k]==y[k] for k in ['prompt_sha256','text','actual_input','actual_output','finish'])
            assert x['actual_output']==256 and x['finish']==['length']
        for side,rows in [('A',a),('B',b)]:
            item[side]={'T':sum(x['ttft'] for x in rows)/3,'D':765/sum(x['latency']-x['ttft'] for x in rows),'input':rows[0]['actual_input'],'ttft_samples':[x['ttft'] for x in rows],'decode_samples':[255/(x['latency']-x['ttft']) for x in rows]}
        item['ttft_increase_pct']=100*(item['B']['T']/item['A']['T']-1)
        item['decode_decrease_pct']=100*(1-item['B']['D']/item['A']['D'])
        metrics.append(item)
summary={'status':'AB_OBSERVATIONS_RECORDED','requests':60,'counts':counts,'starts':4,'phase_seconds':phase['end_monotonic']-phase['start_monotonic'],'lease_start_utc':start['utc'],'lease_end_utc':end['utc'],'lease_seconds':end['monotonic']-start['monotonic'],'monitors':sum(x['event']=='monitor' for x in events),'dbs':dbs,'metrics':metrics,'quality':'reuse prior qualified fixtures; no new quality requests','resolution_band':None,'uncertainty_interval':None}
(R/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
lines=['| 模式 | 档 | A TTFT s | B TTFT s | TTFT增加% | A decode | B decode | 吞吐下降% |','|---|---:|---:|---:|---:|---:|---:|---:|']
for r in metrics:
    a,b=r['A'],r['B'];lines.append(f"| {r['mode']} | {r['nominal_input']} | {a['T']:.6f} | {b['T']:.6f} | {r['ttft_increase_pct']:+.4f} | {a['D']:.6f} | {b['D']:.6f} | {r['decode_decrease_pct']:+.4f} |")
(R/'performance-table.md').write_text('\n'.join(lines)+'\n');print('\n'.join(lines))
