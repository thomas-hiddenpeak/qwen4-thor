"""Actual accepted launcher/rollback modules; only STOPPED CPU operations."""
import json,os,subprocess,sys,time,uuid
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from tools.serve_service.package import read,save,sha,require
from tools.serve_recovery import bundle,access_store,quota_store,usage_store
BASE=ROOT/'.q4t-work/serve-http-20261109';RELEASE=BASE/'release-02';OUT=BASE/'packaged-01'


def main():
 require(not OUT.exists(),'packaged output exists');OUT.mkdir(mode=0o700)
 selected=read(RELEASE/'receipt.json');old=read(RELEASE/'rollback-receipt.json')
 package=Path(selected['package']);previous=Path(old['package']);state=OUT/'state'
 bundle.verify(package,selected['manifest_sha256']);bundle.verify(previous,old['manifest_sha256'])
 counter=0;rows=[];start=time.monotonic()
 def cli(label,owner,operation,*args,target=None):
  nonlocal counter
  counter+=1
  if target:
   command=[sys.executable,'-I','-B',str(package/'source/tools/serve_recovery/service.py'),operation,
            '--package',str(target['package']),'--manifest-sha256',target['manifest_sha256']]
  else:command=[str(owner/'bin/q4t'),'service',operation]
  command += ['--state',str(state),*map(str,args)]
  result=subprocess.run(command,capture_output=True,timeout=25)
  (OUT/(label+'.stdout')).write_bytes(result.stdout);(OUT/(label+'.stderr')).write_bytes(result.stderr)
  row=dict(label=label,argv=command,exit_code=result.returncode);save(OUT/(label+'.json'),row);rows.append(row)
  require(result.returncode==0,'packaged CLI failed '+label+': '+result.stderr.decode()[:300])
  return json.loads(result.stdout)
 try:
  cli('01-old-init',previous,'init')
  before=read(state/'ledger.json')['state']
  families=[access_store.Store,quota_store.Store,usage_store.Store]
  snapshots={v.directory_name:sha(state/v.directory_name/'policy.json') for v in families}
  cli('02-new-deploy',package,'deploy','--expected-manifest-sha256',old['manifest_sha256'],'--request-id',uuid.uuid4())
  require(cli('03-new-observe',package,'observe')['state']=='STOPPED','new selected not stopped')
  for i,name in enumerate(['access','limits','usage'],4):cli(f'{i:02}-new-'+name,package,name+'-status')
  cli('07-old-rollback',package,'rollback','--expected-manifest-sha256',selected['manifest_sha256'],'--request-id',uuid.uuid4(),target=old)
  require(cli('08-old-observe',previous,'observe')['state']=='STOPPED','old rollback not stopped')
  cli('09-new-redeploy',package,'deploy','--expected-manifest-sha256',old['manifest_sha256'],'--request-id',uuid.uuid4())
  require(cli('10-new-current',package,'deployment-status')['current']['package']==str(package),'final selection mismatch')
  require(read(state/'ledger.json')['state']==before,'core state/budgets changed')
  require(all(sha(state/name/'policy.json')==digest for name,digest in snapshots.items()),'client journal changed')
  clients=RELEASE/'clients'
  code='import sys;sys.path.insert(0,'+repr(str(clients))+');from tools.serve_protocol import js_client;assert str(js_client.__file__).startswith('+repr(str(clients))+')'
  js='await import('+json.dumps((clients/'tools/examples/js/chat_client.mjs').as_uri())+');await import('+json.dumps((clients/'tools/examples/js/project_profile.mjs').as_uri())+');await import('+json.dumps((clients/'tools/serve_protocol/sdk.mjs').as_uri())+');'
  for name,command in [('python-client-import',[sys.executable,'-I','-B','-c',code]),('node-client-import',['node','--input-type=module','-e',js])]:
   result=subprocess.run(command,capture_output=True,timeout=15)
   (OUT/(name+'.stdout')).write_bytes(result.stdout);(OUT/(name+'.stderr')).write_bytes(result.stderr)
   save(OUT/(name+'.json'),dict(argv=command,exit_code=result.returncode));require(result.returncode==0,'client import '+name)
  result=dict(status='PASS_N05_ACCEPTED_PACKAGED_CLI',cli_commands=counter,client_imports=2,seconds=time.monotonic()-start,
    manifest_sha256=selected['manifest_sha256'],rollback_manifest_sha256=old['manifest_sha256'],
    core_values_preserved=True,client_journals_preserved=True,state='STOPPED',gpu_operations=0,model_starts=0,chat_requests=0)
  save(OUT/'result.json',result);print(json.dumps(result))
 except BaseException:
  import traceback
  save(OUT/'result.json',dict(status='FAILED',failure=traceback.format_exc(),cli_commands=counter,gpu_operations=0));raise

if __name__=='__main__':os.umask(0o077);main()
