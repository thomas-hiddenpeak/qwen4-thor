import hashlib,json,os,re,subprocess,time
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
protocol=json.loads((OUT/'protocol.json').read_text())
for path,sha in protocol['sha256'].items():
 assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==sha,path
rows=[]
for variant,binary in protocol['binaries'].items():
 env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
 env.update(LD_PRELOAD=protocol['probe'],Q4T_DIAG_MODULE_OBSERVER='1')
 log_path=OUT/f'{variant}-server.log'
 assert not log_path.exists(),'no overwrites/retries'
 cmd=[binary,'serve','--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--port','8730','--max-seq','1','--max-prefill','8192','--max-len','208896','--max-tokens','256','--no-mtp']
 (OUT/f'{variant}-command.json').write_text(json.dumps({'argv':cmd,'environment':{k:v for k,v in env.items() if k.startswith(('Q4T_','CUDA_')) or k=='LD_PRELOAD'}},indent=2)+'\n')
 with log_path.open('x') as log:
  p=subprocess.Popen(cmd,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
  try:
   ready=False
   for _ in range(600):
    if p.poll() is not None:raise RuntimeError('server exited before ready')
    text=log_path.read_text()
    if 'serving on port' in text:ready=True;break
    time.sleep(.5)
   if not ready:raise RuntimeError('startup timeout')
   (OUT/f'{variant}-ready.json').write_text(json.dumps({'pid':p.pid,'monotonic':time.monotonic(),'http_requests_sent':0})+'\n')
  finally:
   if p.poll() is None:p.terminate()
   try:rc=p.wait(timeout=60)
   except subprocess.TimeoutExpired:
    p.kill();p.wait();raise
   (OUT/f'{variant}-exit.json').write_text(json.dumps({'returncode':p.returncode})+'\n')
 assert rc==0
 text=log_path.read_text();assert 'INVALID' not in text
 assert 'phase=request_begin' not in text
 assert 'effective_max_len=208896 effective_max_seq=1 effective_max_prefill=8192' in text
 records=re.findall(r'sequence=(\d+) module_id=(\d+) context=(\S+) bytes=(\d+) sha256=(\w+)',text)
 completes=re.findall(r'\[module-load\] complete=(\d+)',text)
 assert completes==[str(len(records))]
 assert text.count('[module-load] enabled=1')==1
 assert sorted(int(r[0]) for r in records)==list(range(len(records)))
 row={'variant':variant,'loaded':[dict(sequence=int(r[0]),module_id=int(r[1]),context=r[2],bytes=int(r[3]),sha256=r[4]) for r in records],'http_requests':0,'ready':True,'exit':rc}
 rows.append(row);(OUT/'observations.json').write_text(json.dumps(rows,indent=2)+'\n')
 print(json.dumps({'variant':variant,'module_records':len(records),'ready':True,'exit':rc}),flush=True)
