"""Offline B1b evidence validation. No model execution or new requests."""
import json,sqlite3,hashlib,re
from pathlib import Path
out=Path(__file__).resolve().parent
load=lambda p:json.loads((out/p).read_text())
initial=load('phase-exit.json'); initial_lease=load('lease/exit.json')
phase=load('phase-resume-exit.json'); lease=load('lease-resume-01/exit.json')
assert initial['status']=='STOPPED' and initial_lease['status']=='COMMAND_FAILED'
assert initial['completed']==['quality-on','candidate-on','quality-off','candidate-off']
assert phase['status']=='VALIDATION_COMPLETED_PENDING_REVIEW'
assert phase['reserved_requests']==82 and phase['completed']==['parent-off-r1','parent-on']
assert lease['status']=='COMPLETED' and lease['child_returncode']==0
assert not list((out/'parent-off').rglob('benchmark_data.db'))
assert not (out/'parent-off/server.pid').exists()
completed=initial['completed']+phase['completed']
counts={}; dbs=[]
for name in completed:
    count=0
    for db in sorted((out/name).rglob('benchmark_data.db')):
        with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as c:
            raw=c.execute('select success,prompt_tokens,completion_tokens,first_chunk_latency,latency from result order by start_time').fetchall()
        case=next(p for p in db.parents if (p/'responses.json').exists())
        rows=json.loads((case/'responses.json').read_text())
        assert len(raw)==len(rows)
        for x,y in zip(raw,rows):assert list(x)==[y['success'],y['actual_input'],y['actual_output'],y['ttft'],y['latency']]
        count+=len(rows); dbs.append({'path':str(db.relative_to(out)),'requests':len(rows)})
    assert count==(11 if name.startswith('quality') else 15), (name,count)
    counts[name]=count
    assert load(name+'/exit.json')['http_output_checks_passed']
metrics=[]
for mode in ['on','off']:
    for n in [1024,4096,8192,45056,204800]:
        item={'mode':mode,'nominal_input':n}
        a=load(f"{'parent-off-r1' if mode=='off' else 'parent-on'}/context-{n}/responses.json")
        b=load(f'candidate-{mode}/context-{n}/responses.json')
        for x,y in zip(a,b):
            assert all(x[k]==y[k] for k in ['prompt_sha256','text','actual_input','actual_output','finish'])
            assert x['actual_output']==256 and x['finish']==['length'] and n-48<=x['actual_input']<=n
        for side,rows in [('parent',a),('candidate',b)]:
            item[side]={'ttft_mean_s':sum(x['ttft'] for x in rows)/3,'decode_aggregate_tps':765/sum(x['latency']-x['ttft'] for x in rows),'actual_input':rows[0]['actual_input'],'actual_outputs':[x['actual_output'] for x in rows],'ttft_samples_s':[x['ttft'] for x in rows],'latency_samples_s':[x['latency'] for x in rows],'decode_samples_tps':[255/(x['latency']-x['ttft']) for x in rows],'first_after_load':[n==1024 and i==0 for i in range(3)]}
        item['ttft_increase_pct']=100*(item['candidate']['ttft_mean_s']/item['parent']['ttft_mean_s']-1)
        item['decode_decrease_pct']=100*(1-item['candidate']['decode_aggregate_tps']/item['parent']['decode_aggregate_tps'])
        metrics.append(item)
quality={}
for mode in ['on','off']:
    rows=load(f'quality-{mode}/results.json'); assert len(rows)==11
    assert all(r['exact_match'] and r['length_match'] and r['success']==1 for r in rows)
    quality[mode]={'requests':11,'passed':11}
comparison=load('state-comparison.json'); assert len(comparison)==12
assert all(v['parent']==v['candidate'] for v in comparison.values())
direct={}
for name in ['candidate','parent']:
    text=(out/(name+'-contracts.log')).read_text()
    match=re.search(r'(\d+) tests, (\d+) passed, (\d+) failed, (\d+) skipped',text)
    assert match and match[1]==match[2] and match[3]==match[4]=='0'
    direct[name]=dict(zip(['required','passed','failed','skipped'],map(int,match.groups())))
assert 'MTP lifecycle: PASS' in (out/'lifecycle.log').read_text()
metadata_log=re.sub(r'\[q4t\][^\n]*\n','',(out/'candidate-contracts.log').read_text())
pairs=re.findall(r'metadata max=(\d+) rows=(\d+) steps=2 logits_hidden_kv_raw_comp_bit_exact=1',metadata_log)
assert len(pairs)==56 and {(int(a),int(b)) for a,b in pairs}=={(a,b) for a in [3,4,8191,8192,8194,8195,8196] for b in range(1,9)}
identity=load('identity.json')
# Mutable delivery documentation may grow; inference/test/tool inputs must not.
identity['files'].update(load('direct-identity.json'))
for f,h in identity['files'].items():
    if '/docs/' not in f: assert hashlib.sha256(Path(f).read_bytes()).hexdigest()==h,f
summary={'status':'REVIEW_REQUIRED','calculation':'PASS_WITH_STATED_SCOPE','http_quality':quality,'http_counts':counts,'total_requests':sum(counts.values()),'dbs':dbs,'direct':direct,'state_pairs':12,'metrics':metrics,'statistical_performance':'INSUFFICIENT_EVIDENCE','engineering_acceptance':'PENDING_EXPLICIT_B1B_APPROVAL','resolution_band':None,'uncertainty_interval':None,'exact_token_itl_available':False,'initial_lease':initial_lease,'continuation_lease':lease,'tool_failure':'parent cache missing before server start; zero requests, remaining 30 resumed with actual cache','phase_seconds':phase['end']-phase['start'],'default_binary_sha256':hashlib.sha256((out.parent.parent/'build/q4t').read_bytes()).hexdigest()}
(out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
lines=['| 模式 | 档 | 父TTFT s | 候选TTFT s | TTFT增加% | 父decode | 候选decode | 吞吐下降% |','|---|---:|---:|---:|---:|---:|---:|---:|']
for r in metrics:
    a,b=r['parent'],r['candidate'];lines.append(f"| {r['mode']} | {r['nominal_input']} | {a['ttft_mean_s']:.6f} | {b['ttft_mean_s']:.6f} | {r['ttft_increase_pct']:+.4f} | {a['decode_aggregate_tps']:.6f} | {b['decode_aggregate_tps']:.6f} | {r['decode_decrease_pct']:+.4f} |")
(out/'performance-table.md').write_text('\n'.join(lines)+'\n'); print('\n'.join(lines))
