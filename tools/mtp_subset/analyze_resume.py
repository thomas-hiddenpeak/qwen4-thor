import json,re,statistics,hashlib
from pathlib import Path
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('--base',type=Path,required=True)
b=parser.parse_args().base.resolve();o=b/'source/.q4t-work/acceptance-v2'
s={'state':json.loads((o/'state.json').read_text()),'groups':{}}
for name in ['quality-on','quality-off','parent-off','candidate-off','candidate-on','parent-on']:
 p=o/name
 if not (p/'results.json').exists():continue
 rows=json.loads((p/'results.json').read_text());g={'rows':rows};s['groups'][name]=g
 if name.startswith('quality'):g['exact_matches']=sum(r['exact_match'] for r in rows)
 else:
  g['summary']=[dict(length=r['length'],ttft_median=statistics.median(x['ttft'] for x in r['metrics']),decode_median=statistics.median(x['decode_tps'] for x in r['metrics']),first=r['metrics'][0],repeats=r['metrics'][1:],deterministic=r['deterministic']) for r in rows]
 if (p/'exit.json').exists():g['exit']=json.loads((p/'exit.json').read_text())
 events=[json.loads(l) for l in (p/'events.jsonl').read_text().splitlines()]
 by={e['event']:e for e in events}
 if 'server_ready' in by:g['load_seconds']=by['server_ready']['monotonic_seconds']-by['server_started']['monotonic_seconds']
 log=(p/'server.log').read_text();g['acceptance']=[dict(request=m[0],counts=list(map(int,m[1].split(',')))) for m in re.findall(r'\[mtp-accept\] request=(\S+) draft_counts=([0-9,]+)',log)]
 if g['acceptance']:
  counts=[sum(r['counts'][i] for r in g['acceptance']) for i in range(4)]
  g['acceptance_total']=dict(counts=counts,accepted_per_step=sum(i*n for i,n in enumerate(counts))/sum(counts),accepted_fraction_of_fixed_k3=sum(i*n for i,n in enumerate(counts))/(3*sum(counts)))
 g['finalized']=[json.loads(l.split('] ',1)[1]) for l in log.splitlines() if l.startswith('[startup-reservation] ') and '"event":"finalized"' in l]
for mode in ['off','on']:
 if any(n+'-'+mode not in s['groups'] for n in ['parent','candidate']):continue
 p=s['groups']['parent-'+mode];c=s['groups']['candidate-'+mode]
 s[mode+'_comparison']=[]
 for a,z,ar,zr in zip(p['summary'],c['summary'],p['rows'],c['rows']):
  assert a['length']==z['length'] and ar['prompt_sha256']==zr['prompt_sha256']
  s[mode+'_comparison'].append(dict(length=a['length'],decode_change_pct=100*(z['decode_median']/a['decode_median']-1),ttft_change_pct=100*(z['ttft_median']/a['ttft_median']-1),outputs_equal=ar['outputs']==zr['outputs']))
(b/'analysis-v2.json').write_text(json.dumps(s,indent=2)+'\n')
print(json.dumps({k:v for k,v in s.items() if k!='groups'},indent=2))
for n,g in s['groups'].items():print(n,'quality',g.get('exact_matches'),'load',g.get('load_seconds'),'tiers',len(g.get('summary',[])))
