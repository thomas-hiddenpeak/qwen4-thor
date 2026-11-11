"""Deliver memory-observer isolation only; native model/entrypoint bytes unchanged."""
import json,os,runpy,subprocess,sys,time,uuid
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle,publication
from tools.serve_service import package as p
BASE=ROOT/'.q4t-work/serve-adaptive-profiles-20261111'
OUT=BASE/'memory-release-01';DEFAULT=ROOT/'build';STATE=DEFAULT/'service-native-state'
HELPER=ROOT/'.q4t-work/serve-embeddings-20261111/deliver.py'
state_hashes=runpy.run_path(str(HELPER))['state_hashes']
OLD='9e55e265502feba574ae13e314c06aac3e18dc251edf2e4f267866779eed3be9'

def main():
 p.require(not OUT.exists(),'inspect any prior attempt')
 p.require(not subprocess.check_output(['git','status','--porcelain']),'commit first')
 cpu=p.read(BASE/'memory-01.json');p.require(cpu['exit_code']==0 and cpu['default_unchanged'],'CPU isolation not accepted')
 for name,h in p.read(BASE/'memory-cpu-plan.json')['sources'].items():p.require(p.sha(ROOT/name)==h,'CPU source drift')
 current=publication.status(STATE);old=(DEFAULT/'service').resolve(strict=True)
 p.require(current['ready'] and current['revision']==52 and current['current']['manifest_sha256']==OLD and p.sha(old/'manifest.json')==OLD,'previous publication changed')
 manifest=bundle.verify(old,OLD);base=p.read(old/'engine/manifest.json')
 changes={n for n in bundle.SOURCES if p.sha(ROOT/n)!=p.sha(old/'source'/n)}
 p.require(changes=={'tools/serve_service/memory.py'},'unreviewed control changes')
 native=p.read(old/'native-build.json')
 p.require(all(p.sha(ROOT/n)==h for n,h in native['source_files'].items()),'native source changed')
 metadata=p.read(old/'engine/q4t.release.json')
 p.require(all(p.sha(ROOT/n)==h for n,h in p.read(metadata['tested_source_manifest']).items()),'model source changed')
 before=state_hashes();OUT.mkdir(mode=0o700)
 p.save(OUT/'review.json',dict(changed=sorted(changes),cpu=cpu,engine_unchanged=True,native_unchanged=True,
     reused_actual_business='docs/evidence/serve-native-embedding-business-20261111/acceptance-final.json',
     scope='Runtime missing memory observations cannot signal model. Startup unknown and confirmed critical pressure still protect.14 CPU tests include real owned process; no new GPU required for unchanged model.',
     helper_sha256=p.sha(HELPER),business_state_before=before,previous=current))
 app={k:v for k,v in base['settings']['application_state'].items() if k!='profile'}
 receipt=bundle.prepare(old/'engine/q4t',old/'engine/q4t.release.json',base['model_dir'],
     old/'bin/q4t-guardian',old/'bin/q4t-cuda-probe',None,old/'gateway-policy.json',
     old/'engine/client-policy.json',OUT/'bundle',native_receipt=old/'native-build.json',
     native_ingress=old/'bin/q4t-ingress',capacity_config=dict(version=1),application_state=app,
     embedding_model_dir=Path(base['settings']['native_embedding']['directory']))
 p.save(OUT/'receipt.json',receipt);package=Path(receipt['package']);digest=receipt['manifest_sha256']
 p.require(p.read(package/'engine/manifest.json')['settings']==base['settings'],'settings changed')
 for name in ['engine/q4t','engine/q4t.release.json','gateway-policy.json','engine/client-policy.json',*['bin/'+n for n in bundle.EXECUTABLES]]:
  p.require(p.sha(package/name)==p.sha(old/name),'identity changed: '+name)
 decision=dict(status='PASS_TARGETED_MEMORY_OBSERVATION_ISOLATION',acceptance='ACCEPT_MEMORY_OBSERVATION_ISOLATION',
     source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
     engine_binary_sha256=p.sha(package/'engine/q4t'),manifest_sha256=digest,cpu=cpu,
     runtime_model_evidence_reused=True,new_gpu_starts=0,remaining_master_tasks_open=True)
 p.save(OUT/'decision.json',decision)
 recovery=dict(kind='same_fixed_control_package_republication_anchor',package=str(package),manifest_sha256=digest,
     previous_package=str(old),previous_manifest_sha256=OLD,
     previous_memory_observation_isolation_fixed=False,
     command='q4t service publish --state STATE --default-root ROOT --package ANCHOR --manifest-sha256 ANCHOR_SHA --service-release ANCHOR_SERVICE_RELEASE --expected-manifest-sha256 CURRENT_MANIFEST --expected-revision CURRENT_REVISION --request-id NEW_UUID')
 p.save(OUT/'recovery-anchor.json',recovery)
 service=p.read(DEFAULT/'service.release.json');previous=service['acceptance_record']
 service.update(acceptance=decision['acceptance'],acceptance_record=str(OUT/'decision.json'),acceptance_sha256=p.sha(OUT/'decision.json'),
     previous_acceptance_record=previous,package=str(package),manifest_sha256=digest,
     engine_binary_sha256=receipt['engine_binary_sha256'],engine_release_sha256=p.sha(package/'engine/q4t.release.json'),
     engine_metadata_unchanged=True,control_review=str(OUT/'review.json'),
     rollback_receipt=str(OUT/'recovery-anchor.json'),rollback_sha256=p.sha(OUT/'recovery-anchor.json'),
     memory_observation_isolation_targeted_accepted=True,
     scope='Native text/application APIs, fixed dedicated retrieval, native ingress and self-supervision. Exact current capabilities are frozen in release declarations. Runtime missing memory observations cannot stop healthy models; confirmed critical memory/device protections remain. Adaptive profile switching and all other remaining Serve master tasks continue.')
 p.save(OUT/'service.release.json',service)
 rows=[]
 def run(label,args):
  started=time.monotonic();argv=[str(x) for x in args]
  value=subprocess.run(argv,capture_output=True,stdin=subprocess.DEVNULL,timeout=45)
  (OUT/(label+'.stdout')).write_bytes(value.stdout);(OUT/(label+'.stderr')).write_bytes(value.stderr)
  row=dict(label=label,argv=argv,returncode=value.returncode,seconds=time.monotonic()-started);rows.append(row);p.save(OUT/(label+'.json'),row)
  p.require(value.returncode==0,'failed command: '+label);p.require(state_hashes()==before,'business state changed')
  return json.loads(value.stdout)
 try:
  cli=package/'engine/q4t'
  p.require(run('previous-check',[cli,'service','check','--state',STATE,'--package',old,'--manifest-sha256',OLD])['desired']=='STOPPED','service active')
  for label,entry in [('native',cli),('legacy',package/'bin/q4t')]:
   p.require(run('candidate-'+label+'-check',[entry,'service','check'])['status']=='PASS','package check')
  request=str(uuid.uuid4());p.save(OUT/'publish-request.json',dict(request_id=request,expected_revision=52,previous=OLD,target=digest))
  value=run('publish',[cli,'service','publish','--state',STATE,'--default-root',DEFAULT,'--package',package,
      '--manifest-sha256',digest,'--service-release',OUT/'service.release.json','--expected-manifest-sha256',OLD,'--expected-revision',52,'--request-id',request])
  p.require(value['durable'] and value['revision']==54,'publication failed')
  for action in ['check','status','observe']:
   value=run('default-'+action,[DEFAULT/'q4t','service',action,'--state',STATE])
   p.require(value['status']=='PASS' if action=='check' else value['state']=='STOPPED','default health')
  final=run('publication',[DEFAULT/'q4t','service','publish-status','--state',STATE])
  p.require(final['ready'] and final['revision']==54 and final['current']['manifest_sha256']==digest,'default selection')
  p.save(OUT/'result.json',dict(status='PASS_MEMORY_OBSERVATION_ISOLATION_DELIVERED',publication=final,
      commands=rows,business_state_preserved=state_hashes()==before,engine_unchanged=True,new_gpu_starts=0,
      state='STOPPED',adaptive_profiles_implemented=False,remaining_master_tasks_open=True))
 except BaseException as error:
  p.save(OUT/'result.json',dict(status='FAILED',commands=rows,detail=str(error),category=type(error).__name__))
  raise
 print(json.dumps(dict(status='PASS_MEMORY_OBSERVATION_ISOLATION_DELIVERED',revision=54,commands=len(rows))))

if __name__=='__main__':os.umask(0o077);main()
