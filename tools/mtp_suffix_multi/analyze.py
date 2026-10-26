"""Read-only F review; no new model requests or inferred performance margin."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics


def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    plan=json.loads(args.plan.read_text());root=Path(plan['output'])
    state=json.loads((root/'state.json').read_text())
    post=json.loads((args.plan.parent/'postflight.json').read_text())
    summary=json.loads((root/'telemetry-summary.json').read_text())
    services={}; comparisons=[]
    for s in plan['steps']:
        if s['kind']!='block':continue
        out=root/s['name']
        if not (out/'multi-audit.json').exists():continue
        audit=json.loads((out/'multi-audit.json').read_text())
        cases={}
        for r in json.loads((out/'results.json').read_text()):
            rows=r['rows'];suffix={v['request']:v for v in audit['suffix']}
            ends={v['id']:v for v in audit['lifecycle']}
            cases[r['id']]=dict(throughput=r['throughput'],makespan=r['makespan_seconds'],
                count=len(rows),total_output_tokens=r['total_output_tokens'],
                ttft_ms=[v['ttft']*1000 for v in rows],
                decode_tps=[(v['output_tokens']-1)/(v['latency']-v['ttft']) for v in rows],
                queue_ms=[float(ends[v['response_id']]['slot_wait_ms']) for v in rows],
                outputs=[dict(prompt=v['prompt_sha256'],output=v['output_sha256'],text=v['text']) for v in rows],
                suffix=[suffix[v['response_id']] for v in rows if v['response_id'] in suffix])
        services[s['name']]=dict(head=s['head'],suffix=s['suffix'],cases=cases,
            batches=audit['batches'],cache=audit['cache'])
    for head in ['full','subset']:
        on=next((v for v in services.values() if v['head']==head and v['suffix']),None)
        off=next((v for v in services.values() if v['head']==head and not v['suffix']),None)
        if on is None or off is None:continue
        for name in ['repeat-code','repeat-text','mixed','nonrepeat']:
            a,b=on['cases'][name],off['cases'][name]
            oa=sorted((v['prompt'],v['output']) for v in a['outputs'])
            ob=sorted((v['prompt'],v['output']) for v in b['outputs'])
            comparisons.append(dict(head=head,case=name,
                throughput_off=b['throughput'],throughput_on=a['throughput'],
                throughput_change_percent=100*(a['throughput']/b['throughput']-1),
                output_multisets_equal=oa==ob,
                median_ttft_change_ms=statistics.median(a['ttft_ms'])-statistics.median(b['ttft_ms']),
                median_decode_change_percent=100*(statistics.median(a['decode_tps'])/
                    statistics.median(b['decode_tps'])-1)))
    valid=(state['status']=='MEASURED_REQUIRES_REVIEW' and
           post['status']=='MEASURED_REQUIRES_REVIEW' and
           summary['sample_attempts']==summary['flushed_samples'] and
           summary['unflushed_attempts']==0 and not summary['failures_at_exit'])
    goals=[c for c in comparisons if c['case'].startswith('repeat-')]
    complete=len(comparisons)==8 and state['requests_completed']==136
    go=valid and complete and len(goals)==4 and all(
        c['throughput_change_percent']>0 and c['output_multisets_equal'] for c in goals)
    # Unified physical memory gauges overlap with CUDA/RSS. Keep raw gauges;
    # never add independent peaks or claim service attribution for system RAM.
    gauges={}
    with (root/'unified-memory.jsonl').open() as f:
        for line in f:
            r=json.loads(line)
            for key,number in re.findall(r'^(\w+):\s+(\d+) kB$',
                                          r['meminfo'], re.MULTILINE):
                value=int(number)*1024
                g=gauges.setdefault(key,dict(min=value,max=value))
                g['min']=min(g['min'],value);g['max']=max(g['max'],value)
    result=dict(plan_sha256=sha(args.plan),candidate_sha256=plan['candidate_sha256'],
        evidence_valid=valid,measurement_complete=complete,
        statistics='descriptive one startup pair/head; no population or noninferiority claim',
        decision='CONTINUE_TO_FROZEN_B' if go else 'STOP_REQUIRES_REVIEW',
        performance_acceptance=False,comparisons=comparisons,services=services,
        meminfo_raw_gauges=gauges,telemetry=summary,
        limits=['F is feasibility, not final acceptance',
                '32 incoming requests queued into four active slots',
                'No claim that arbitrary outputs match plain decode or other batch shapes'])
    with args.output.open('x') as f:json.dump(result,f,indent=2,ensure_ascii=False);f.write('\n')
    print(json.dumps(dict(decision=result['decision'],valid=valid,complete=complete,
        comparisons=comparisons),ensure_ascii=False))

if __name__=='__main__':main()
