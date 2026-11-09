"""Seal accepted N05 package while retaining the real accepted N04 rollback."""
import hashlib,json,os,shutil,subprocess,sys
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from tools.serve_service import package as p
from tools.serve_recovery import bundle
from tools.serve_progress.prepare import copy_file
BASE=ROOT/'.q4t-work/serve-http-20261109';OUT=BASE/'release-01'
BUILD=ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build';E=ROOT/'docs/evidence'


def main():
 p.require(not OUT.exists(),'release exists')
 p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit release inputs')
 plan=p.read(BASE/'business-02/plan.json');accepted=p.read(E/'serve-http-business-20261109/decision.json')
 p.require(accepted['status']=='PASS_TARGETED_NATIVE_HTTP_TRANSPORT' and accepted['lease_status']=='COMPLETED' and accepted['all_nvml_ok'],'business acceptance')
 p.require(all(p.sha(ROOT/n)==h for n,h in plan['sources'].items()),'tested sources changed')
 p.require(all(p.sha(r['path'])==r['sha256'] for r in plan['protected']),'defaults changed')
 p.require(p.sha(BUILD/'q4t')==accepted['binary_sha256'],'accepted engine changed')
 native=p.read(BASE/'final-build-01/native-build.json')
 p.require(all(p.sha(ROOT/n)==h for n,h in native['source_files'].items()),'native sources changed')
 for n,k in [('q4t-ingress','ingress_sha256'),('q4t-guardian','guardian_sha256'),('q4t-cuda-probe','cuda_probe_sha256')]:p.require(p.sha(BUILD/n)==native[k],'native ELF changed')
 old=(ROOT/'build/service').resolve(strict=True);old_hash=p.sha(old/'manifest.json')
 p.require(bundle.verify(old,old_hash)['version']==8,'previous v8 required')
 candidate=Path(plan['package']['package'])
 p.require(all(p.sha(ROOT/n)==p.sha(candidate/'source'/n) for n in bundle.SOURCES),'control source changed after actual business')
 groups=['serve-http-framing-20261109','serve-http-session-20261109','serve-http-proxy-20261109','serve-http-package-20261109','serve-http-driver-20261109','serve-http-prefix-fix-20261109','serve-http-business-20261109']
 indices={}
 for group in groups:
  index=p.read(E/group/'index.json')
  p.require(all(p.sha(E/group/n)==v['sha256'] for n,v in index.items()),'evidence changed')
  indices[group]=dict(index_sha256=p.sha(E/group/'index.json'),files=len(index))
 caps=p.read(E/'serve-http-business-20261109/capabilities.json');config=p.read(E/'serve-http-business-20261109/configuration.json')
 protocol_sources=p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
 protocol=hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(protocol_sources.items())).encode()).hexdigest()
 p.require(protocol==accepted['protocol_sha256']==caps['protocol_source_sha256']==config['protocol_source_sha256'],'protocol mismatch')
 OUT.mkdir(mode=0o700);engine=OUT/'engine';engine.mkdir()
 for n in ('q4t','CMakeCache.txt'):copy_file(BUILD/n,engine/n)
 for n in ('q4t','q4t.release.json','service.release.json'):copy_file(ROOT/'build'/n,OUT/'previous'/n)
 runtime=p.read(BASE/'discovery-build-01/runtime-source.json')
 p.require(all(p.sha(ROOT/n)==h for n,h in runtime.items()),'runtime source drift')
 p.save(engine/'runtime-source.json',runtime)
 commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
 control={n:p.sha(ROOT/n) for n in bundle.SOURCES}
 p.save(OUT/'control-review.json',dict(source_commit=commit,files=control,native=native,evidence=indices,defaults=plan['protected'],previous_package=str(old),previous_manifest_sha256=old_hash,
  actual_configuration_provenance='Actual TLS policy v4 with prefix and synthetic keys. Default retains existing plain-loopback policy v2, keys/limits/settings; corresponding transport CPU and prior default evidence reused.',
  rollback='Retained actual accepted v8 package. Use the new v9 sealed service module to select this old target; old verifier cannot parse a v9 CURRENT package. Never restore mutable journals.'))
 decision=dict(status='PASS_TARGETED_NATIVE_HTTP_DELIVERY',acceptance=accepted['acceptance'],binary_sha256=accepted['binary_sha256'],protocol_sha256=protocol,source_commit=commit,actual_business=accepted,evidence=indices,additional_gpu_tests=0,full_model_quality=False,performance_acceptance=False,long_stability_accepted=False,remaining_master_tasks_open=True)
 p.save(OUT/'decision.json',decision)
 declarations=engine/'declarations';declarations.mkdir()
 p.save(declarations/'capabilities.json',caps);p.save(declarations/'configuration.json',config)
 schemas=dict(config='serve-config-v1.schema.json',message='serve-message-profile-v3.schema.json',preflight='serve-preflight-v1.schema.json',reasoning='serve-reasoning-v1.schema.json',response='serve-transport-v1.schema.json',stop='serve-stop-v1.schema.json',thinking='serve-thinking-v1.schema.json',completion_request='serve-completion-request-v1.schema.json',completion_response='serve-completion-transport-v1.schema.json',application='serve-application-v1.schema.json',openapi='serve-openapi-bundle-v1.json')
 for n,source in schemas.items():copy_file(ROOT/'docs'/source,declarations/'schemas'/(n+'.json'))
 metadata=p.read(ROOT/'build/q4t.release.json')
 metadata.update(version='serve-native-http-20261109',source_commit=commit,binary_sha256=accepted['binary_sha256'],acceptance=accepted['acceptance'],acceptance_decision=str(OUT/'decision.json'),acceptance_decision_sha256=p.sha(OUT/'decision.json'),build_cache=str(engine/'CMakeCache.txt'),build_cache_sha256=p.sha(engine/'CMakeCache.txt'),tested_source_manifest=str(engine/'runtime-source.json'),tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),observed_configuration=config,inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),sha256=p.sha(OUT/'previous/q4t.release.json')),previous_binary=str(OUT/'previous/q4t'),previous_release=str(OUT/'previous/q4t.release.json'),source_note='Native persistent HTTP/chunked/authenticated continue, trusted proxy/prefix and clients; actual core/TLS/SDK/tool/Responses/Completions/optional proxy business accepted. All remaining master tasks active.')
 metadata['chat_protocol'].update(caps,protocol_sha256=protocol,declaration_evidence='Actual N05 discovery; unchanged inference/application evidence inherited.')
 for n in schemas:
  path=declarations/'schemas'/(n+'.json');metadata['chat_protocol'][n+'_schema']=str(path);metadata['chat_protocol'][n+'_schema_sha256']=p.sha(path)
 metadata['release_declarations']=dict(profile=p.APPLICATION_DECLARATION_PROFILE,directory=str(declarations),binary_sha256=accepted['binary_sha256'],protocol_sha256=protocol,files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
 metadata['http_transport_acceptance']=dict(keep_alive=True,chunked_requests=True,authenticated_continue=True,complete_sse_framing=True,trusted_proxy=True,path_prefix=True,official_sdk_prefix=True,default_profile_is_observed_profile=False)
 metadata['scope']['production']+='; native persistent HTTP, explicit trusted proxy/prefix and optional frontends.'
 notices=metadata['third_party_notices'];notices['source_directory']=str(engine/'licenses/structured_output');notices['files']={}
 for file in sorted((ROOT/'third_party/licenses').rglob('*')):
  if file.is_file():
   name=str(file.relative_to(ROOT/'third_party/licenses'));copy_file(file,engine/'licenses'/name)
   if name.startswith('structured_output/'):notices['files'][name[len('structured_output/'):]]=dict(sha256=p.sha(file),text=file.read_text())
 metadata['application_notices']=dict(source_directory=str(engine/'licenses/openai-node'),files={str(f.relative_to(engine/'licenses/openai-node')):p.sha(f) for f in (engine/'licenses/openai-node').rglob('*') if f.is_file()})
 copy_file('/usr/share/doc/libidn2-0/copyright',engine/'licenses/system-libidn2-copyright')
 client_files=['tools/examples/chat_client.py','tools/examples/deadline_http.py','tools/examples/js/chat_client.mjs','tools/examples/js/node_transport.mjs','tools/serve_protocol/sdk.mjs','tools/serve_protocol/js_client.py','tools/serve_protocol/run_sdk.mjs']
 for n in client_files:copy_file(ROOT/n,OUT/'clients'/n)
 metadata['client_artifacts']=dict(directory=str(OUT/'clients'),files={n:p.sha(ROOT/n) for n in client_files},server_dependency=False)
 p.save(engine/'q4t.release.json',metadata);p.verify_declarations(metadata,required=True)
 native['business_evidence']=dict(path=str(E/'serve-http-business-20261109/decision.json'),sha256=p.sha(E/'serve-http-business-20261109/decision.json'));p.save(OUT/'native-build.json',native)
 receipt=bundle.prepare(engine/'q4t',engine/'q4t.release.json',p.read(old/'engine/manifest.json')['model_dir'],BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,old/'gateway-policy.json',old/'engine/client-policy.json',OUT/'bundle',native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress')
 p.save(OUT/'receipt.json',receipt)
 rollback=dict(package=str(old),manifest_sha256=old_hash,engine_binary_sha256=p.sha(old/'engine/q4t'),engine_release_sha256=p.sha(old/'engine/q4t.release.json'),service_release=str(OUT/'previous/service.release.json'),service_release_sha256=p.sha(OUT/'previous/service.release.json'))
 p.save(OUT/'rollback-receipt.json',rollback)
 p.require(bundle.verify(receipt['package'],receipt['manifest_sha256'])['version']==9,'v9 not sealed')
 for n in ('gateway-policy.json','engine/client-policy.json'):p.require(p.sha(OUT/'bundle'/n)==p.sha(old/n),'default seeds changed')
 p.require(p.read(OUT/'bundle/engine/manifest.json')['settings']==p.read(old/'engine/manifest.json')['settings'],'default managed settings changed')
 p.require(all(p.sha(r['path'])==r['sha256'] for r in plan['protected']),'defaults changed while sealing')
 p.save(OUT/'result.json',dict(status='PASS_N05_ACCEPTED_PACKAGE_SEALED',package=receipt,rollback=rollback,engine_release_sha256=p.sha(engine/'q4t.release.json'),runtime_sources=len(runtime),protocol_sources=len(protocol_sources),evidence_groups=len(indices),default_changed=False,model_starts=0,chat_requests=0,packaged_cli_pending=True))
 print(json.dumps(p.read(OUT/'result.json')))

if __name__=='__main__':os.umask(0o077);main()
