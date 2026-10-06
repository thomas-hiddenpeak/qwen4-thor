"""Small projection of closed results; no trace replay or new inference."""
from collections import Counter
import hashlib
import json
from pathlib import Path

R = Path(__file__).resolve().parent

def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f, 'sha256').hexdigest()

p = R/'retention-analysis.json'
d = json.loads(p.read_text())
assert d['passed'] and not d['failure']
summary = {k:d[k] for k in ['schema','scope_sha256','manifest_sha256',
    'execution_plan_sha256','model_runs','HTTP_requests','runtime_changes',
    'performance_acceptance','physical_IO_prediction','limits','decision',
    'hypothesis_identified','runtime_candidate_admitted','status',
    'reconstructed_decode_layer_plans','plans_sha256']}
summary['requests'] = []
for q in d['requests']:
    row = {k:q[k] for k in ['group','position','input_tokens','output_tokens',
        'complete','exact_gpu_endpoint_checked','iterator_exhausted',
        'counters','supply','observer_counters']}
    opportunities = [l['opportunities'] for l in q['layers']]
    row['opportunity_totals'] = {k:sum(o[k] for o in opportunities) for k in
        ['plan_count','missing_total','supported_missing_total',
         'potential_candidate_capacity','observed_candidates']}
    row['support_counts'] = {k:sum(o['support_counts'][k] for o in opportunities)
                            for k in opportunities[0]['support_counts']}
    for field in ['latest_prior_victim_plan_gap_histogram',
                  'strictly_intervening_victims_histogram']:
        totals = Counter()
        for o in opportunities:totals.update(o[field])
        row[field] = dict(sorted(totals.items(),key=lambda x:int(x[0])))
    events = [e for o in opportunities for e in o['entry_mirror_first_events']]
    row['entry_mirror_events_by_category'] = {}
    for category in ['gpu_only','l2_only','both','sole']:
        selected = [e for e in events if e['entry_category']==category]
        row['entry_mirror_events_by_category'][category] = dict(
            entries=len(selected),
            first_miss_observed=sum(e['first_gpu_miss_plan'] is not None for e in selected),
            first_eviction_observed=sum(e['first_gpu_eviction_plan'] is not None for e in selected),
            eviction_before_first_miss=sum(e['first_gpu_eviction_plan'] is not None and
                e['first_gpu_miss_plan'] is not None and e['first_gpu_eviction_plan'] <
                e['first_gpu_miss_plan'] for e in selected))
    row['endpoints'] = []
    for e in q['endpoints']:
        row['endpoints'].append(dict(event=e['event'],
            counts={k:sum(l['counts'][k] for l in e['layers'])
                    for k in e['layers'][0]['counts']},
            per_layer_counts=[dict(layer=l['layer'],**l['counts']) for l in e['layers']]))
    summary['requests'].append(row)
summary['comparisons'] = []
for c in d['comparisons']:
    item={k:v for k,v in c.items() if k!='layers'}
    item['layers']=[{k:v for k,v in l.items() if k!='per_plan'} for l in c['layers']]
    item['miss_counts']={k:sum(l['miss_counts'][k] for l in c['layers']) for k in c['layers'][0]['miss_counts']}
    item['capacities']={side:{k:sum(l['capacities'][side][k] for l in c['layers'])
        for k in c['layers'][0]['capacities'][side]} for side in ['S','L']}
    summary['comparisons'].append(item)
summary['source_sha256']={str(p):sha(p),str(Path(__file__)):sha(__file__)}
summary['projection_limits']=['Original report retains per-plan capacities and full entry-expert event rows; this projection retains their aggregates and per-layer intervals only.', 'Endpoint counts include repeated boundary observations and are not independent samples or physical memory totals.']
out=R/'retention-summary.json'
with out.open('x') as f:json.dump(summary,f,ensure_ascii=False,indent=2,allow_nan=False);f.write('\n')
print(json.dumps(dict(summary_sha256=sha(out),bytes=out.stat().st_size)))
for c in summary['comparisons']:
    print(json.dumps({k:c[k] for k in ['position','miss_counts','capacities','common_candidate_difference_interval']}))
