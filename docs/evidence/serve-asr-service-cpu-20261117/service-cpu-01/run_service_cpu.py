import hashlib,json,os,pathlib,shutil,subprocess,tarfile,time
root=pathlib.Path.cwd();w=root/'.q4t-work/serve-audio-native-20261117';out=w/'service-cpu-01';out.mkdir()
plan=json.loads((w/'service-cpu-plan-01.json').read_text())
def digest(p):
 with open(p,'rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
protected=['build/q4t','build/q4t.release.json','build/service/manifest.json','build/service.release.json','build/service-native-state/ledger.json']
before={p:digest(root/p) for p in protected}
sources=[p for base in ['include','src','cmake'] for p in (root/base).rglob('*') if p.is_file()]+[root/p for p in ['CMakeLists.txt','tests/host/CMakeLists.txt','tests/server_asr_service_test.cpp','tests/server_asr_protocol_test.cpp','tests/asr_tokenizer_test.cpp','tests/embedding_contract_test.cpp','tests/server_chat_sampling_response_test.cpp','tests/server_http_response_test.cpp','tests/test_main.cpp']]
identity={str(p.relative_to(root)):digest(p) for p in sorted(sources)}
(out/'source-identity.json').write_text(json.dumps(identity,indent=2)+'\n')
with tarfile.open(out/'sources.tar.gz','w:gz') as f:
 for p in sorted(sources):f.add(p,arcname=str(p.relative_to(root)))
shutil.copy2(w/'service-cpu-plan-01.json',out/'plan.json')
for build,label in [('host','host'),('engine-build','engine')]:
 b=root/('.q4t-work/serve-file-search-20261112/host' if label=='host' else '.q4t-work/serve-custom-tools-20261108/engine-build')
 g=b/'generated/q4t/server'
 if g.is_dir():shutil.copytree(g,out/('generated-'+label))
scratch=out/'scratch';scratch.mkdir()
env=dict(os.environ,Q4T_ASR_ASSETS=str(w/'assets-01'),Q4T_EMBEDDING_ASSETS=str(root/'.q4t-work/native-embedding-20261111/assets'),Q4T_TEST_OUTPUT_DIR=str(scratch),CUDA_VISIBLE_DEVICES='')
result={'status':'RUNNING','tests':34,'model_constructions':0,'gpu_actions':0,'defaults_before':before,'runs':[]}
start=time.monotonic()
try:
 for group,src in [('host','q4t_host_tests'),('tokenizer','q4t_asr_tokenizer_tests')]:
  binary=w/'validated-binaries'/('q4t_asr_service-'+group+'-01')
  assert not binary.exists();shutil.copy2(root/'.q4t-work/serve-file-search-20261112/host'/src,binary);binary.chmod(0o555)
  listed=subprocess.check_output([str(binary),'--list'],text=True,env=env).splitlines()
  names=plan[group];assert len(names)==len(set(names)) and all(n in listed for n in names)
  required=out/(group+'-required.txt');required.write_text('\n'.join(names)+'\n')
  cmd=[str(binary),'--required-list',str(required)]
  t=time.monotonic()
  with (out/(group+'-stdout.txt')).open('w') as log:p=subprocess.run(cmd,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=max(1,300-(t-start)))
  result['runs'].append({'group':group,'command':cmd,'binary_sha256':digest(binary),'returncode':p.returncode,'seconds':time.monotonic()-t,'tests':len(names)})
  print(group,(out/(group+'-stdout.txt')).read_text(),flush=True)
 result['status']='PASS' if all(x['returncode']==0 for x in result['runs']) else 'FAILED'
except Exception as e:result['status']='FAILED';result['exception']=repr(e)
finally:
 result['seconds']=time.monotonic()-start;result['defaults_after']={p:digest(root/p) for p in protected};result['defaults_unchanged']=result['defaults_after']==before
 result['sources_unchanged']=all(digest(root/p)==h for p,h in identity.items())
 if not result['defaults_unchanged'] or not result['sources_unchanged']:result['status']='FAILED'
 (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');shutil.copy2(__file__,out/'run_service_cpu.py')
print(json.dumps(result));raise SystemExit(0 if result['status']=='PASS' else 1)
