import argparse, hashlib, json, os, pathlib, shutil, subprocess, tarfile, time
root=pathlib.Path.cwd(); w=root/'.q4t-work/serve-audio-native-20261117'
p=argparse.ArgumentParser();p.add_argument('--full',action='store_true');a=p.parse_args()
lease=()
if a.full:
 fd=int(os.environ['Q4T_MANAGED_LEASE_FD']);os.fstat(fd);lease=(fd,)
out=w/('asset-full-01' if a.full else 'asset-cpu-01');out.mkdir()
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  while b:=f.read(1024*1024):h.update(b)
 return h.hexdigest()
binary=w/'validated-binaries/q4t_model_assets-cpu-01'
if not binary.exists():
 shutil.copy2(root/'.q4t-work/serve-file-search-20261112/host/q4t_model_asset_tests',binary);binary.chmod(0o555)
listed=subprocess.check_output([str(binary),'--list'],text=True).splitlines()
full='asr_assets_full_checkpoint_verified_layout_and_metadata_published'
required=[full] if a.full else [n for n in listed if n!=full]
assert len(required)==(1 if a.full else 17),required
(out/'required.txt').write_text('\n'.join(required)+'\n')
sources=[path for base in ['include','src','cmake'] for path in (root/base).rglob('*') if path.is_file()]+[root/x for x in ['CMakeLists.txt','tests/host/CMakeLists.txt','tests/model_asset_test_main.cpp','tests/server_model_assets_test.cpp','tests/server_embedding_assets_test.cpp','tests/router_trace_hash_test.cpp']]
identity={str(p.relative_to(root)):digest(p) for p in sorted(sources)}
(out/'source-identity.json').write_text(json.dumps(identity,indent=2)+'\n')
with tarfile.open(out/'sources.tar.gz','w:gz') as archive:
 for p in sorted(sources): archive.add(p,arcname=str(p.relative_to(root)))
protected=['build/q4t','build/q4t.release.json','build/service/manifest.json','build/service.release.json','build/service-native-state/ledger.json']
before={p:digest(root/p) for p in protected}
scratch=out/'scratch';scratch.mkdir()
env=dict(os.environ,Q4T_ASR_ASSETS=str(w/'assets-01'),Q4T_EMBEDDING_ASSETS=str(root/'.q4t-work/native-embedding-20261111/assets'),Q4T_TEST_OUTPUT_DIR=str(scratch),CUDA_VISIBLE_DEVICES='')
command=[str(binary),'--required-list',str(out/'required.txt')]
result={'command':command,'required':required,'binary_sha256':digest(binary),'defaults_before':before,'gpu_actions':0,'model_constructions':0,'full_checkpoint_reads':int(a.full),'status':'RUNNING'}
start=time.monotonic()
try:
 with (out/'stdout.txt').open('w') as log: r=subprocess.run(command,cwd=root,env=env,pass_fds=lease,stdout=log,stderr=subprocess.STDOUT,timeout=120 if a.full else 180)
 result['exit_code']=r.returncode;result['status']='PASS' if r.returncode==0 else 'FAILED'
except Exception as e: result['status']='FAILED';result['error']=str(e)
finally:
 result['seconds']=time.monotonic()-start;result['defaults_after']={p:digest(root/p) for p in protected};result['defaults_unchanged']=result['defaults_after']==before
 if not result['defaults_unchanged']:result['status']='FAILED'
 (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');shutil.copy2(__file__,out/'run_asset_checks.py')
print(json.dumps(result));print((out/'stdout.txt').read_text())
raise SystemExit(0 if result['status']=='PASS' else 1)
