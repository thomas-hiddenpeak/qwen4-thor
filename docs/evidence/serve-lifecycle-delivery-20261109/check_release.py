"""Frozen N07 packaged-entry checks, without model starts or publication."""
import hashlib,json,os,subprocess,sys,time
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from tools.serve_service.package import read,save,sha,require
BASE=ROOT/'.q4t-work/serve-lifecycle-20261109';RELEASE=BASE/'release-01';OUT=BASE/'packaged-01'
STATE=ROOT/'build/service-native-state'


def main():
 require(not OUT.exists(),'package check output exists');OUT.mkdir(mode=0o700)
 receipt=read(RELEASE/'receipt.json');package=Path(receipt['package']);cli=package/'engine/q4t'
 old=read(RELEASE/'rollback-receipt.json');old_args=['--package',old['package'],'--manifest-sha256',old['manifest_sha256'],'--state',str(STATE)]
 commands=[('help',[str(cli),'service','--help']),
  ('new-package-check',[str(cli),'service','check']),
  ('old-selected-check',[str(cli),'service','check',*old_args]),
  ('old-selected-status',[str(cli),'service','status',*old_args]),
  ('publication-status',[str(cli),'service','publish-status','--state',str(STATE)]),
  ('sealed-legacy-entry',[str(package/'bin/q4t'),'service','check'])]
 clients=RELEASE/'clients'
 commands += [('python-client-import',[sys.executable,'-I','-B','-c',
  'import sys;sys.path.insert(0,sys.argv[1]);import chat_client,deadline_http,project_records;print("PASS_CLIENT_IMPORTS")',str(clients/'tools/examples')]),
  ('node-client-import',['node','--input-type=module','-e',
   'await import(process.argv[1]); await import(process.argv[2]); console.log("PASS_CLIENT_IMPORTS");',
   (clients/'tools/examples/js/chat_client.mjs').as_uri(),(clients/'tools/examples/js/project_profile.mjs').as_uri()])]
 save(OUT/'plan.json',dict(version=1,commands=[dict(label=n,argv=c) for n,c in commands],model_starts=0,chat_requests=0,scope='Actual accepted ELF, legacy entry and unchanged client snapshot imports; readonly live default'))
 before={str(p.relative_to(STATE)):sha(p) for p in STATE.rglob('*.json')}
 save(OUT/'state-before-hashes.json',before)
 rows=[]
 try:
  for label,command in commands:
   start=time.monotonic();result=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,timeout=30)
   (OUT/(label+'.stdout')).write_bytes(result.stdout);(OUT/(label+'.stderr')).write_bytes(result.stderr)
   row=dict(label=label,argv=command,exit_code=result.returncode,seconds=time.monotonic()-start);rows.append(row);save(OUT/(label+'.json'),row)
   require(result.returncode==0,'packaged entry failed: '+label)
   if label.endswith('check') or label=='sealed-legacy-entry':require(json.loads(result.stdout)['status']=='PASS','check result differs')
   if label=='old-selected-status':require(json.loads(result.stdout)['state']=='STOPPED','default not stopped')
   if label=='publication-status':require(json.loads(result.stdout)['status']=='UNPUBLISHED','unexpected prior default publication')
   if label=='help':require(b'publish-rollback' in result.stdout,'publication CLI absent')
  require({str(p.relative_to(STATE)):sha(p) for p in STATE.rglob('*.json')}==before,'readonly checks changed state')
  status='PASS_N07_ACCEPTED_PACKAGED_CLI'
 except BaseException:
  save(OUT/'result.json',dict(status='FAILED',commands=rows,model_starts=0,chat_requests=0));raise
 save(OUT/'result.json',dict(status=status,commands=rows,manifest_sha256=receipt['manifest_sha256'],engine_binary_sha256=receipt['engine_binary_sha256'],state_preserved=True,model_starts=0,chat_requests=0))
 print(json.dumps(dict(status=status,commands=len(rows),model_starts=0,chat_requests=0)))


if __name__=='__main__':os.umask(0o077);main()
