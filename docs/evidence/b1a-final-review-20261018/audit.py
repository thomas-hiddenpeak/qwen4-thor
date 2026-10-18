"""Read-only evidence review; isolated-index rollback check. No GPU/HTTP."""
from pathlib import Path
import subprocess,json,hashlib,os,sqlite3,re
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=ROOT/'.q4t-work/b1a-final-review-20261018'
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
def git(*args,env=None):return subprocess.check_output(['git',*args],cwd=ROOT,env=env).decode().strip()
result={'head_reviewed':git('rev-parse','HEAD'),'branch':git('branch','--show-current'),'new_gpu_http_requests':0,'manifests':{},'identities':{},'contracts':{},'metrics':[]}
for folder in ['mtp-position-b1a-20261017','b1a-ab60-20261018','no-mtp-baseline-20261018']:
 p=ROOT/'docs/evidence'/folder/'raw-manifest.json';m=json.loads(p.read_text())
 entries=m if isinstance(m,list) else m['files']
 for item in entries:
  f=ROOT/item['path'] if isinstance(m,list) else Path(m['raw_root'])/item['path']
  assert f.stat().st_size==item.get('bytes',item.get('size')),f
  assert sha(f)==item['sha256'],f
 result['manifests'][folder]={'files':len(entries),'manifest_sha256':sha(p),'status':'PASS'}
d=json.loads((ROOT/'docs/evidence/mtp-position-b1a-20261017/delivery-identity.json').read_text())
for name,expected in d['sha256'].items():
 assert sha(ROOT/name)==expected,name
 result['identities'][name]=expected
for f in ['.q4t-work/b1a-20261017/q4t-baseline','.q4t-work/b1a-20261017/q4t-candidate']:
 result['identities'][f]=sha(ROOT/f)
assert not git('diff','--name-only','1c6e792','HEAD','--','src','include','tests','CMakeLists.txt')
log=(ROOT/'docs/evidence/mtp-position-b1a-20261017/contracts.log').read_text()
assert len(re.findall(r'^metadata max=',log,re.M))==56
assert '9 tests, 9 passed, 0 failed, 0 skipped' in log
for pos in [8194,8195]:
 for rows in range(1,9):assert f'metadata max={pos} rows={rows} steps=2 logits_hidden_kv_raw_comp_bit_exact=1' in log
result['contracts']={'same_shape_cases':56,'direct_tests_passed':9,'tested_accept_counts_including_correction':[1,2,4],'accepted_draft_counts':[0,1,3],'a2_not_forced':True}
logs=list((ROOT/'docs/evidence/mtp-position-b1a-20261017').glob('build*.log'))
assert all(not re.search(r'\bwarning:',p.read_text(),re.I) for p in logs)
result['build_logs']={p.name:sha(p) for p in logs}
raw=ROOT/'.q4t-work/b1a-ab60-20261018';summary=json.loads((raw/'summary.json').read_text())
count=0
for mode in ['off','on']:
 for n in [1024,4096,8192,45056,204800]:
  groups={}
  for side in ['A','B']:
   folder=raw/f'{side}-{mode}'/f'context-{n}'
   rows=json.loads((folder/'responses.json').read_text());dbs=list(folder.rglob('benchmark_data.db'));assert len(dbs)==1
   with sqlite3.connect(dbs[0].resolve().as_uri()+'?mode=ro',uri=True) as c:
    r=c.execute('select success,prompt_tokens,completion_tokens,first_chunk_latency,latency from result order by start_time').fetchall()
   assert len(r)==len(rows)==3
   for x,y in zip(r,rows):assert list(x)==[y['success'],y['actual_input'],y['actual_output'],y['ttft'],y['latency']]
   count+=len(r);groups[side]=rows
  for a,b in zip(groups['A'],groups['B']):
   assert all(a[k]==b[k] for k in ['prompt_sha256','text','actual_input','actual_output','finish'])
  a,b=[groups[x] for x in ['A','B']]
  ta,tb=[sum(x['ttft'] for x in rows)/3 for rows in [a,b]]
  da,db=[sum(x['actual_output']-1 for x in rows)/sum(x['latency']-x['ttft'] for x in rows) for rows in [a,b]]
  m={'mode':mode,'tier':n,'A_T':ta,'B_T':tb,'A_D':da,'B_D':db,'ttft_increase_pct':100*(tb/ta-1),'decode_decrease_pct':100*(1-db/da),'ttft_delta_ms':1000*(tb-ta)}
  old=next(x for x in summary['metrics'] if x['mode']==mode and x['nominal_input']==n)
  assert abs(old['ttft_increase_pct']-m['ttft_increase_pct'])<1e-9
  assert abs(old['decode_decrease_pct']-m['decode_decrease_pct'])<1e-9
  result['metrics'].append(m)
result['actual_ab_requests']=count;assert count==60
for mode in ['quality-on','quality-off']:
 e=json.loads((ROOT/'docs/evidence/mtp-position-b1a-20261017'/mode/'exit.json').read_text());assert e['http_output_checks_passed']
 result[mode]=e
assert json.loads((ROOT/'docs/evidence/mtp-position-b1a-20261017/boundaries/exit.json').read_text())['passed']
paths=['CMakeLists.txt','include/q4t/mtp/mtp.h','src/mtp/mtp.cu','tests/mtp_position_metadata_test.cpp','tests/mtp_spec_multi_test.cpp']
patch=subprocess.check_output(['git','diff','--binary','867b8a6','HEAD','--',*paths],cwd=ROOT)
(OUT/'implementation.patch').write_bytes(patch)
index=OUT/'rollback.index'
if index.exists():index.unlink()
env=dict(os.environ,GIT_INDEX_FILE=str(index));git('read-tree','HEAD',env=env)
subprocess.run(['git','apply','--cached','--reverse',str(OUT/'implementation.patch')],cwd=ROOT,env=env,check=True)
tree=git('write-tree',env=env)
remaining=git('diff','--name-only','867b8a6',tree).splitlines()
assert all(x.startswith('docs/') for x in remaining),remaining
result['rollback']={'isolated_index_tree':tree,'implementation_paths':paths,'remaining_differences_from_parent':'docs only','patch_sha256':sha(OUT/'implementation.patch'),'working_tree_code_modified':False}
result['evaluation_sha256']=sha(ROOT/'docs/EVALUATION.md')
(OUT/'audit.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'manifests':result['manifests'],'actual_ab_requests':count,'contracts':result['contracts'],'rollback':result['rollback']},indent=2))
