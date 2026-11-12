"""Seal accepted N07 engine/control sources with unchanged numeric defaults."""
import hashlib,json,os,subprocess,sys
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from tools.serve_service import package as p,capacity
from tools.serve_recovery import bundle
from tools.serve_progress.prepare import copy_file
BASE=ROOT/'.q4t-work/serve-lifecycle-20261109';OUT=BASE/'release-01'
BUILD=ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build';E=ROOT/'docs/evidence'


def main():
 p.require(not OUT.exists(),'N07 release exists')
 p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit N07 inputs')
 plan=p.read(BASE/'business-02/plan.json');accepted=p.read(E/'serve-lifecycle-business-20261109/decision.json')
 p.require(accepted['status']=='PASS_TARGETED_NATIVE_LIFECYCLE' and accepted['cleanup_confirmed'] and accepted['all_nvml_ok'],'N07 business gate')
 p.require(all(p.sha(ROOT/n)==h for n,h in plan['sources'].items()),'tested N07 source drift')
 p.require(all(p.sha(r['path'])==r['sha256'] for r in plan['protected']),'default drift')
 p.require(p.sha(BUILD/'q4t')==accepted['binary_sha256'],'accepted ELF changed')
 native=p.read(BASE/'business-build-01/native-build.json')
 p.require(all(p.sha(ROOT/n)==h for n,h in native['source_files'].items()),'native source drift')
 for n,k in [('q4t-ingress','ingress_sha256'),('q4t-guardian','guardian_sha256'),('q4t-cuda-probe','cuda_probe_sha256')]:p.require(p.sha(BUILD/n)==native[k],'native ELF changed')
 old=(ROOT/'build/service').resolve(strict=True);old_hash=p.sha(old/'manifest.json')
 p.require(bundle.verify(old,old_hash)['version']==9,'actual N05 default required')
 candidate=Path(plan['package']['package'])
 p.require(all(p.sha(ROOT/n)==p.sha(candidate/'source'/n) for n in bundle.SOURCES),'tested control source drift')
 groups=['serve-lifecycle-capacity-20261109','serve-lifecycle-cli-20261109',
  'serve-lifecycle-publication-20261109','serve-lifecycle-driver-20261109',
  'serve-lifecycle-capacity-fix-20261109','serve-lifecycle-business-20261109']
 indices={}
 for group in groups:
  index=p.read(E/group/'index.json');rows=index.get('files',index)
  p.require(all(p.sha(E/group/n)==(v if isinstance(v,str) else v['sha256']) for n,v in rows.items()),'evidence changed')
  indices[group]=dict(index_sha256=p.sha(E/group/'index.json'),files=len(rows))
 caps=p.read(E/'serve-lifecycle-business-20261109/capabilities.json');config=p.read(E/'serve-lifecycle-business-20261109/configuration.json')
 protocol_sources=p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
 protocol=hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(protocol_sources.items())).encode()).hexdigest()
 p.require(protocol==accepted['protocol_sha256']==caps['protocol_source_sha256']==config['protocol_source_sha256'],'protocol drift')
 OUT.mkdir(mode=0o700);engine=OUT/'engine';engine.mkdir()
 for n in ('q4t','CMakeCache.txt'):copy_file(BUILD/n,engine/n)
 for n in ('q4t','q4t.release.json','service.release.json'):copy_file(ROOT/'build'/n,OUT/'previous'/n)
 runtime=p.read(BASE/'business-build-01/runtime-source.json')
 p.require(all(p.sha(ROOT/n)==h for n,h in runtime.items()),'runtime drift')
 p.save(engine/'runtime-source.json',runtime)
 commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
 control={n:p.sha(ROOT/n) for n in bundle.SOURCES}
 defaults=capacity.normalize(dict(version=1))
 old_settings=p.read(old/'engine/manifest.json')['settings']
 p.require(all(defaults[k]==v for k,v in old_settings['profile'].items() if k!='name'),'numeric default drift')
 p.save(OUT/'control-review.json',dict(source_commit=commit,files=control,native=native,evidence=indices,defaults=plan['protected'],previous_package=str(old),previous_manifest_sha256=old_hash,
  actual_configuration_provenance='Actual N07 custom context/prefill/output/admission/deadline and MTP k3. Default preserves N05 numeric settings and client policy; exact argv/profile CPU and prior N05 actual evidence reused.',
  default_capacity=defaults,rollback='Actual accepted N05 v9 retained. New sealed q4t service publish-rollback selects its immutable generation and preserves current business journals.'))
 decision=dict(status='PASS_TARGETED_NATIVE_LIFECYCLE_DELIVERY',acceptance=accepted['acceptance'],binary_sha256=accepted['binary_sha256'],protocol_sha256=protocol,source_commit=commit,actual_business=accepted,evidence=indices,additional_gpu_tests=0,full_model_quality=False,performance_acceptance=False,long_stability_accepted=False,remaining_master_tasks_open=True)
 p.save(OUT/'decision.json',decision)
 declarations=engine/'declarations';declarations.mkdir()
 p.save(declarations/'capabilities.json',caps);p.save(declarations/'configuration.json',config)
 metadata=p.read(ROOT/'build/q4t.release.json')
 for key in ('validation_only','validation_declaration_basis'):metadata.pop(key,None)
 metadata.update(version='serve-native-lifecycle-20261109',source_commit=commit,binary_sha256=accepted['binary_sha256'],acceptance=accepted['acceptance'],acceptance_decision=str(OUT/'decision.json'),acceptance_decision_sha256=p.sha(OUT/'decision.json'),build_cache=str(engine/'CMakeCache.txt'),build_cache_sha256=p.sha(engine/'CMakeCache.txt'),tested_source_manifest=str(engine/'runtime-source.json'),tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),observed_configuration=config,inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),sha256=p.sha(OUT/'previous/q4t.release.json')),previous_binary=str(OUT/'previous/q4t'),previous_release=str(OUT/'previous/q4t.release.json'),source_note='Native lifecycle/capacity/default-publication targeted acceptance. Inference implementation unchanged except CLI dispatch/source identity; current real JSON/SSE/tool, CLI handler and capacity evidence; no full performance/model-quality claim.')
 metadata['chat_protocol'].update(caps,protocol_sha256=protocol,declaration_evidence='Actual N07 discovery; actual custom capacity is recorded, not mislabeled as default-profile execution.')
 for n in p.declaration_schemas(p.APPLICATION_DECLARATION_PROFILE):
  copy_file(old/'engine/declarations/schemas'/(n+'.json'),declarations/'schemas'/(n+'.json'))
  path=declarations/'schemas'/(n+'.json');metadata['chat_protocol'][n+'_schema']=str(path);metadata['chat_protocol'][n+'_schema_sha256']=p.sha(path)
 metadata['release_declarations']=dict(profile=p.APPLICATION_DECLARATION_PROFILE,directory=str(declarations),binary_sha256=accepted['binary_sha256'],protocol_sha256=protocol,files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
 metadata['native_lifecycle_acceptance']=dict(configurable_capacity=True,independent_entry_capacity=True,unified_native_cli=True,coordinated_publication=True,resumable_short_writes=True,manual_previous_generation_rollback=True,default_profile=defaults,default_profile_is_observed_profile=False,automatic_code_rollback=False)
 metadata['scope']['production']+='; configurable native service lifecycle and persistent coordinated default publication.'
 notices=metadata['third_party_notices'];notices['source_directory']=str(engine/'licenses/structured_output');notices['files']={}
 for file in sorted((ROOT/'third_party/licenses').rglob('*')):
  if file.is_file():
   name=str(file.relative_to(ROOT/'third_party/licenses'));copy_file(file,engine/'licenses'/name)
   if name.startswith('structured_output/'):notices['files'][name[len('structured_output/'):]]=dict(sha256=p.sha(file),text=file.read_text())
 metadata['application_notices']=dict(source_directory=str(engine/'licenses/openai-node'),files={str(f.relative_to(engine/'licenses/openai-node')):p.sha(f) for f in (engine/'licenses/openai-node').rglob('*') if f.is_file()})
 copy_file('/usr/share/doc/libidn2-0/copyright',engine/'licenses/system-libidn2-copyright')
 clients=metadata['client_artifacts']
 for n,h in clients['files'].items():
  p.require(p.sha(ROOT/n)==h,'client changed after prior accepted protocol business')
  copy_file(ROOT/n,OUT/'clients'/n)
 metadata['client_artifacts']=dict(directory=str(OUT/'clients'),files=clients['files'],server_dependency=False)
 p.save(engine/'q4t.release.json',metadata);p.verify_declarations(metadata,required=True)
 p.save(OUT/'native-build.json',native)
 receipt=bundle.prepare(engine/'q4t',engine/'q4t.release.json',p.read(old/'engine/manifest.json')['model_dir'],BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,old/'gateway-policy.json',old/'engine/client-policy.json',OUT/'bundle',native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress',capacity_config=dict(version=1))
 p.save(OUT/'receipt.json',receipt)
 rollback=dict(package=str(old),manifest_sha256=old_hash,engine_binary_sha256=p.sha(old/'engine/q4t'),engine_release_sha256=p.sha(old/'engine/q4t.release.json'),service_release=str(OUT/'previous/service.release.json'),service_release_sha256=p.sha(OUT/'previous/service.release.json'))
 p.save(OUT/'rollback-receipt.json',rollback)
 p.require(bundle.verify(receipt['package'],receipt['manifest_sha256'])['version']==10,'v10 not sealed')
 for n in ('gateway-policy.json','engine/client-policy.json'):p.require(p.sha(OUT/'bundle'/n)==p.sha(old/n),'default client seeds changed')
 settings=p.read(OUT/'bundle/engine/manifest.json')['settings']
 p.require(settings['profile']==defaults and {k:v for k,v in settings.items() if k!='profile'}=={k:v for k,v in old_settings.items() if k!='profile'},'default settings drift')
 p.require(all(p.sha(r['path'])==r['sha256'] for r in plan['protected']),'defaults changed during sealing')
 result=dict(status='PASS_N07_ACCEPTED_PACKAGE_SEALED',package=receipt,rollback=rollback,engine_release_sha256=p.sha(engine/'q4t.release.json'),runtime_sources=len(runtime),protocol_sources=len(protocol_sources),evidence_groups=len(indices),default_changed=False,model_starts=0,chat_requests=0,packaged_cli_pending=True)
 p.save(OUT/'result.json',result);print(json.dumps(result))


if __name__=='__main__':os.umask(0o077);main()
