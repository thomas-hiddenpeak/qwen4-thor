"""Offline phase gates and descriptive paired results; never runs inference."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'.q4t-work/prefix-cache-20261028'
def read(path):return json.loads(path.read_text())
def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(path,value):path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['a','b','c','d']);args=p.parse_args()
    phase=BASE/('phase-'+args.phase);plan=read(phase/'plan.json')
    state=read(phase/'run/state.json');post=read(phase/'postflight.json');guard=read(phase/'lease/exit.json')
    if state['status']!='MEASURED_REQUIRES_REVIEW' or post['status']!=state['status']:
        raise RuntimeError('stage not complete/valid')
    if guard.get('status')!='COMPLETED':raise RuntimeError('outer lease not complete: '+repr(guard))
    gate=dict(passed=True,candidate_sha256=plan['candidate_sha256'],phase=args.phase,
        requests=plan['http_requests'],starts=plan['http_loads'],direct_contracts=2 if args.phase=='b' else 0,
        state_sha256=sha(phase/'run/state.json'),postflight_sha256=sha(phase/'postflight.json'),
        guard_sha256=sha(phase/'lease/exit.json'),statistics='DESCRIPTIVE_ONLY',results=[])
    if args.phase=='a':
        for step in plan['steps']:
            folder=phase/'run'/step['name']
            gate['results'].append(dict(head=step['head'],lifecycle=read(folder/'lifecycle.json'),
                http_quality=read(folder/'first-http-gate.json')['passed']))
        if 'PASS shared_checks=' not in (phase/'run/00-full-on/cpu-contract.log').read_text():
            raise RuntimeError('CPU storage contract')
    elif args.phase=='b':
        for step in plan['steps']:
            log=(phase/'run'/(step['name']+'-driver.log')).read_text()
            if 'PASS prefix_state=1' not in log:raise RuntimeError('independent prefix state contract')
            gate['results'].append(dict(head=step['head'],passed=True,log_sha256=sha(phase/'run'/(step['name']+'-driver.log'))))
    elif args.phase=='c':
        arms={(s['head'],s['shared']):read(phase/'run'/s['name']/'results.json') for s in plan['steps']}
        for head in ['full','subset']:
            for off,on in zip(arms[head,False],arms[head,True]):
                if off['id']!=on['id']:raise RuntimeError('case identity')
                same=[a['output_sha256']==b['output_sha256'] for a,b in zip(off['rows'],on['rows'])]
                equal_length=all(a['output_tokens']==b['output_tokens']==256 for a,b in zip(off['rows'],on['rows']))
                reduction=1-on['makespan']/off['makespan'];target=.10 if off['id'].endswith('32768') else .03
                row=dict(head=head,scenario=off['id'],off_seconds=off['makespan'],on_seconds=on['makespan'],
                    reduction=reduction,target=target,target_met=reduction>=target,output_hash_equal=same,
                    equal_output_256=equal_length,off_child_ttft=[r['ttft'] for r in off['rows'][1:]],
                    on_child_ttft=[r['ttft'] for r in on['rows'][1:]],
                    throughput_gain=(on['output_tokens']/on['makespan'])/(off['output_tokens']/off['makespan'])-1)
                gate['results'].append(row)
        gate['passed']=all(r['target_met'] and all(r['output_hash_equal']) and r['equal_output_256'] for r in gate['results'])
    elif args.phase=='d':
        arms={(s['head'],s['binary']==plan['candidate']):read(phase/'run'/s['name']/'results.json') for s in plan['steps']}
        for head in ['full','subset','plain']:
            for off,on in zip(arms[head,False],arms[head,True]):
                same=[a['output_sha256']==b['output_sha256'] for a,b in zip(off['rows'],on['rows'])]
                mean=lambda xs:sum(xs)/len(xs)
                def stats(case):
                    return {k:mean([r[k] for r in case['rows']]) for k in ['latency','ttft']}
                a,b=stats(off),stats(on)
                gate['results'].append(dict(head=head,tier=off['id'],parent=a,candidate=b,
                    latency_change=b['latency']/a['latency']-1,ttft_change=b['ttft']/a['ttft']-1,
                    output_equal=same))
        gate['passed']=all(all(r['output_equal']) for r in gate['results'])
        gate['protection_note']='Output gate only. Timing requires engineering review, no automatic tolerance/noninferiority.'
    save(BASE/('gate-'+args.phase+'.json'),gate)
    print(json.dumps(gate,indent=2,ensure_ascii=False))


if __name__=='__main__':main()
