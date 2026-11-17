import hashlib,json,os,pathlib,shutil,subprocess,sys,tarfile,time
root=pathlib.Path.cwd();w=root/'.q4t-work/serve-audio-native-20261117';out=w/sys.argv[1];out.mkdir()
plan=json.loads((w/(sys.argv[2] if len(sys.argv)>2 else 'recovery-cpu-plan-01.json')).read_text())
def digest(p):
 with open(p,'rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
protected=['build/q4t','build/q4t.release.json','build/service/manifest.json','build/service.release.json','build/service-native-state/ledger.json']
before={p:digest(root/p) for p in protected}
sources=[p for base in ['tools/serve_recovery','tools/serve_service','tools/serve_responses','tools/serve_ops','tools/serve_media','tools/evalscope'] for p in (root/base).glob('*.py')]+[root/'tests/fixtures/asr/asr_zh.wav']
identity={str(p.relative_to(root)):digest(p) for p in sorted(set(sources))}
(out/'source-identity.json').write_text(json.dumps(identity,indent=2)+'\n')
with tarfile.open(out/'sources.tar.gz','w:gz') as archive:
 for p in identity:archive.add(root/p,arcname=p)
(out/'plan.json').write_text(json.dumps(plan,indent=2)+'\n');shutil.copy2(__file__,out/'run.py')
scratch=out/'scratch';scratch.mkdir()
env=dict(os.environ,Q4T_TEST_OUTPUT_DIR=str(scratch),CUDA_VISIBLE_DEVICES='',PYTHONDONTWRITEBYTECODE='1')
result={'status':'RUNNING','tests':plan['count'],'gpu_actions':0,'defaults_before':before}
start=time.monotonic()
try:
 assert len(plan['tests'])==plan['count']==len(set(plan['tests']))
 cmd=[sys.executable,'-B','-m','unittest','-v',*plan['tests']]
 with (out/'stdout.txt').open('w') as log:proc=subprocess.run(cmd,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=plan['seconds'])
 result.update(command=cmd,returncode=proc.returncode,status='PASS' if proc.returncode==0 else 'FAILED')
 print((out/'stdout.txt').read_text(),flush=True)
except Exception as e:result.update(status='FAILED',exception=repr(e))
finally:
 result['seconds']=time.monotonic()-start;result['defaults_after']={p:digest(root/p) for p in protected};result['defaults_unchanged']=result['defaults_after']==before
 result['sources_unchanged']=all(digest(root/p)==h for p,h in identity.items())
 if not result['defaults_unchanged'] or not result['sources_unchanged']:result['status']='FAILED'
 (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k not in ('command','defaults_before','defaults_after')}));raise SystemExit(0 if result['status']=='PASS' else 1)
