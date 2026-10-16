import hashlib,json,os,subprocess
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor');out=Path(__file__).resolve().parent
protocol=json.loads((out/'protocol.json').read_text())
base=root/protocol['fixture']
env=os.environ.copy()
for key in list(env):
 if key.startswith('Q4T_') or key=='LD_PRELOAD':env.pop(key)
for path,expected in protocol['tool_sha256'].items():assert hashlib.sha256((root/path).read_bytes()).hexdigest()==expected
assert hashlib.sha256((root/protocol['probe']).read_bytes()).hexdigest()==protocol['probe_sha256']
assert int(Path('/proc/self/personality').read_text().strip(),16)&0x40000==0
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
 for index,variant in enumerate(protocol['order']):
  binary=root/protocol['binary_paths'][variant]
  assert hashlib.sha256(binary.read_bytes()).hexdigest()==protocol['sha256'][variant]
  name=f'{index+1:02}-{variant}';dest=out/'http'/name
  assert not dest.exists(), 'Do not overwrite/repeat a request'
  cmd=[str(root/'tools/evalscope/.venv/bin/python3'),'tools/evalscope/run_acceptance.py','--binary',str(binary),'--mode','performance','--perf-lengths','204800','--perf-repeats','1','--fixtures',str(base),'--reference',str(base/'results.json'),'--output',str(dest),'--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--startup-timeout','300','--allow-unqualified-binary','--port','8730','--ucf-probe',str(root/protocol['probe'])]
  with (out/(name+'-driver.log')).open('w') as f:rc=subprocess.run(cmd,cwd=root,env=env,stdout=f,stderr=subprocess.STDOUT).returncode
  (out/(name+'-exit.json')).write_text(json.dumps({'command':cmd,'returncode':rc},indent=2)+'\n')
  if rc:raise SystemExit(rc)
  counters=list(dest.rglob('ucf/counter.json'));assert len(counters)==1
  counter=json.loads(counters[0].read_text());assert counter['complete'] and not counter['error']
  bound=counter['boundary']
  assert 0<=bound['pre_request_count_ns']<=1000000, 'pre-request boundary invalid'
  assert 0<=bound['first_to_stop_ns']<=1000000, 'first-output stop boundary invalid'
  row=json.loads((dest/'results.json').read_text())[0]
  obs={'name':name,'variant':variant,'metrics':row['metrics'][0],'counter':counter}
  rows.append(obs);(out/'observations.json').write_text(json.dumps(rows,indent=2)+'\n')
  print(json.dumps({'name':name,'metrics':obs['metrics'],'boundary':bound}),flush=True)
 print('Four fixed UCF mechanism requests completed.',flush=True)
finally:
 stop.set();t.join()
