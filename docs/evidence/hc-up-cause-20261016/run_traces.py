import json, subprocess, threading, time
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor'); out=Path(__file__).resolve().parent
paths=list(Path('/sys/class/thermal').glob('thermal_zone*/temp'))+list(Path('/sys/class/devfreq').glob('*/cur_freq'))+list(Path('/sys/devices/system/cpu/cpufreq').glob('policy*/scaling_cur_freq'))
stop=threading.Event()
def telemetry():
 with (out/'telemetry.jsonl').open('w') as f:
  while not stop.is_set():
   values={}
   for p in paths:
    try: values[str(p)]=int(p.read_text().strip())
    except (OSError,ValueError): pass
   f.write(json.dumps({'monotonic':time.monotonic(),'wall':time.time(),'values':values})+'\n'); f.flush(); stop.wait(1)
t=threading.Thread(target=telemetry);t.start()
try:
 for side,binary,base in [('A','.q4t-work/mtp-decode-20261016/q4t-main','.q4t-work/mtp-numerical-closure-20261016/http/off'),('B','.q4t-work/mtp-decode-20261016/hc-up/q4t','.q4t-work/mtp-decode-20261016/hc-up/off')]:
  cmd=['python3','tools/evalscope/profile_http.py','--diagnose-regression','--accepted-run',base,'--binary',binary,'--output',str(out/side),'--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--port','8730','--lengths','204800']
  start=time.monotonic()
  with (out/(side+'-driver.log')).open('w') as f: rc=subprocess.run(cmd,cwd=root,stdout=f,stderr=subprocess.STDOUT).returncode
  (out/(side+'-driver-exit.json')).write_text(json.dumps({'cmd':cmd,'returncode':rc,'start':start,'end':time.monotonic()},indent=2))
  if rc: raise SystemExit(rc)
finally:stop.set();t.join()
