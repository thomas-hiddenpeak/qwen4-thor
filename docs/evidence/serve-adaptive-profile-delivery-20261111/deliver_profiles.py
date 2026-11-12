"""Publish accepted adaptive profiles; keep engine bytes and business journals."""
import json,os,runpy,shutil,subprocess,sys,time,uuid
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle,profile_policy,publication
from tools.serve_service import package as p
BASE=ROOT/'.q4t-work/serve-adaptive-profiles-20261111'
OUT=BASE/'profile-release-01';TEST=BASE/'managed-01'
DEFAULT=ROOT/'build';STATE=DEFAULT/'service-native-state'
OLD='b2bd575ace695b1fcbedc3ab965e7312be39ad14e1a9dfa1da326848a026ea60'
state_hashes=runpy.run_path(str(ROOT/'.q4t-work/serve-embeddings-20261111/deliver.py'))['state_hashes']

def main():
 p.require(not OUT.exists(),'preserve any previous publication attempt')
 p.require(not subprocess.check_output(['git','status','--porcelain']),'commit first')
 accepted=p.read(TEST/'acceptance-final.json');plan=p.read(TEST/'plan.json')
 p.require(accepted['status']=='ACCEPT_MANAGED_ADAPTIVE_PROFILES' and accepted['all_queries_ok'] and not accepted['env_abort'] and all(accepted['postflight'].values()),'managed business failed')
 p.require(all(p.sha(ROOT/n)==h for n,h in plan['sources'].items()),'accepted source drift')
 p.require(all(p.sha(row['path'])==row['sha256'] for row in plan['protected']),'default drift')
 tested=Path(plan['package']['package']);tested_manifest=bundle.verify(tested,plan['package']['manifest_sha256'])
 current=publication.status(STATE);old=(DEFAULT/'service').resolve(strict=True)
 p.require(current['ready'] and current['revision']==54 and current['current']['manifest_sha256']==OLD and p.sha(old/'manifest.json')==OLD,'publication changed')
 bundle.verify(old,OLD);base=p.read(old/'engine/manifest.json')
 p.require(all(p.sha(ROOT/n)==tested_manifest['files']['source/'+n] for n in bundle.SOURCES),'sealed control source drift')
 native=p.read(tested/'native-build.json')
 p.require(all(p.sha(ROOT/n)==h for n,h in native['source_files'].items()),'native source drift')
 before=state_hashes();OUT.mkdir(mode=0o700)
 for name in ['q4t','q4t.release.json','service.release.json']:
  target=OUT/'previous'/name;target.parent.mkdir(exist_ok=True);shutil.copyfile((DEFAULT/name).resolve(strict=True),target)
 p.save(OUT/'review.json',dict(source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),previous=current,business_state_before=before,actual_managed_acceptance=accepted,engine_unchanged=True,
    code_matches_accepted_bundle=True,only_qualification_policy_difference=dict(tested=tested_manifest['recovery_profile_policy'],delivered=profile_policy.DEFAULTS),
    default_policy_evidence='docs/evidence/serve-adaptive-profile-state-20261111/README.md',
    rollback_scope='Retain this qualified package for exact same-version recovery. Older-package automatic fallback and qualified registry migration still require R06; old rev54 cannot silently drop the new profile journal.',
    native_metadata_review='Refresh service-level Guardian/ingress/probe identities from actual bundle, correcting inherited stale Guardian metadata.',new_gpu_starts=0))
 app={k:v for k,v in base['settings']['application_state'].items() if k!='profile'}
 receipt=bundle.prepare(old/'engine/q4t',old/'engine/q4t.release.json',base['model_dir'],tested/'bin/q4t-guardian',tested/'bin/q4t-cuda-probe',None,old/'gateway-policy.json',old/'engine/client-policy.json',OUT/'bundle',native_receipt=tested/'native-build.json',native_ingress=tested/'bin/q4t-ingress',capacity_config=dict(version=1),application_state=app,embedding_model_dir=Path(base['settings']['native_embedding']['directory']),recovery_profiles=BASE/'qualification-01/qualified/registry.json',recovery_profile_policy=profile_policy.DEFAULTS)
 p.save(OUT/'receipt.json',receipt);target=Path(receipt['package']);digest=receipt['manifest_sha256'];manifest=bundle.verify(target,digest)
 p.require(p.read(target/'engine/manifest.json')['settings']==base['settings'],'declared capacity changed')
 for n in ['engine/q4t','engine/q4t.release.json','gateway-policy.json','engine/client-policy.json']:
  p.require(p.sha(target/n)==p.sha(old/n),'existing identity changed: '+n)
 p.require(manifest['recovery_profiles']==tested_manifest['recovery_profiles'],'qualification changed')
 decision=dict(status='PASS_TARGETED_ADAPTIVE_PROFILE_RECOVERY',acceptance='ACCEPT_ADAPTIVE_PROFILE_RECOVERY',engine_binary_sha256=receipt['engine_binary_sha256'],manifest_sha256=digest,actual_business=accepted,source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),new_gpu_starts=0,default_policy=profile_policy.DEFAULTS,remaining_master_tasks_open=True,full_performance=False,long_stability_accepted=False)
 p.save(OUT/'decision.json',decision)
 recovery=dict(kind='qualified_same_version_republication_anchor',package=str(target),manifest_sha256=digest,previous_package=str(old),previous_manifest_sha256=OLD,previous_automated_profiles_compatible=False,automatic_older_code_fallback_implemented=False,
    command='q4t service publish --state STATE --default-root ROOT --package ANCHOR --manifest-sha256 ANCHOR_SHA --service-release ANCHOR_RELEASE --expected-manifest-sha256 CURRENT_SHA --expected-revision CURRENT_REVISION --request-id NEW_UUID')
 p.save(OUT/'recovery-anchor.json',recovery)
 release=p.read(OUT/'previous/service.release.json');previous=release['acceptance_record']
 release.update(acceptance=decision['acceptance'],acceptance_record=str(OUT/'decision.json'),acceptance_sha256=p.sha(OUT/'decision.json'),previous_acceptance_record=previous,package=str(target),manifest_sha256=digest,engine_binary_sha256=receipt['engine_binary_sha256'],engine_release_sha256=p.sha(target/'engine/q4t.release.json'),engine_metadata_unchanged=True,guardian_sha256=p.sha(target/'bin/q4t-guardian'),ingress_sha256=p.sha(target/'bin/q4t-ingress'),cuda_probe_sha256=p.sha(target/'bin/q4t-cuda-probe'),recovery_bundle_profile=manifest['profile'],control_review=str(OUT/'review.json'),rollback_receipt=str(OUT/'recovery-anchor.json'),rollback_sha256=p.sha(OUT/'recovery-anchor.json'),adaptive_profiles_targeted_accepted=True,adaptive_profiles_policy=profile_policy.DEFAULTS,adaptive_profiles_registry=manifest['recovery_profiles'],scope='Qualified primary/light MTP-only automatic RAM downgrade/recovery with persistent intents, original Guardian budgets and exact client usage handoff. Existing native APIs/ingress/retrieval retained. Remaining master tasks, including automatic older-version fallback, continue.')
 release['limitations']=[s.replace('R05/R06 validated downgrade and automatic older-code recovery remain required.','R06 automatic older-code recovery and qualified registry migration remain required.') for s in release['limitations']]
 p.save(OUT/'service.release.json',release)
 rows=[]
 def run(label,args):
  p.require(len(rows)<9,'delivery command bound')
  argv=[str(x) for x in args];start=time.monotonic();r=subprocess.run(argv,capture_output=True,stdin=subprocess.DEVNULL,timeout=45)
  (OUT/(label+'.stdout')).write_bytes(r.stdout);(OUT/(label+'.stderr')).write_bytes(r.stderr)
  row=dict(label=label,argv=argv,returncode=r.returncode,seconds=time.monotonic()-start);rows.append(row);p.save(OUT/(label+'.json'),row)
  p.require(r.returncode==0,'failed delivery: '+label);p.require(state_hashes()==before,'existing business journals changed')
  return json.loads(r.stdout)
 try:
  cli=target/'engine/q4t'
  p.require(run('previous-check',[cli,'service','check','--state',STATE,'--package',old,'--manifest-sha256',OLD])['desired']=='STOPPED','service is active')
  for label,entry in [('native',cli),('legacy',target/'bin/q4t')]:p.require(run('candidate-'+label+'-check',[entry,'service','check'])['status']=='PASS','candidate check')
  rid=str(uuid.uuid4());p.save(OUT/'publish-request.json',dict(request_id=rid,expected_revision=54,previous=OLD,target=digest))
  value=run('publish',[cli,'service','publish','--state',STATE,'--default-root',DEFAULT,'--package',target,'--manifest-sha256',digest,'--service-release',OUT/'service.release.json','--expected-manifest-sha256',OLD,'--expected-revision',54,'--request-id',rid])
  p.require(value['durable'] and value['revision']==56,'publication failed')
  for action in ['check','status','observe']:
   value=run('default-'+action,[DEFAULT/'q4t','service',action,'--state',STATE])
   p.require(value['status']=='PASS' if action=='check' else value['state']=='STOPPED','default health')
  profiles=run('default-profiles',[DEFAULT/'q4t','service','profiles-status','--state',STATE])
  p.require(profiles['enabled'] and profiles['selected']=='primary' and profiles['configuration']==profile_policy.DEFAULTS,'default profile selection')
  final=run('publication',[DEFAULT/'q4t','service','publish-status','--state',STATE])
  p.require(final['ready'] and final['revision']==56 and final['current']['manifest_sha256']==digest,'default selection')
  p.save(OUT/'result.json',dict(status='PASS_ADAPTIVE_PROFILE_RECOVERY_DELIVERED',commands=rows,publication=final,profiles=profiles,business_state_preserved=state_hashes()==before,engine_unchanged=True,new_gpu_starts=0,state='STOPPED',remaining_master_tasks_open=True))
 except BaseException as error:
  p.save(OUT/'result.json',dict(status='FAILED',commands=rows,detail=str(error),category=type(error).__name__));raise
 print(json.dumps(dict(status='PASS_ADAPTIVE_PROFILE_RECOVERY_DELIVERED',revision=56,commands=len(rows),manifest_sha256=digest)))

if __name__=='__main__':os.umask(0o077);main()
