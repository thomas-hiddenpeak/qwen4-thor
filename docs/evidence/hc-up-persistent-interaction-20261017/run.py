import hashlib,json,os,re,subprocess,threading,time
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor')
OUT=Path(__file__).resolve().parent
protocol=json.loads((OUT/'protocol.json').read_text())
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
for path,sha in protocol['sha256'].items():
 assert digest(path)==sha,('identity changed',path)
assert digest(__file__)==protocol['driver_sha256']
base=ROOT/'.q4t-work/mtp-numerical-closure-20261016/http/off'
reference=next(x for x in json.loads((base/'results.json').read_text()) if x['length']==204800)
assert reference['outputs']==[protocol['expected_output']]*3
probe=ROOT/'.q4t-work/hc-up-persistent-control-20261017/probe.so'
manifest=ROOT/'docs/evidence/hc-up-persistent-control-20261017/sizes.txt'
env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
env.update(LD_PRELOAD=str(probe),Q4T_DIAG_PERSISTENT_MANIFEST=str(manifest),
           Q4T_DIAG_PERSISTENT_SHIFT='0')
(OUT/'environment.json').write_text(json.dumps({k:v for k,v in env.items() if k.startswith(('Q4T_','CUDA_')) or k=='LD_PRELOAD'},indent=2)+'\n')
stop=threading.Event()
paths=[Path('/proc/meminfo'),Path('/proc/vmstat'),Path('/proc/pressure/cpu'),Path('/proc/pressure/memory'),Path('/proc/pressure/io')]
paths+=list(Path('/sys/class/devfreq').glob('*/cur_freq'))
paths+=list(Path('/sys/class/thermal').glob('thermal_zone*/temp'))
paths+=list(Path('/sys/devices/system/cpu/cpufreq').glob('policy*/scaling_cur_freq'))
def monitor():
 with (OUT/'telemetry.jsonl').open('x') as f:
  while not stop.is_set():
   row={}
   for p in paths:
    try:row[str(p)]=p.read_text()
    except OSError as e:row[str(p)]={'error':str(e)}
   f.write(json.dumps({'monotonic':time.monotonic(),'wall':time.time(),'values':row})+'\n');f.flush();stop.wait(5)
t=threading.Thread(target=monitor,daemon=True);t.start()
try:
 rows=[]
 for index,(variant,mode) in enumerate(protocol['order']):
  env['Q4T_DIAG_PERSISTENT_MODE']=mode
  binary=Path(protocol['binaries'][variant]);assert digest(binary)==protocol['sha256'][str(binary)]
  name=f'{index+1:02}-{variant}-{mode}';dest=OUT/'http'/name
  assert not dest.exists(),'Do not overwrite/repeat'
  cmd=[str(ROOT/'tools/evalscope/.venv/bin/python3'),'tools/evalscope/run_acceptance.py','--binary',str(binary),'--mode','performance','--perf-lengths','204800','--perf-repeats','1','--fixtures',str(base),'--reference',str(base/'results.json'),'--output',str(dest),'--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--startup-timeout','300','--allow-unqualified-binary','--port','8730','--persistent-backing-fds']
  with (OUT/(name+'-driver.log')).open('x') as log:
   rc=subprocess.run(cmd,cwd=ROOT,env=env,pass_fds=(73,74,75),stdout=log,stderr=subprocess.STDOUT).returncode
  (OUT/(name+'-exit.json')).write_text(json.dumps({'command':cmd,'returncode':rc},indent=2)+'\n')
  if rc:raise SystemExit(rc)
  text=(dest/'server.log').read_text()
  assert 'INVALID' not in text
  assert text.count('[persistent-layout] startup_complete=1541')==1
  assert text.count('[persistent-layout] cleanup_complete=3')==1
  captures=re.findall(r'\[persistent-layout\] slot=(\d+) original=(\S+) va=(\d+) exposed=(\S+) bytes=(\d+) mode=(\S+)',text)
  assert len(captures)==3
  for i,(slot,original,va,exposed,n,reported_mode) in enumerate(captures):
   assert int(slot)==i and reported_mode==mode
   assert int(n)==[167772160,167772160,2348023808][i]
   assert int(va)==0x4000000000+i*0x100000000
   assert int(exposed,16)==(int(va) if mode=='shared' else int(original,16))
  exit_record=json.loads((dest/'exit.json').read_text())
  assert exit_record['http_output_checks_passed'] and exit_record['server']==0
  result=json.loads((dest/'results.json').read_text())
  assert len(result)==1 and result[0]['outputs']==[protocol['expected_output']]
  result=result[0];assert result['prompt_sha256']==reference['prompt_sha256']
  rows.append({'name':name,'variant':variant,'mode':mode,'metrics':result['metrics'][0],
               'outputs':result['outputs'],'prompt_sha256':result['prompt_sha256'],'placements':captures})
  (OUT/'observations.json').write_text(json.dumps(rows,indent=2)+'\n')
  print(json.dumps(rows[-1]),flush=True)
 print('Eight frozen persistent-placement requests completed.',flush=True)
finally:
 stop.set();t.join()
