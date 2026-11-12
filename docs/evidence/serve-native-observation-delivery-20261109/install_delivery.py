"""Install accepted service-only observation/retention; preserve engine and STOPPED ledger."""
from contextlib import ExitStack
import fcntl,json,os,shutil,stat,subprocess,sys,time,traceback
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path.cwd().resolve();sys.path.insert(0,str(ROOT))
from tools.serve_service.package import read,save,sha,require
from tools.serve_recovery import bundle,ledger
BASE=ROOT/'.q4t-work/serve-native-observation-20261109';RELEASE=BASE/'release-02';OUT=BASE/'delivery-01';STATE=ROOT/'build/service-native-state'

def replace(source,target):
 temp=target.with_name(target.name+'.observation-new');require(not temp.exists(),'pending replacement')
 with source.open('rb') as src,temp.open('xb') as dst:
  shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
 temp.chmod(0o600);require(sha(temp)==sha(source),'replacement identity');os.replace(temp,target)
 fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)

def point(package):
 temp=ROOT/'build/service.observation-new';require(not os.path.lexists(temp),'pending symlink')
 temp.symlink_to(package);os.replace(temp,ROOT/'build/service')
 fd=os.open(ROOT/'build',os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)

def main():
 require(not OUT.exists(),'delivery exists');OUT.mkdir(mode=0o700)
 review=read(RELEASE/'control-review.json');expected=review['default']
 require(all(sha(ROOT/n)==h for n,h in review['files'].items()),'control source drift')
 receipt=read(RELEASE/'receipt.json');package=Path(receipt['package'])
 manifest=bundle.verify(package,receipt['manifest_sha256'])
 actual=read(BASE/'packaged-03/result.json')
 require(actual['status']=='PASS_ACTUAL_V6_PACKAGED_CLI_EXPORT_RETENTION' and actual['manifest_sha256']==receipt['manifest_sha256'],'packaged acceptance')
 require(read(BASE/'bundle-cpu-01/result.json')['exit_code']==0,'bundle CPU')
 decision=dict(status='ACCEPT_NATIVE_OBSERVATION_PROTECTED_RETENTION',manifest_sha256=receipt['manifest_sha256'],source_commit=review['source_commit'],engine_binary_sha256=expected['q4t'],engine_release_sha256=expected['q4t.release.json'],engine_unchanged=True,evidence=review['evidence'],packaged=dict(path=str(BASE/'packaged-03/result.json'),sha256=sha(BASE/'packaged-03/result.json')),bundle_cpu=dict(path=str(BASE/'bundle-cpu-01/result.json'),sha256=sha(BASE/'bundle-cpu-01/result.json')),controller_advisory_change_only=True,previous_business_reused=True,production_runs_removed=0,additional_gpu_tests=0,full_model_quality_run=False,performance_acceptance=False,remaining_master_tasks_open=True)
 save(RELEASE/'decision.json',decision)
 rollback=OUT/'rollback';rollback.mkdir(mode=0o700)
 files={'service.release.json':ROOT/'build/service.release.json','service.json':STATE/'service.json'}
 with ExitStack() as stack:
  for path in (Path(manifest['lock_path']),STATE/'guardian.lock',STATE/'writer.lock'):
   fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC);stack.callback(os.close,fd);info=os.fstat(fd)
   require(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and info.st_nlink==1,'ownership lock')
   fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  require(all(sha(ROOT/'build'/n)==h for n,h in expected.items()),'default changed')
  previous=(ROOT/'build/service').resolve(strict=True);old=read(files['service.release.json'])
  bundle.verify(previous,old['manifest_sha256'])
  envelope=read(STATE/'ledger.json');value=envelope['state'];ledger.validate(value,value['service_id'])
  require(envelope['sha256']==ledger.digest(value) and value['desired']=='STOPPED' and not any(value['active'].values()) and not (STATE/'ledger.pending').exists(),'durable stopped boundary')
  for n in ('gateway-policy.json','engine/client-policy.json'):
   require(sha(package/n)==sha(previous/n),'policy/key/quota drift')
  for name,path in files.items():
   shutil.copyfile(path,rollback/name);(rollback/name).chmod(0o400);require(sha(path)==sha(rollback/name),'rollback copy')
  save(rollback/'receipt.json',dict(package=str(previous),manifest_sha256=old['manifest_sha256'],service_id=value['service_id'],ledger_sha256=expected['service-native-state/ledger.json'],engine_unchanged=dict(binary_sha256=expected['q4t'],release_sha256=expected['q4t.release.json']),files={n:sha(rollback/n) for n in files},procedure='With GPU/Guardian/writer locks and durably STOPPED ledger, restore these two records and repoint build/service to previous package. Engine, keys, quota and ledger remain unchanged.'))
  descriptor=read(files['service.json']);descriptor.update(package=str(package),manifest_sha256=receipt['manifest_sha256']);save(OUT/'service.json',descriptor)
  new=dict(old);new.update(package=str(package),manifest_sha256=receipt['manifest_sha256'],acceptance=decision['status'],acceptance_record=str(RELEASE/'decision.json'),acceptance_sha256=sha(RELEASE/'decision.json'),previous_acceptance_record=old['acceptance_record'],control_review=str(RELEASE/'control-review.json'),rollback_receipt=str(rollback/'receipt.json'),rollback_sha256=sha(rollback/'receipt.json'),native_observation_targeted_accepted=True,protected_retention_targeted_accepted=True,engine_default_replaced=False,engine_metadata_unchanged=True,gpu_starts_for_install=0,state_ledger_preserved=True,automatically_started=False,scope='Native read-only service observations and bounded isolated exporter; archived and ownership-verified run retirement with legacy/boot migration. Previously accepted engine/protocols and native recovery unchanged. All remaining master tasks continue.')
  save(OUT/'service.release.json',new)
  try:
   point(package);replace(OUT/'service.json',files['service.json']);replace(OUT/'service.release.json',files['service.release.json'])
   bundle.verify((ROOT/'build/service').resolve(),receipt['manifest_sha256'])
   for name,args in [('help',['--help']),('observe',['observe','--state',str(STATE)]),('metrics',['metrics','--state',str(STATE)])]:
    cmd=[str(ROOT/'build/service/bin/q4t'),'service',*args];p=subprocess.run(cmd,capture_output=True,timeout=15)
    (OUT/(name+'.stdout')).write_bytes(p.stdout);(OUT/(name+'.stderr')).write_bytes(p.stderr);save(OUT/(name+'.json'),dict(argv=cmd,exit_code=p.returncode))
    require(p.returncode==0,'installed '+name)
    if name=='observe':
     status=json.loads(p.stdout);require(status['state']=='STOPPED' and status['business_ready'] is False,'installed stopped observation')
   for n in ('q4t','q4t.release.json','service-native-state/ledger.json'):require(sha(ROOT/'build'/n)==expected[n],'protected default changed')
  except BaseException:
   for name,path in files.items():replace(rollback/name,path)
   point(previous);save(OUT/'rollback-applied.json',dict(applied=True));raise
 return dict(status='PASS_NATIVE_OBSERVATION_RETENTION_DEFAULT_DELIVERY',package=str(package),manifest_sha256=receipt['manifest_sha256'],engine_binary_sha256=expected['q4t'],engine_release_sha256=expected['q4t.release.json'],service_release_sha256=sha(files['service.release.json']),service_id=value['service_id'],ledger_sha256=expected['service-native-state/ledger.json'],ledger_preserved=True,desired='STOPPED',rollback_receipt=str(rollback/'receipt.json'),rollback_sha256=sha(rollback/'receipt.json'),model_starts=0,chat_requests=0,production_runs_removed=0,policy_keys_quota_unchanged=True,remaining_master_tasks_open=True)

if __name__=='__main__':
 start=time.monotonic()
 try:result=main()
 except Exception:
  if OUT.exists():(OUT/'failure.txt').write_text(traceback.format_exc())
  raise
 result['seconds']=time.monotonic()-start;save(OUT/'result.json',result);print(json.dumps(result))
