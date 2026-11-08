from pathlib import Path
import fcntl, json, os, shutil, subprocess, sys
sys.dont_write_bytecode=True
ROOT=Path.cwd();sys.path[:0]=[str(ROOT),str(ROOT/'tools/evalscope')]
from tools.serve_recovery import bundle, store
from tools.serve_service import package as pkg
from tools.evalscope import gpu_exclusive as guard
read=pkg.read;save=pkg.save;sha=pkg.sha;require=pkg.require
base=ROOT/'.q4t-work/serve-tool-stream-20261108';release=base/'release-01'
plan=read(base/'business-01/plan.json');receipt=read(release/'receipt.json');new=Path(receipt['package']);manifest=bundle.verify(new,receipt['manifest_sha256'])
state=ROOT/'build/service-native-state';old=(ROOT/'build/service').resolve();desc=read(state/'service.json')
require(desc['package']==str(old) and desc['manifest_sha256']==sha(old/'manifest.json'),'service changed')
bundle.verify(old,desc['manifest_sha256'])
for name,digest in plan['protected'].items():require(sha(name)==digest,'default changed')
output=base/'delivery-01';output.mkdir(mode=0o700);rollback=output/'rollback';rollback.mkdir(mode=0o700)
lock_path=guard.common_lock()
def copy_atomic(source,target):
 tmp=target.with_name('.'+target.name+'.tool-stream-'+str(os.getpid()))
 require(not tmp.exists(),'stale copy temp')
 try:
  shutil.copy2(source,tmp)
  with tmp.open('rb') as f:os.fsync(f.fileno())
  os.replace(tmp,target)
  fd=os.open(target.parent,os.O_DIRECTORY|os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
 finally:tmp.unlink(missing_ok=True)
def point(target):
 tmp=ROOT/'build'/('.service-tool-stream-'+str(os.getpid()))
 require(not tmp.exists() and not tmp.is_symlink(),'stale symlink')
 os.symlink(str(target),tmp);os.replace(tmp,ROOT/'build/service')
 fd=os.open(ROOT/'build',os.O_DIRECTORY|os.O_RDONLY)
 try:os.fsync(fd)
 finally:os.close(fd)
with lock_path.open('a+b') as gpu, (state/'guardian.lock').open('a+b') as service_lock:
 fcntl.flock(gpu,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(service_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 before=sha(state/'ledger.json')
 with store.Store(state,desc['service_id']) as journal:
  require(journal.state['desired']=='STOPPED' and all(v is None for v in journal.state['active'].values()),'service not durably stopped')
  require(sha(state/'ledger.json')==before,'ledger changed while opening')
  originals={'q4t':ROOT/'build/q4t','q4t.release.json':ROOT/'build/q4t.release.json','service.release.json':ROOT/'build/service.release.json','service.json':state/'service.json'}
  for name,path in originals.items():shutil.copy2(path,rollback/name);require(sha(path)==sha(rollback/name),'rollback mismatch')
  old_record=read(rollback/'service.release.json')
  save(rollback/'receipt.json',dict(package=str(old),manifest_sha256=sha(old/'manifest.json'),service_id=desc['service_id'],ledger_sha256=before,files={n:sha(rollback/n) for n in originals},procedure='Acquire GPU, Guardian and writer locks with durable STOPPED state. Restore the four files to their original paths and repoint build/service. Preserve the actual ledger and recovery budgets; never initialize/reset ledger.'))
  service_meta=dict(old_record)
  service_meta.update(acceptance='ACCEPT_TARGETED_TOOL_STREAM',acceptance_record=str(release/'decision.json'),acceptance_sha256=sha(release/'decision.json'),package=str(new),manifest_sha256=receipt['manifest_sha256'],engine_binary_sha256=sha(new/'engine/q4t'),engine_release_sha256=sha(new/'engine/q4t.release.json'),rollback_receipt=str(rollback/'receipt.json'),rollback_sha256=sha(rollback/'receipt.json'),previous_acceptance_record=old_record['acceptance_record'],unchanged_control_evidence=str(release/'unchanged-control.json'),scope='Accepted allowed tools and incremental function argument streams; native ingress/TLS/CORS/recovery bytes and policy/keys unchanged. Legacy/custom and remaining master tasks continue.',sampling_profile='q4t-sampling-v1',sampling_targeted_accepted=True,prediction_implemented=True,prediction_profile="q4t-prediction-v1",prediction_targeted_accepted=True,tool_stream_targeted_accepted=True,tool_stream_profile="q4t-tool-stream-v1")
  save(output/'service.release.json',service_meta)
  new_desc=dict(desc,package=str(new),manifest_sha256=receipt['manifest_sha256']);save(output/'service.json',new_desc)
  changed=False
  try:
   changed=True
   copy_atomic(new/'engine/q4t',ROOT/'build/q4t');copy_atomic(new/'engine/q4t.release.json',ROOT/'build/q4t.release.json')
   copy_atomic(output/'service.release.json',ROOT/'build/service.release.json');copy_atomic(output/'service.json',state/'service.json');point(new)
   require(sha(ROOT/'build/q4t')==plan['binary_sha256'],'installed binary mismatch')
   for label,args,wanted in [('help',['--help'],0),('status',['status','--state',str(state)],1)]:
    cmd=[str(ROOT/'build/service/bin/q4t'),'service',*args]
    with (output/(label+'.stdout')).open('xb') as stdout,(output/(label+'.stderr')).open('xb') as stderr:
     completed=subprocess.run(cmd,stdout=stdout,stderr=stderr,timeout=10)
    save(output/(label+'.json'),dict(argv=cmd,returncode=completed.returncode,model_starts=0,chat_requests=0))
    require(completed.returncode==wanted,'installed '+label+' failed')
   require(read(output/'status.stdout')['state']=='UNKNOWN','stopped service status unexpectedly live')
   require(sha(state/'ledger.json')==before,'ledger or budgets changed')
   require(sha(new/'gateway-policy.json')==sha(old/'gateway-policy.json') and sha(new/'engine/client-policy.json')==sha(old/'engine/client-policy.json'),'policy/key drift')
   save(output/'result.json',dict(status='PASS_TOOL_STREAM_DEFAULT_INSTALL',package=str(new),manifest_sha256=receipt['manifest_sha256'],engine_binary_sha256=sha(ROOT/'build/q4t'),engine_release_sha256=sha(ROOT/'build/q4t.release.json'),service_release_sha256=sha(ROOT/'build/service.release.json'),service_id=desc['service_id'],ledger_sha256=before,ledger_preserved=True,desired='STOPPED',rollback_receipt=str(rollback/'receipt.json'),rollback_sha256=sha(rollback/'receipt.json'),model_starts=0,chat_requests=0,policy_keys_quota_unchanged=True))
  except BaseException:
   if changed:
    for name,path in originals.items():copy_atomic(rollback/name,path)
    point(old)
   raise
print(json.dumps(read(output/'result.json'),indent=2))
