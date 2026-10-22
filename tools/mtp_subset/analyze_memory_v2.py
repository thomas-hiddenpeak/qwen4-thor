import json,re
from pathlib import Path
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('--base',type=Path,required=True)
b=parser.parse_args().base.resolve();o=b/'source/.q4t-work/acceptance-v2'
windows={}
for name in ['quality-on','quality-off','parent-off','candidate-off','candidate-on','parent-on']:
 p=o/name/'events.jsonl'
 if not p.exists():continue
 ev=[json.loads(l) for l in p.read_text().splitlines()]
 by={e['event']:e for e in ev}
 if 'server_started' not in by:continue
 windows[name]={'start':by['server_started']['unix_seconds'],'end':by.get('server_exited',{}).get('unix_seconds',float('inf')),'pid':str(by['server_started']['pid']),'samples':0,'rss_peak_bytes':0,'available_min_bytes':float('inf'),'cached_peak_bytes':0,'nvml_process_peak_bytes':0}
for line in (o/'unified-memory.jsonl').open():
 r=json.loads(line)
 for n,w in windows.items():
  if w['pid'] not in r['processes']:continue
  mem={k:int(v)*1024 for k,v in re.findall(r'^(\w+):\s*(\d+) kB$',r['meminfo'],re.M)}
  status=r['processes'][w['pid']];rss=re.search(r'^VmRSS:\s*(\d+) kB',status,re.M)
  w['samples']+=1;w['rss_peak_bytes']=max(w['rss_peak_bytes'],int(rss[1])*1024 if rss else 0);w['available_min_bytes']=min(w['available_min_bytes'],mem['MemAvailable']);w['cached_peak_bytes']=max(w['cached_peak_bytes'],mem['Cached'])
for line in (b/'lease-v2/nvml-queries.jsonl').open():
 r=json.loads(line)
 for pm in r.get('process_memory',[]):
  for w in windows.values():
   if str(pm['pid'])==w['pid']:w['nvml_process_peak_bytes']=max(w['nvml_process_peak_bytes'],pm['used_bytes'])
for w in windows.values():
 for k in ['start','end']:w.pop(k)
(b/'memory-v2.json').write_text(json.dumps(windows,indent=2)+'\n');print(json.dumps(windows,indent=2))
