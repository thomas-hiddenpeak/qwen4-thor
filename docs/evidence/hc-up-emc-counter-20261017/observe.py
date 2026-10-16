"""Fixed three read-only EMC counter observations, no clock writes/load."""
import json,subprocess,time
from pathlib import Path
OUT=Path(__file__).resolve().parent
result={'protocol':{'reads':3,'interval_seconds':1,'gpu_requests':0,'writes':False,'purpose':'Check whether currently active EMC residency count advances without switching; units remain unproven'},'samples':[]}
for i in range(3):
 if i:time.sleep(1)
 before=time.monotonic_ns()
 run=subprocess.run(['sudo','-n','cat','/sys/kernel/debug/bpmp/debug/emc/stats'],capture_output=True,text=True,timeout=5)
 after=time.monotonic_ns();assert run.returncode==0,run.stderr
 rows={};meta={}
 for line in run.stdout.splitlines():
  fields=line.split()
  if len(fields)==2 and fields[0].isdigit():rows[fields[0]]=int(fields[1])
  elif len(fields)==2 and fields[0].endswith(':'):meta[fields[0][:-1]]=int(fields[1])
 assert len(rows)==4 and set(meta)=={'transitions','time-stamp'}
 result['samples'].append({'before_monotonic_ns':before,'after_monotonic_ns':after,'rates':rows,'meta':meta,'raw':run.stdout,'sysfs_cur_freq':Path('/sys/class/devfreq/bwmgr/cur_freq').read_text().strip()})
(OUT/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
