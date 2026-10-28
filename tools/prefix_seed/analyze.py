"""Offline evidence gates; no inference and no adaptive acceptance thresholds."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'.q4t-work/prefix-seed-20261028'
def read(path):return json.loads(path.read_text())
def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['a','b','c','d']);args=p.parse_args()
    folder=BASE/('phase-'+args.phase);plan=read(folder/'plan.json');state=read(folder/'run/state.json')
    post=read(folder/'postflight.json');guard=read(folder/'lease/exit.json')
    if state['status']!='MEASURED_REQUIRES_REVIEW' or post['status']!=state['status'] or guard['status']!='COMPLETED':raise RuntimeError('stage incomplete')
    if post['final_monitor_errors'] or post['cleanup_errors'] or not post['final_monitor_barrier']:raise RuntimeError('stage monitoring')
    gate=dict(passed=True,phase=args.phase,candidate_sha256=plan['candidate_sha256'],requests=plan['http_requests'],
        rejected_http=plan.get('rejected_http_requests',0),starts=plan['http_loads'],results=[],statistics='DESCRIPTIVE_ONLY',
        state_sha256=sha(folder/'run/state.json'),postflight_sha256=sha(folder/'postflight.json'),guard_sha256=sha(folder/'lease/exit.json'))
    if args.phase=='a':
        for step in plan['steps']:
            d=folder/'run'/step['name'];lc=read(d/'lifecycle.json')
            if not lc['passed'] or not read(d/'first-http-gate.json')['passed'] or len(read(d/'rejected-probes.json'))!=4:raise RuntimeError('HTTP contract')
            gate['results'].append(dict(head=step['head'],lifecycle=lc))
        raw=(folder/'run/00-full-on/cpu-contract.log').read_text()
        counts=re.findall(r'(\d+) tests, (\d+) passed, (\d+) failed, (\d+) skipped',raw)
        if len(counts)!=1 or counts[0][0]!=counts[0][1] or int(counts[0][0])<2 or counts[0][2:]!=('0','0'):raise RuntimeError('CPU policy gate')
        gate['cpu_tests']=int(counts[0][0])
    elif args.phase=='b':
        for step in plan['steps']:
            log=folder/'run'/(step['name']+'-driver.log')
            if 'PASS prefix_state=1' not in log.read_text():raise RuntimeError('independent full-state contract')
            gate['results'].append(dict(head=step['head'],log_sha256=sha(log)))
    elif args.phase=='c':
        arms={};slots={}
        for step in plan['steps']:
            d=folder/'run'/step['name'];key=(step['head'],step['block'],step['shared'])
            arms[key]={c['id']:c for c in read(d/'results.json')}
            slots[key]={req:int(slot) for req,slot in re.findall(r'\[q4t\]\[slot-admission\] request=(\S+) slot=(\d+) ',(d/'server.log').read_text())}
            if len(arms[key])!=4:raise RuntimeError('four scenarios per service')
        for head in ['full','subset']:
            for scenario in plan['scenarios']:
                blocks=[]
                for block in [0,1]:
                    off=arms[head,block,False][scenario['id']];on=arms[head,block,True][scenario['id']]
                    if len(off['rows'])!=4 or len(on['rows'])!=4:raise RuntimeError('four slots per case')
                    same=all(x['prompt_sha256']==y['prompt_sha256'] and x['input_tokens']==y['input_tokens'] and x['output_sha256']==y['output_sha256'] and x['output_tokens']==y['output_tokens']==256 for x,y in zip(off['rows'],on['rows']))
                    cache=([r['cached_tokens'] for r in off['rows']]==[0]*4 and [r['cached_tokens'] for r in on['rows']]==[0]+[scenario['prefix']]*3 and all(r['cache_source']=='shared' for r in on['rows'][1:]))
                    for enabled,case in [(False,off),(True,on)]:
                        actual=[slots[head,block,enabled][r['response_id']] for r in case['rows']]
                        if actual[0]!=0 or set(actual[1:])!={1,2,3}:raise RuntimeError('long/short slots')
                    blocks.append(dict(block=block,off_seconds=off['makespan'],on_seconds=on['makespan'],
                        reduction=1-on['makespan']/off['makespan'],output_and_input_equal=same,cache_contract=cache,
                        off_child_ttft=[r['ttft'] for r in off['rows'][1:]],on_child_ttft=[r['ttft'] for r in on['rows'][1:]],
                        seed_writes=on['seed_writes']))
                ratio=math.exp(sum(math.log(b['on_seconds']/b['off_seconds']) for b in blocks)/2)
                target=.03 if scenario['prefix']==8192 else .10
                passed=1-ratio>=target and all(b['reduction']>0 for b in blocks)
                gate['results'].append(dict(head=head,scenario=scenario['id'],blocks=blocks,
                    gm_time_reduction=1-ratio,gm_throughput_gain=1/ratio-1,target=target,target_met=passed))
        gate['correctness_passed']=all(b['output_and_input_equal'] and b['cache_contract'] for r in gate['results'] for b in r['blocks'])
        gate['performance_targets_passed']=all(r['target_met'] for r in gate['results'])
        gate['passed']=gate['correctness_passed'] and gate['performance_targets_passed']
        if not read(folder/'io-release.json')['passed']:raise RuntimeError('IO preparation cleanup')
    else:
        import importlib.util
        spec=importlib.util.spec_from_file_location('agent_compare',ROOT/'tools/mtp_suffix_agent/analyze.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        arms={(s['head'],s['binary']==plan['candidate']):read(folder/'run'/s['name']/'results.json') for s in plan['steps']}
        for head in ['full','subset','plain']:
            for off,on in zip(arms[head,False],arms[head,True]):
                same=[a['output_sha256']==b['output_sha256'] for a,b in zip(off['rows'],on['rows'])]
                gate['results'].append(dict(head=head,tier=off['id'],output_equal=same,**module.compare(off,on)))
        gate['passed']=all(all(r['output_equal']) for r in gate['results'])
        gate['protection_note']='Output gate only; timing needs engineering review, no automatic tolerance.'
    (BASE/('gate-'+args.phase+'.json')).write_text(json.dumps(gate,indent=2)+'\n')
    print(json.dumps(gate,indent=2))


if __name__=='__main__':main()
