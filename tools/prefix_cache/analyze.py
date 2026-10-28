"""Offline phase gates and descriptive paired results; never runs inference."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'.q4t-work/prefix-cache-20261028'
def read(path):return json.loads(path.read_text())
def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(path,value):path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['a','b','c','d']);p.add_argument('--directory',type=Path);args=p.parse_args()
    phase=args.directory or BASE/('phase-'+args.phase);plan=read(phase/'plan.json')
    state=read(phase/'run/state.json');post=read(phase/'postflight.json');guard=read(phase/'lease/exit.json')
    if state['status']!='MEASURED_REQUIRES_REVIEW' or post['status']!=state['status']:
        raise RuntimeError('stage not complete/valid')
    if guard.get('status')!='COMPLETED':raise RuntimeError('outer lease not complete: '+repr(guard))
    gate=dict(passed=True,candidate_sha256=plan['candidate_sha256'],phase=args.phase,source=str(phase),
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
        slots={}
        for step in plan['steps']:
            raw=(phase/'run'/step['name']/'server.log').read_text()
            slots[step['head'],step['shared']]={
                request:int(slot) for request,slot in re.findall(
                    r'\[q4t\]\[slot-admission\] request=(\S+) slot=(\d+) ',raw)}
        for head in ['full','subset']:
            if len(arms[head,False])!=4 or len(arms[head,True])!=4:
                raise RuntimeError('four scenarios required per arm')
            for off,on in zip(arms[head,False],arms[head,True]):
                if off['id']!=on['id']:raise RuntimeError('case identity')
                if len(off['rows'])!=4 or len(on['rows'])!=4:
                    raise RuntimeError('parent and three children required')
                same=[a['output_sha256']==b['output_sha256'] for a,b in zip(off['rows'],on['rows'])]
                equal_length=all(a['output_tokens']==b['output_tokens']==256 for a,b in zip(off['rows'],on['rows']))
                same_input=all(a['input_tokens']==b['input_tokens'] and
                    a['prompt_sha256']==b['prompt_sha256'] for a,b in zip(off['rows'],on['rows']))
                public=int(off['id'].rsplit('-',1)[1])
                cache_contract=([r['cached_tokens'] for r in off['rows']]==[0]*4 and
                    [r['cached_tokens'] for r in on['rows']]==[0,public,public,public] and
                    all(r['cache_source']=='shared' for r in on['rows'][1:]))
                slot_assignments={}
                for enabled,case in [(False,off),(True,on)]:
                    actual=[slots[head,enabled][r['response_id']] for r in case['rows']]
                    if actual[0]!=0 or set(actual[1:])!={1,2,3}:
                        raise RuntimeError('long parent and independent short slots')
                    slot_assignments['on' if enabled else 'off']=actual
                reduction=1-on['makespan']/off['makespan'];target=.10 if off['id'].endswith('32768') else .03
                row=dict(head=head,scenario=off['id'],off_seconds=off['makespan'],on_seconds=on['makespan'],
                    reduction=reduction,target=target,target_met=reduction>=target,output_hash_equal=same,
                    equal_output_256=equal_length,input_identity_equal=same_input,
                    cache_contract=cache_contract,slot_assignments=slot_assignments,
                    off_child_ttft=[r['ttft'] for r in off['rows'][1:]],
                    on_child_ttft=[r['ttft'] for r in on['rows'][1:]],
                    throughput_gain=(on['output_tokens']/on['makespan'])/(off['output_tokens']/off['makespan'])-1)
                gate['results'].append(row)
        gate['correctness_passed']=all(all(r['output_hash_equal']) and
            r['equal_output_256'] and r['input_identity_equal'] and
            r['cache_contract'] for r in gate['results'])
        gate['performance_targets_passed']=all(r['target_met'] for r in gate['results'])
        gate['passed']=gate['correctness_passed'] and gate['performance_targets_passed']
    elif args.phase=='d':
        arms={(s['head'],s['binary']==plan['candidate']):read(phase/'run'/s['name']/'results.json') for s in plan['steps']}
        for head in ['full','subset','plain']:
            for off,on in zip(arms[head,False],arms[head,True]):
                same=[a['output_sha256']==b['output_sha256'] for a,b in zip(off['rows'],on['rows'])]
                spec=importlib.util.spec_from_file_location('prefix_compare',ROOT/'tools/mtp_suffix_agent/analyze.py')
                compare=importlib.util.module_from_spec(spec);spec.loader.exec_module(compare)
                gate['results'].append(dict(head=head,tier=off['id'],output_equal=same,
                                           **compare.compare(off,on)))
        gate['passed']=all(all(r['output_equal']) for r in gate['results'])
        gate['protection_note']='Output gate only. Timing requires engineering review, no automatic tolerance/noninferiority.'
    save(BASE/('gate-'+args.phase+'.json'),gate)
    print(json.dumps(gate,indent=2,ensure_ascii=False))


if __name__=='__main__':main()
