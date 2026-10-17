import json,statistics,hashlib,re
from pathlib import Path
out=Path(__file__).resolve().parent
prior=out.parent/'mtp-numerical-closure-20261016/http'
summary={'performance_verdict':'INSUFFICIENT_EVIDENCE','resolution_bands':None,'difference_uncertainty_intervals':None,'reason':'No established comparable measurement resolution bands; historical timings are descriptive only. No extra sampling.','groups':{}}
lines=['| 模式 | 档位 | 历史TTFT s | 候选TTFT s | 历史decode tok/s | 候选decode tok/s |','|---|---:|---:|---:|---:|---:|']
for mode in ['on','off']:
    old=json.loads((prior/mode/'results.json').read_text())
    new=json.loads((out/mode/'results.json').read_text())
    rows=[]
    for a,b in zip(old,new):
        assert a['length']==b['length'] and a['prompt_sha256']==b['prompt_sha256'] and a['outputs']==b['outputs']
        def metrics(row):
            ts=[x['ttft'] for x in row['metrics']]
            ds=[x['decode_tps'] for x in row['metrics']]
            return {'ttft_mean_s':statistics.mean(ts),'ttft_samples_s':ts,'decode_aggregate_tps':len(ds)/sum(1/x for x in ds),'decode_samples_tps':ds}
        am,bm=metrics(a),metrics(b)
        responses=json.loads((out/mode/f"context-{b['length']}"/'responses.json').read_text())
        assert len(responses)==3 and all(r['success']==1 and r['actual_output']==256 and r['finish']==['length'] and b['length']-48<=r['actual_input']<=b['length'] for r in responses)
        bm['actual_input_tokens']=[r['actual_input'] for r in responses]
        bm['actual_output_tokens']=[r['actual_output'] for r in responses]
        bm['first_after_server_load']=[b['length']==1024 and i==0 for i in range(3)]
        bm['latency_samples_s']=[r['latency'] for r in responses]
        bm['exact_token_itl_available']=False
        rows.append({'nominal_length':b['length'],'outputs_identical':True,'baseline_historical':am,'candidate':bm,'ttft_slowdown_pct':100*(bm['ttft_mean_s']/am['ttft_mean_s']-1),'decode_slowdown_pct':100*(1-bm['decode_aggregate_tps']/am['decode_aggregate_tps']),'verdict':'INSUFFICIENT_EVIDENCE'})
        lines.append(f"| {mode} | {b['length']} | {am['ttft_mean_s']:.3f} | {bm['ttft_mean_s']:.3f} | {am['decode_aggregate_tps']:.3f} | {bm['decode_aggregate_tps']:.3f} |")
    summary['groups'][mode]=rows
for mode in ['quality-on','quality-off']:
    rows=json.loads((out/mode/'results.json').read_text())
    summary[mode]={'count':len(rows),'passed':all(r['exact_match'] and r['length_match'] and r['success']==1 for r in rows)}
log=(out/'contracts.log').read_text()
assert '9 tests, 9 passed, 0 failed, 0 skipped' in log
assert len(re.findall(r'^metadata max=',log,re.M))==56
summary['direct_contracts']={'required':9,'passed':9,'failed':0,'skipped':0,'position_shape_pairs':56,'consecutive_forwards_per_pair':2,'accepted_draft_counts_observed':[0,1,3],'k':[1,3]}
summary['boundaries']=json.loads((out/'boundaries/exit.json').read_text())
assert summary['boundaries']['passed'] and summary['boundaries']['server_returncode']==0
assert len(json.loads((out/'boundaries/responses.json').read_text()))==6
assert json.loads((out/'boundaries/cancellation.json').read_text())['passed']
summary['lease']=json.loads((out/'lease/exit.json').read_text())
assert summary['lease']['status']=='COMPLETED' and summary['lease']['child_returncode']==0
summary['http_budget']={'generation_requests':59,'cancel_control_requests':1,'total':60,'extra_diagnostic_requests':0}
summary['production_binary_preserved']=hashlib.sha256((out.parent.parent/'build/q4t').read_bytes()).hexdigest()
for name in ['build.log','build-contracts-post-http.log']:
    assert 'warning:' not in (out/name).read_text().lower()
(out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
(out/'performance-table.md').write_text('\n'.join(lines)+'\n')
