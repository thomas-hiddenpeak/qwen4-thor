"""Read-only paired metrics; never starts inference or enlarges a stage."""
import argparse
import json
import math
from pathlib import Path
import statistics


def metric(case):
    rows = case['rows']
    durations = [r['latency'] - r['ttft'] for r in rows]
    if any(t <= 0 for t in durations): raise ValueError('invalid decode duration')
    return dict(e2e=case['throughput'],
        decode=sum(r['output_tokens'] - 1 for r in rows) / sum(durations),
        ttft_mean=statistics.mean(r['ttft'] for r in rows),
        ttft_max=max(r['ttft'] for r in rows), makespan=case['makespan_seconds'],
        total_output=case['total_output_tokens'],
        outputs=sorted((r['prompt_sha256'], r['output_sha256']) for r in rows))


def compare(off, on):
    a, b = metric(off), metric(on)
    return dict(off=a, on=b, e2e_percent=100 * (b['e2e']/a['e2e']-1),
        decode_percent=100 * (b['decode']/a['decode']-1),
        ttft_percent=100 * (b['ttft_mean']/a['ttft_mean']-1),
        output_same=a['outputs']==b['outputs'])


def analyze(plan):
    base = Path(plan['output'])
    ready, missing = {}, []
    for step in plan['steps']:
        if not step['http_loads']: continue
        path = base / step['name'] / 'results.json'
        if path.exists(): ready[step['name']] = {r['id']:r for r in json.loads(path.read_text())}
        else: missing.append(step['name'])
    pairs, protections = [], []
    for block in [1,2]:
        for head in ['full','subset']:
            steps = {s['suffix']:s for s in plan['steps'] if s.get('block')==block and s.get('head')==head}
            for scenario in ['repeat-code','repeat-text','mixed','nonrepeat']:
                a, b = ready.get(steps[False]['name'],{}), ready.get(steps[True]['name'],{})
                if scenario in a and scenario in b:
                    pairs.append(dict(head=head,block=block,scenario=scenario,**compare(a[scenario],b[scenario])))
    for head in ['full','subset','plain']:
        steps = {s['binary']==plan['candidate']:s for s in plan['steps'] if s.get('head')==head and s.get('layout')=='single'}
        a, b = ready.get(steps[False]['name'],{}), ready.get(steps[True]['name'],{})
        for name in sorted(a.keys() & b.keys()):
            protections.append(dict(head=head,scenario=name,**compare(a[name],b[name])))
    gains = []
    for head in ['full','subset']:
        for scenario in ['repeat-code','repeat-text']:
            rows=[r for r in pairs if r['head']==head and r['scenario']==scenario]
            if not rows: continue
            e2e=math.exp(statistics.mean(math.log(1+r['e2e_percent']/100) for r in rows))-1
            decode=math.exp(statistics.mean(math.log(1+r['decode_percent']/100) for r in rows))-1
            gains.append(dict(head=head,scenario=scenario,blocks=len(rows),e2e_percent=e2e*100,
                decode_percent=decode*100,threshold_met=len(rows)==2 and e2e>=.01 and decode>=.03,
                output_same=all(r['output_same'] for r in rows)))
    result=dict(stage_state=json.loads((base/'state.json').read_text()),missing_services=missing,
        paired=pairs,single_protections=protections,repeated_gains=gains,
        metric_definition='E2E total output / group makespan; decode sum(output-1) / sum(latency-TTFT); TTFT includes queue; descriptive paired results only')
    telemetry=base/'unified-memory.jsonl'
    if telemetry.exists():
        peak={};count=0
        for line in telemetry.open():
            row=json.loads(line);count+=1
            # Preserve raw telemetry schema; RAM is reviewed from its physical
            # MemTotal/Available/Free/cache fields, never RSS+managed double sum.
            for k,v in row.items():
                if isinstance(v,(int,float)) and ('bytes' in k or 'rss' in k):peak[k]=max(peak.get(k,0),v)
        result['telemetry']=dict(samples=count,top_level_peaks=peak)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with args.output.open('x') as f:json.dump(analyze(json.loads(args.plan.read_text())),f,indent=2);f.write('\n')
