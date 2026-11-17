from pathlib import Path
import importlib.util,json,os,subprocess,time,shutil,hashlib
ROOT=Path(__file__).resolve().parents[2]
W=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('asr_acceptance',ROOT/'tools/native_asr/acceptance.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
old=W/'compute-gpu-01/run';r=json.loads((old/'result.json').read_text())
assert r['status']=='FAILED' and r['error']=='reference execution failed'
assert r['runs'][0]['exit_code']==0 and r['thresholds']==a.THRESHOLDS
identity=json.loads((W/'compute-gpu-01/source-identity.json').read_text())
assert a.digest(Path(identity['binary']))==r['binary_sha256']
assert a.digest(W/'model-cases-01/plan.json')==r['plan_sha256']
for path,digest in identity['source_sha256'].items():
 if path.startswith(('src/','include/','cmake/')) or path in ['CMakeLists.txt','tools/native_asr/model_replay.cpp','tools/native_asr/CMakeLists.txt']:
  assert a.digest(ROOT/path)==digest,path
assert a.defaults()==r['defaults_before']==r['defaults_after']
lease=int(os.environ['Q4T_MANAGED_LEASE_FD']);os.fstat(lease)
out=W/'compute-reference-02/run';out.mkdir(parents=True,exist_ok=False)
(out/'native').mkdir()
actual={str(p.relative_to(old)):p for p in (old/'native').rglob('*') if p.is_file()}
expected={k:v for k,v in r['artifacts'].items() if k.startswith('native/')}
assert actual.keys()==expected.keys()
for name,p in actual.items():
 assert not p.is_symlink() and a.digest(p)==expected[name]
 dest=out/name;dest.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(p,dest)
record={'status':'RUNNING','thresholds':a.THRESHOLDS,'source_group':str(old),
 'source_result_sha256':a.digest(old/'result.json'),'reused_native_loads':1,
 'new_gpu_loads':0,'cpu_reference_loads':1,'cpu_max_forwards':264,'seconds_limit':600,
 'defaults_before':a.defaults(),'reference_source_sha256':a.digest(ROOT/'tools/native_asr/model_reference.py')}
start=time.monotonic()
command=[str(ROOT/'.q4t-work/refenv/bin/python'),'-B',str(ROOT/'tools/native_asr/model_reference.py'),
 '--assets',str(W/'assets-01'),'--official-source',str(W/'official-src'),
 '--plan',str(W/'model-cases-01/plan.json'),'--native',str(out/'native'),'--output',str(out/'reference')]
record['command']=command
try:
 env=dict(os.environ,PYTHONPATH=str(W/'reference-deps'),PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',OMP_NUM_THREADS='8')
 with (out/'reference.log').open('wb') as log:
  child=subprocess.run(command,cwd=ROOT,env=env,pass_fds=(lease,),stdout=log,stderr=subprocess.STDOUT,timeout=600)
 record['reference_exit_code']=child.returncode
 assert child.returncode==0,'reference execution failed'
 record['comparisons']=a.compare(out,json.loads((W/'model-cases-01/plan.json').read_text()))
 assert all(x['passed'] for x in record['comparisons']),'numerical/business comparison failed'
 record['status']='PASS_COMPONENT_PENDING_LEASE_RECONCILIATION'
except Exception as e:
 record.update(status='FAILED',error=str(e))
finally:
 record['seconds']=time.monotonic()-start
 record['defaults_after']=a.defaults()
 record['defaults_unchanged']=record['defaults_before']==record['defaults_after']
 if not record['defaults_unchanged']:record.update(status='FAILED',error='protected default changed')
 record['artifacts']={str(p.relative_to(out)):a.digest(p) for p in sorted(out.rglob('*')) if p.is_file()}
 (out/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({k:record[k] for k in ['status','seconds','defaults_unchanged']}))
raise SystemExit(0 if record['status'].startswith('PASS') else 1)
