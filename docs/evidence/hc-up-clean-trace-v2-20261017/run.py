import hashlib,json,os,subprocess
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor');out=Path(__file__).resolve().parent
p=json.loads((out/'protocol.json').read_text())
for f,h in p['tool_sha256'].items():assert hashlib.sha256((root/f).read_bytes()).hexdigest()==h
env=os.environ.copy()
for k in list(env):
 if k.startswith('Q4T_') or k=='LD_PRELOAD':env.pop(k)
(out/'environment.json').write_text(json.dumps({k:v for k,v in env.items() if k.startswith(('Q4T_','CUDA_')) or k=='LD_PRELOAD'},indent=2)+'\n')
import threading,time
stop=threading.Event()
paths=[Path('/proc/meminfo'),Path('/proc/vmstat'),Path('/proc/pressure/cpu'),Path('/proc/pressure/memory'),Path('/proc/pressure/io')]
paths+=list(Path('/sys/class/devfreq').glob('*/cur_freq'))
paths+=list(Path('/sys/class/thermal').glob('thermal_zone*/temp'))
paths+=list(Path('/sys/devices/system/cpu/cpufreq').glob('policy*/scaling_cur_freq'))
def telemetry():
 with (out/'telemetry.jsonl').open('w') as f:
  while not stop.is_set():
   snapshot={}
   for p in paths:
    try:snapshot[str(p)]=p.read_text()
    except OSError as e:snapshot[str(p)]={'error':str(e)}
   f.write(json.dumps({'monotonic':time.monotonic(),'wall':time.time(),'values':snapshot})+'\n');f.flush();stop.wait(5)
t=threading.Thread(target=telemetry,daemon=True);t.start()
try:
 for v in p['order']:
  binary=root/p['binary'][v]
  assert hashlib.sha256(binary.read_bytes()).hexdigest()==p['binary_sha256'][v]
  assert not (out/v).exists()
  cmd=['python3','tools/evalscope/profile_http.py','--diagnose-regression','--accepted-run',str(root/p['accepted'][v]),'--binary',str(binary),'--output',str(out/v),'--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--port','8730','--lengths','204800']
  with (out/(v+'-driver.log')).open('w') as f:rc=subprocess.run(cmd,cwd=root,env=env,stdout=f,stderr=subprocess.STDOUT).returncode
  (out/(v+'-driver-exit.json')).write_text(json.dumps({'command':cmd,'returncode':rc},indent=2)+'\n')
  if rc:raise SystemExit(rc)
  status=json.loads((out/v/'exit.json').read_text())
  assert status['failure'] is None and status['shutdown']==0 and status['trace_exports_complete']
  print(v+' clean capture and output check completed',flush=True)
finally:
 stop.set();t.join()
