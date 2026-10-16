import collections,json
from pathlib import Path
root=Path(__file__).resolve().parent
assert (root/'fixture.stdout').read_text()=='fixture passed\n'
groups=collections.defaultdict(list)
for line in (root/'fixture.log').read_text().splitlines():
 assert line.startswith('[gather-interval]')
 r={k:int(v) for k,v in (x.split('=') for x in line.split()[1:])};groups[r['tid']].append(r)
assert len(groups)==2
for rows in groups.values():
 assert [x['seq'] for x in rows]==[0,1,2,3]
 assert all(x['copy_rc']==0 for x in rows)
 assert rows[0]['prior_sync']==rows[3]['prior_sync']==0
 v=rows[1];assert v['prior_sync']==1 and v['io_calls']==1 and v['requested_completions']==7 and v['io_errors']==0
 assert 20000000<=v['cpu_ns']<40000000 and v['wall_ns']>=50000000
 assert v['io_wall_ns']>=30000000 and v['io_cpu_ns']<10000000
 assert v['wall_ns']-v['io_wall_ns']<40000000 # excludes prior fake 20ms sync
 assert rows[2]['prior_sync']==1 and rows[2]['io_errors']==1 and rows[2]['requested_completions']==2
result={'threads':dict(groups),'checks':['arguments, return values, errno preserved by fixture assertions','no prior sync and failed sync marked unavailable','independent thread sequences','CPU work separated from fake IO sleep','prior synchronization excluded','IO failure visible','unrelated memcpy shape ignored'],'scope':'CPU-only fake library; no CUDA or model execution'}
(root/'cpu-validation.json').write_text(json.dumps(result,indent=2)+'\n')
print('Gather observer CPU fixture checks passed')
