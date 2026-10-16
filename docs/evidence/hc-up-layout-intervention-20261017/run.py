import hashlib,json,os,subprocess
from pathlib import Path
from validate_layout import validate
root=Path('/home/rm01/models/dev/qwen4-thor');out=Path(__file__).resolve().parent
protocol=json.loads((out/'protocol.json').read_text());identity=json.loads((out/'derivation.json').read_text())
base=root/'.q4t-work/mtp-numerical-closure-20261016/http/off'
env=os.environ.copy()
# Freeze diagnostics independently from a caller's shell experiments.
for key in list(env):
 if key.startswith('Q4T_') or key=='LD_PRELOAD':env.pop(key)
env['LD_PRELOAD']=str(out/'probe.so')
assert hashlib.sha256((out/'probe.so').read_bytes()).hexdigest()==protocol['probe_sha256']
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
 rows=[]
 for index,(variant,mode) in enumerate(protocol['order']):
  env['Q4T_DIAG_SCRATCH_LAYOUT']=mode
  binary=out/variant/'q4t';expected=identity['variants'][variant]['sha256']
  assert hashlib.sha256(binary.read_bytes()).hexdigest()==expected
  name=f'{index+1:02}-{variant}-{mode}';dest=out/'http'/name
  assert not dest.exists(), 'Do not overwrite/repeat a request'
  cmd=[str(root/'tools/evalscope/.venv/bin/python3'),'tools/evalscope/run_acceptance.py','--binary',str(binary),'--mode','performance','--perf-lengths','204800','--perf-repeats','1','--fixtures',str(base),'--reference',str(base/'results.json'),'--output',str(dest),'--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--startup-timeout','300','--allow-unqualified-binary','--port','8730']
  with (out/(name+'-driver.log')).open('w') as f:rc=subprocess.run(cmd,cwd=root,env=env,stdout=f,stderr=subprocess.STDOUT).returncode
  (out/(name+'-exit.json')).write_text(json.dumps({'command':cmd,'returncode':rc},indent=2)+'\n')
  if rc:raise SystemExit(rc)
  coverage=validate((dest/'server.log').read_text(),mode)
  (dest/'layout-coverage.json').write_text(json.dumps(coverage,indent=2)+'\n')
  row=json.loads((dest/'results.json').read_text())[0]
  rows.append({'name':name,'variant':variant,'mode':mode,'binary_sha256':expected,'metrics':row['metrics'][0],'output':row['outputs'][0],'prompt':row['prompt_sha256']})
  (out/'observations.json').write_text(json.dumps(rows,indent=2)+'\n');print(json.dumps(rows[-1]),flush=True)
 print('Eight fixed layout-interaction requests completed.',flush=True)
finally:
 stop.set();t.join()
