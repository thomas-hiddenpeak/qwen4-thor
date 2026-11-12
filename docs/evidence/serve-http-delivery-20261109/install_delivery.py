"""Deliver accepted N05 under ownership with a verified retained v8 rollback."""
from contextlib import ExitStack
import fcntl,json,os,shutil,stat,subprocess,sys,uuid
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from tools.serve_service.package import read,save,sha,require
from tools.serve_recovery import bundle,ledger,access_store,quota_store,usage_store
BASE=ROOT/'.q4t-work/serve-http-20261109';RELEASE=BASE/'release-02';OUT=BASE/'delivery-01'
STATE=ROOT/'build/service-native-state'


def replace(source,target):
 temp=target.with_name(target.name+'.http-new');require(not os.path.lexists(temp),'replacement pending')
 with source.open('rb') as src,temp.open('xb') as dst:
  shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
 temp.chmod(0o755 if target.name=='q4t' else 0o600);require(sha(temp)==sha(source),'copy identity')
 os.replace(temp,target);fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)


def point(package):
 temp=ROOT/'build/service.http-new';require(not os.path.lexists(temp),'pointer pending')
 temp.symlink_to(package);os.replace(temp,ROOT/'build/service');fd=os.open(ROOT/'build',os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)


def main():
 require(not OUT.exists(),'delivery output exists');OUT.mkdir(mode=0o700)
 receipt=read(RELEASE/'receipt.json');package=Path(receipt['package']);digest=receipt['manifest_sha256']
 rollback=read(RELEASE/'rollback-receipt.json');old=Path(rollback['package']);old_digest=rollback['manifest_sha256']
 manifest=bundle.verify(package,digest);bundle.verify(old,old_digest)
 reviewed=read(RELEASE/'control-review.json');passed=read(BASE/'packaged-01/result.json')
 require(passed['status']=='PASS_N05_ACCEPTED_PACKAGED_CLI' and passed['manifest_sha256']==digest,'actual packaged CLI gate')
 require(all(sha(ROOT/n)==h for n,h in reviewed['files'].items()),'control source drift')
 decision=dict(status='PASS_N05_DEFAULT_DELIVERY_PREREQUISITES',acceptance='ACCEPT_TARGETED_NATIVE_HTTP_TRANSPORT',
  package_manifest_sha256=digest,business=dict(path=str(ROOT/'docs/evidence/serve-http-business-20261109/decision.json'),sha256=sha(ROOT/'docs/evidence/serve-http-business-20261109/decision.json')),
  packaged=dict(path=str(BASE/'packaged-01/result.json'),sha256=sha(BASE/'packaged-01/result.json')),
  performance_acceptance=False,long_stability_accepted=False,model_starts=0,remaining_master_tasks_open=True)
 save(OUT/'decision.json',decision)
 request=str(uuid.uuid4());revert=str(uuid.uuid4());save(OUT/'operation-ids.json',dict(select=request,rollback=revert))
 def cli(label,operation,*args,target=None):
  command=([sys.executable,'-I','-B',str(package/'source/tools/serve_recovery/service.py'),operation,
      '--package',str(old),'--manifest-sha256',old_digest] if target else
      [str(package/'bin/q4t'),'service',operation])
  command+=['--state',str(STATE),*map(str,args)]
  result=subprocess.run(command,capture_output=True,timeout=25)
  (OUT/(label+'.stdout')).write_bytes(result.stdout);(OUT/(label+'.stderr')).write_bytes(result.stderr)
  save(OUT/(label+'.json'),dict(argv=command,exit_code=result.returncode,model_starts=0))
  require(result.returncode==0,'installed command '+label+' failed')
  return json.loads(result.stdout)
 with ExitStack() as stack:
  lock=Path(manifest['lock_path']);fd=os.open(lock,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC);stack.callback(os.close,fd)
  held=os.fstat(fd);named=lock.stat(follow_symlinks=False)
  require(stat.S_ISREG(held.st_mode) and held.st_uid==os.geteuid() and held.st_nlink==1 and
          (held.st_dev,held.st_ino)==(named.st_dev,named.st_ino),'ownership identity')
  fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  require(all(sha(row['path'])==row['sha256'] for row in reviewed['defaults']),'default drift before delivery')
  require((ROOT/'build/service').resolve()==old,'current package changed')
  before=read(STATE/'ledger.json');core=before['state'];ledger.validate(core,core['service_id'])
  require(before['sha256']==ledger.digest(core) and core['desired']=='STOPPED' and not any(core['active'].values()) and not (STATE/'ledger.pending').exists(),'service not durably stopped')
  core_sha=sha(STATE/'ledger.json');families=[access_store.Store,quota_store.Store,usage_store.Store]
  journals={w.directory_name:sha(STATE/w.directory_name/'policy.json') for w in families}
  for n in ('gateway-policy.json','engine/client-policy.json'):require(sha(package/n)==sha(old/n),'seed policy changed')
  backup=OUT/'rollback';backup.mkdir()
  for n in ('q4t','q4t.release.json','service.release.json'):
   shutil.copyfile(ROOT/'build'/n,backup/n);(backup/n).chmod(0o500 if n=='q4t' else 0o400)
  old_meta=read(ROOT/'build/service.release.json')
  undo=[sys.executable,'-I','-B',str(package/'source/tools/serve_recovery/service.py'),'rollback',
        '--package',str(old),'--manifest-sha256',old_digest,'--state',str(STATE),
        '--expected-manifest-sha256',digest,'--request-id','NEW_UUID']
  undo_receipt=dict(package=str(old),manifest_sha256=old_digest,service_id=core['service_id'],
      old_engine_sha256=sha(old/'engine/q4t'),old_engine_release_sha256=sha(old/'engine/q4t.release.json'),
      sealed_selection_tool=str(package/'source/tools/serve_recovery/service.py'),
      sealed_selection_tool_sha256=sha(package/'source/tools/serve_recovery/service.py'),
      selection_command=undo,files={n:sha(backup/n) for n in ('q4t','q4t.release.json','service.release.json')},
      preserve_current_journals=True,requires_stopped_and_global_ownership=True,
      procedure='Run the pinned NEW module to select the retained v8 target; then restore only these default engine/metadata files and service symlink. Never restore service.json, keys, limits, usage or core journals. New v9 package path must remain retained for this command. This is manual rollback, not R06 automatic recovery.')
  save(backup/'receipt.json',undo_receipt)
  metadata=dict(old_meta)
  metadata.update(package=str(package),manifest_sha256=digest,acceptance=decision['acceptance'],
      acceptance_record=str(OUT/'decision.json'),acceptance_sha256=sha(OUT/'decision.json'),
      previous_acceptance_record=old_meta['acceptance_record'],engine_binary_sha256=sha(package/'engine/q4t'),
      engine_release_sha256=sha(package/'engine/q4t.release.json'),ingress_sha256=sha(package/'bin/q4t-ingress'),
      guardian_sha256=sha(package/'bin/q4t-guardian'),control_review=str(RELEASE/'control-review.json'),
      engine_default_replaced=True,engine_metadata_unchanged=False,state_ledger_preserved=True,
      core_state_values_preserved=True,ledger_envelope_upgraded=False,native_http_targeted_accepted=True,
      connection_reuse=True,chunked_requests=True,authenticated_continue=True,trusted_proxy_optional=True,
      path_prefix_optional=True,client_artifacts=str(RELEASE/'clients'),gpu_starts_for_install=0,automatically_started=False,
      rollback_receipt=str(backup/'receipt.json'),rollback_sha256=sha(backup/'receipt.json'),
      scope='Accepted native HTTP connection reuse/chunked/100/SSE, explicit trusted proxy/prefix, Python/SDK clients; native/optional-front real business. Default retains original loopback policy, client keys/limits and all mutable state.',
      limitations=['Remaining OpenAI inference/application tasks stay active in the master plan.',
          'N07 unified CLI/profiles and atomic publication remain required.',
          'R05/R06 automatic downgrade/older-code recovery remain required; this is a verified manual rollback.',
          'NVML-free cold start, physical resource isolation and whole-host recovery remain active.',
          'Long stability remains user-run; TLS/CORS/proxy options stay deployment-selected.'])
  save(OUT/'service.release.json',metadata)
  try:
   cli('select','deploy','--expected-manifest-sha256',old_digest,'--request-id',request)
   replace(package/'engine/q4t',ROOT/'build/q4t');replace(package/'engine/q4t.release.json',ROOT/'build/q4t.release.json')
   point(package);replace(OUT/'service.release.json',ROOT/'build/service.release.json')
   bundle.verify((ROOT/'build/service').resolve(),digest)
   require(cli('observe','observe')['state']=='STOPPED','installed not stopped')
   require(cli('selection','deployment-status')['current']['package']==str(package),'installed descriptor mismatch')
   for name in ('access','limits','usage'):cli(name,name+'-status')
   require(sha(STATE/'ledger.json')==core_sha,'core ledger bytes changed')
   require(all(sha(STATE/n/'policy.json')==h for n,h in journals.items()),'client journals changed')
  except BaseException:
   try:
    desc=read(STATE/'service.json')
    if desc['package']==str(package):cli('failed-rollback','rollback','--expected-manifest-sha256',digest,'--request-id',revert,target=old)
    require(read(STATE/'service.json')['package']==str(old),'deployment needs resume')
    for n in ('q4t','q4t.release.json','service.release.json'):replace(backup/n,ROOT/'build'/n)
    point(old);save(OUT/'rollback-applied.json',dict(actual_previous_code=True,mutable_state_preserved=True))
   except BaseException:save(OUT/'resume-required.json',dict(select=request,rollback=revert,do_not_restore_mutable_state=True))
   raise
  result=dict(status='PASS_N05_NATIVE_HTTP_DEFAULT_DELIVERY',package=str(package),manifest_sha256=digest,
      engine_binary_sha256=sha(ROOT/'build/q4t'),engine_release_sha256=sha(ROOT/'build/q4t.release.json'),
      service_release_sha256=sha(ROOT/'build/service.release.json'),ingress_sha256=sha(package/'bin/q4t-ingress'),
      service_id=core['service_id'],ledger_sha256=core_sha,core_state_sha256=ledger.digest(core),
      rollback_receipt=str(backup/'receipt.json'),rollback_sha256=sha(backup/'receipt.json'),
      rollback_package=str(old),ledger_bytes_preserved=True,client_journal_bytes_preserved=True,
      desired='STOPPED',model_starts=0,chat_requests=0,remaining_master_tasks_open=True)
  save(OUT/'result.json',result);print(json.dumps(result))

if __name__=='__main__':os.umask(0o077);main()
