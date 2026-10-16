"""Apply only pre-frozen validity and decision gates to completed crossover."""
import fcntl
import hashlib
import json
from pathlib import Path
import sys

OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(OUT.parents[1]/'tools/evalscope'))
from gpu_clock_io import snapshot
from gpu_exclusive import common_lock
p=json.loads((OUT/'protocol.json').read_text())
config=json.loads((OUT/'config.json').read_text())
for path,digest in p['sha256'].items():
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest,path
lease=json.loads((OUT/'guard/exit.json').read_text())
assert lease['status']=='COMPLETED' and lease['child_returncode']==0
assert json.loads((OUT/'command-result.json').read_text())['returncode']==0
restored=json.loads((OUT/'restored.json').read_text())
assert restored['marker_absent'] and restored['snapshot']['range']==config['range']
now=snapshot()
assert now['identity']==config['identity'] and now['range']==config['range']
lock=common_lock();assert not lock.with_name(lock.name+'.recovery-required').exists()
with lock.open('r+') as probe:fcntl.flock(probe,fcntl.LOCK_EX | fcntl.LOCK_NB)
rows=json.loads((OUT/'observations.json').read_text())
assert [r['label'] for r in rows]==p['order']
times={};enabled=None;counter_identity=None;events={}
for row in rows:
    label=row['label'];dest=OUT/'http'/label
    c=json.loads((dest/'context-204800/run-1/oc/counter.json').read_text())
    assert c['complete'] and not c['error'] and len(c['rows'])==2
    a,b=c['rows']
    ident=(a['boot_id'],a['device'])
    assert ident==(b['boot_id'],b['device'])
    if counter_identity is None:counter_identity=ident
    assert counter_identity==ident
    alarms=[f'oc{i}' for i in range(1,4) if a['values'][f'oc{i}_throt_en']]
    if enabled is None:enabled=alarms
    assert enabled==alarms and enabled
    for i in range(1,4):
        k=f'oc{i}'
        assert a['values'][k+'_throt_en']==b['values'][k+'_throt_en']
        assert c['delta'][k]==b['values'][k+'_event_cnt']-a['values'][k+'_event_cnt']>=0
    assert all(c['boundary'][key]<=p['max_each_slack_ns'] for key in ('leading_slack_ns','trailing_slack_ns'))
    result=json.loads((dest/'results.json').read_text())
    assert len(result)==1 and result[0]['outputs']==[p['expected_output']]
    assert result[0]['prompt_sha256']==p['expected_prompt']
    ex=json.loads((dest/'exit.json').read_text())
    assert ex['server']==0 and ex['completed']==1 and ex['http_output_checks_passed'] and ex['diagnostic_oc']
    for suffix in ('before','after'):
        clock=json.loads((OUT/f'{label}-clock-{suffix}.json').read_text())
        assert clock['identity']==config['identity'] and clock['range']==row['frequency']
    times[label]=c['boundary']['first_output_ns']-c['boundary']['request_start_ns']
    events[label]=sum(c['delta'][k] for k in enabled)
R=max(abs(times[f+b+'2']-times[f+b+'1']) for f in 'OL' for b in 'AB')
D={f:[times[f+'B'+str(j)]-times[f+'A'+str(j)] for j in (1,2)] for f in 'OL'}
I=[a-b for a,b in zip(D['O'],D['L'])]
oc=all(events[x]>0 if x[0]=='O' else events[x]==0 for x in p['order'])
interaction=all(d>0 for d in D['O']) and I[0]*I[1]>0 and min(abs(x) for x in I)>2*R
monitor=[json.loads(l) for l in (OUT/'guard/events.jsonl').read_text().splitlines()]
monitor=[e for e in monitor if e['event']=='monitor']
assert monitor and all(not e['conflicts'] for e in monitor)
result={'valid':True,'TTFT_ns':times,'enabled_OC_event_sums':events,'R_ns':R,
        'D_ns':D,'interaction_ns':I,'OC_suppression_gate':oc,
        'interaction_gate':interaction,'guard_monitor_count':len(monitor),
        'decision':p['decision']['positive'] if oc and interaction else p['decision']['negative'],
        'performance_acceptance':False}
(OUT/'analysis.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
