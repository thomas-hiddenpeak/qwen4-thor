"""Account for checkpoint bytes and observed save intervals in completed C."""
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'.q4t-work/prefix-cache-20261028'


def main():
    phase=BASE/'phase-c';plan=json.loads((phase/'plan.json').read_text())
    state=json.loads((phase/'run/state.json').read_text())
    if state['status']=='RUNNING':raise ValueError('completed stage required')
    cases=[];totals={}
    for step in plan['steps']:
        folder=phase/'run'/step['name']
        if not (folder/'results.json').exists():continue
        raw=(folder/'server.log').read_text().splitlines()
        records=[dict(re.findall(r'(\w+)=(\S+)',l),record_type='shared' if l.startswith('[prefix-cache]') else 'session')
            for l in raw if l.startswith(('[prefix-cache] request=','[session-cache] request='))]
        sizes={};previous=0
        if step['shared']:
            first=[r for r in records if r['record_type']=='shared'][:3]
            if [int(r['prefix']) for r in first]!=[8192,32768,65536] or any(r['error']!='none' for r in first):
                raise ValueError('empty-pool seed sizing')
            for r in first:
                sizes[int(r['prefix'])]=int(r['bytes'])-previous;previous=int(r['bytes'])
        for case in json.loads((folder/'results.json').read_text()):
            ids={r['response_id'] for r in case['rows']};parent=case['rows'][0]['response_id']
            public=int(case['id'].rsplit('-',1)[1])
            rows=[r for r in records if r.get('request') in ids]
            writes=[r for r in rows if r['record_type']=='shared' and r['event']=='save' and r['error']=='none']
            unused=[r for r in writes if r['request']!=parent or int(r['prefix'])!=public]
            item=dict(head=step['head'],shared=step['shared'],scenario=case['id'],
                checkpoint_saves=len(writes),checkpoint_bytes_written=sum(sizes[int(r['prefix'])] for r in writes),
                checkpoint_bytes_not_reused_in_this_transaction=sum(sizes[int(r['prefix'])] for r in unused),
                checkpoint_save_wall_ms=sum(float(r['ms']) for r in writes),
                shared_restores=sum(r['record_type']=='session' and r['event']=='restore' and r.get('source')=='shared' and int(r['prefix'])>0 for r in rows),
                parent_session_save_ms=sum(float(r['ms']) for r in rows if r['record_type']=='session' and r['event']=='save' and r['request']==parent),
                parent_snapshot_prefix=[int(r['prefix']) for r in rows if r['record_type']=='session' and r['event']=='save' and r['request']==parent],
                parent_ttft=case['rows'][0]['ttft'],parent_latency=case['rows'][0]['latency'],transaction_seconds=case['makespan'])
            cases.append(item)
        if sizes:totals[step['head']]=dict(entry_bytes=sizes)
    result=dict(cases=cases,heads=totals,method='Entry sizes inferred from the first three successful saves into each empty pool; unchanged production range layout makes sizes depend only on P.',
        conclusions_limited=['Unused means not restored in this frozen transaction, not proof of no possible future reuse.',
        'Prefix save wall time is under model lock. Session save wall time can include lock wait; do not sum overlapping waits as device execution.',
        'Increased existing same-byte snapshot save time is observed, but SSD cache/GC/thermal or host-IO causation is not proven.',
        'Request latency includes final snapshot save and HTTP terminal; it is not pure GPU decode time.'])
    (BASE/'io-review.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
