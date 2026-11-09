"""Seal a service-only v6 candidate around the accepted, unchanged engine."""
import json,shutil,subprocess,sys,time,traceback
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path.cwd().resolve();sys.path.insert(0,str(ROOT))
from tools.serve_service.package import read,save,sha,require
from tools.serve_recovery import bundle
BASE=ROOT/'.q4t-work/serve-native-observation-20261109';OUT=BASE/'release-01'

def main():
 require(not OUT.exists(),'candidate exists');OUT.mkdir(mode=0o700)
 require(not subprocess.check_output(['git','status','--porcelain']),'commit sources before seal')
 old=(ROOT/'build/service').resolve();old_release=read(ROOT/'build/service.release.json')
 manifest=bundle.verify(old,old_release['manifest_sha256'])
 require(manifest['version']==5,'unexpected default')
 default={n:sha(ROOT/'build'/n) for n in ('q4t','q4t.release.json','service.release.json','service-native-state/service.json','service-native-state/ledger.json')}
 require(default['q4t']=='1ff729a08e75e399a30e090d10a5ce76eb80b394cccb98c02ffff542b056dad6','engine drift')
 require(default['q4t.release.json']=='f44634f5235c9555a06c20e57fbe86d1606f57ba7b4095e875b2d71ba31b38fc','metadata drift')
 old_native=read(old/'native-build.json')
 require(all(sha(ROOT/n)==h for n,h in old_native['source_files'].items()),'native source drift')
 runtime=read(read(ROOT/'build/q4t.release.json')['tested_source_manifest'])
 require(all(sha(ROOT/n)==h for n,h in runtime.items()),'accepted runtime source drift')
 groups=('serve-native-observation-20261109','serve-run-retention-20261109','serve-retention-epoch-20261109','serve-retention-lineage-20261109','serve-cache-progress-delivery-20261109','serve-cache-io-business-20261109','serve-progress-business-20261109')
 evidence={}
 for group in groups:
  p=ROOT/'docs/evidence'/group;index=read(p/'index.json')
  rows=index.get('files',index);rows=({r['path']:r['sha256'] for r in rows} if isinstance(rows,list) else rows)
  require(all(sha(p/n)==(h['sha256'] if isinstance(h,dict) else h) for n,h in rows.items()),'evidence changed '+group)
  evidence[group]=dict(index_sha256=sha(p/'index.json'),files=len(rows))
 control={n:sha(ROOT/n) for n in bundle.SOURCES}
 changed={n:dict(before=sha(old/'source'/n) if (old/'source'/n).exists() else None,after=h) for n,h in control.items() if not (old/'source'/n).exists() or sha(old/'source'/n)!=h}
 require(set(changed)=={'tools/serve_recovery/'+n+'.py' for n in ('bundle','acceptance','controller','service','retention','retention_epoch')}|{'tools/serve_host/native.py','tools/serve_host/native_exporter.py'},'unexpected control change')
 save(OUT/'control-review.json',dict(previous_package=str(old),source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),files=control,changed=changed,default=default,evidence=evidence,engine_sources_unchanged=len(runtime),native_sources_unchanged=len(old_native['source_files']),controller_change='Accept v3 with optional advisory retention_context; model/recovery behavior unchanged.',engine_metadata_unchanged=True,gpu_tests_reused=True))
 shutil.copyfile(old/'native-build.json',OUT/'native-build.json');(OUT/'native-build.json').chmod(0o400)
 model=read(old/'engine/manifest.json')['model_dir']
 receipt=bundle.prepare(ROOT/'build/q4t',ROOT/'build/q4t.release.json',model,old/'bin/q4t-guardian',old/'bin/q4t-cuda-probe',None,old/'gateway-policy.json',old/'engine/client-policy.json',OUT/'bundle',native_receipt=OUT/'native-build.json',native_ingress=old/'bin/q4t-ingress')
 save(OUT/'receipt.json',receipt)
 value=bundle.verify(receipt['package'],receipt['manifest_sha256'])
 require(value['version']==6,'version')
 for n in ('gateway-policy.json','engine/client-policy.json','engine/q4t','engine/q4t.release.json','bin/q4t-ingress','bin/q4t-guardian','bin/q4t-cuda-probe'):
  require(sha(OUT/'bundle'/n)==sha(old/n),'invariant bytes changed '+n)
 require(read(OUT/'bundle/engine/manifest.json')['settings']==read(old/'engine/manifest.json')['settings'],'managed settings changed')
 require(all(sha(ROOT/'build'/n)==h for n,h in default.items()),'default changed while packaging')
 return dict(status='PASS_V6_SERVICE_CANDIDATE_SEALED',manifest_sha256=receipt['manifest_sha256'],changed_control=len(changed),engine_sources_unchanged=len(runtime),model_starts=0,chat_requests=0,default_changed=False)

if __name__=='__main__':
 start=time.monotonic()
 try:
  result=main()
 except Exception:
  if OUT.exists():(OUT/'failure.txt').write_text(traceback.format_exc())
  raise
 result['seconds']=time.monotonic()-start;save(OUT/'result.json',result);print(json.dumps(result))
