from pathlib import Path
import copy, hashlib, json, os, re, shutil, subprocess, sys
sys.dont_write_bytecode=True
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle
from tools.serve_service import package as pkg
sys.path.insert(0,str(ROOT/'tools/evalscope'))
from tools.evalscope import gpu_exclusive as guard
base=ROOT/'.q4t-work/serve-legacy-tools-20261108';business=base/'business-01';build=base/'engine-build'
read=pkg.read;save=pkg.save;sha=pkg.sha;require=pkg.require
plan=read(business/'plan.json');result=read(business/'result.json');cleanup=read(business/'cleanup.json');outer=read(business/'lease/exit.json')
require(result['status']=='PASS_LEGACY_TOOLS_BUSINESS' and result['counts']['starts']==1 and result['counts']['chats']==7,'business failed')
require(outer['status']=='COMPLETED' and outer['child_returncode']==0,'lease did not pass')
require(not cleanup['remaining'] and cleanup['model_exit']==0 and cleanup['ports_released'],'cleanup failed')
for name,digest in plan['sources'].items():
 require(sha(ROOT/name)==digest,'source drift '+name)
 require(hashlib.sha256(subprocess.check_output(['git','show',plan['source_commit']+':'+name])).hexdigest()==digest,'Git identity '+name)
for name,digest in plan['protected'].items():require(sha(name)==digest,'default changed')
require(sha(build/'q4t')==plan['binary_sha256']==result['binary_sha256'],'ELF differs')
for name,identity in cleanup['owned'].items():
 current=guard.processes().get(int(name))
 require(current is None or current['start_ticks']!=identity['start_ticks'],'owned process survives')
queries=[json.loads(line) for line in (business/'lease/nvml-queries.jsonl').read_text().splitlines()]
require(queries and all(v['status']=='OK' for v in queries),'NVML unsuccessful')
events=[json.loads(line) for line in (business/'lease/events.jsonl').read_text().splitlines()]
require(events[0]['event']=='lease_acquired' and events[-1]['event']=='lease_released','lease bounds')
for folder in ('build-01','native-build-01'):
 for log in (base/folder).glob('*.log'):require(not re.search(r'\b(?:warning|error):',log.read_text()),'build warnings '+str(log))
cpu=read(base/'cpu-01/result.json')
require(all(cpu[k]['exit_code']==0 for k in ('host','shapes','python','schema','sources')),'CPU failed')
require(all(row['exit_code']==0 for row in read(base/'cpu-02/result.json').values()),'CPU repair/bridge failed')
release=base/'release-01';release.mkdir(mode=0o700)
old=(ROOT/'build/service').resolve();old_meta=read(ROOT/'build/q4t.release.json')
snapshot=base/'source'
for name,digest in plan['sources'].items():
 p=snapshot/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p);require(sha(p)==digest,'copy drift')
engine=release/'engine';engine.mkdir();previous=release/'previous';previous.mkdir()
for name in ('q4t','q4t.release.json'):shutil.copy2(ROOT/'build'/name,previous/name)
shutil.copy2(build/'q4t',engine/'q4t');shutil.copyfile(build/'CMakeCache.txt',engine/'CMakeCache.txt')
runtime={n:d for n,d in plan['sources'].items() if n.startswith(('src/','include/','cmake/')) or n=='CMakeLists.txt'}
save(engine/'runtime-source.json',runtime)
proto_sources=read(build/'generated/q4t/server/chat_protocol_sources.json')
for name,digest in proto_sources.items():require(runtime[name]==digest,'embedded protocol source mismatch')
protocol=hashlib.sha256(''.join(n+':'+d+'\n' for n,d in sorted(proto_sources.items())).encode()).hexdigest()
caps=read(business/'capabilities.json');require(protocol==caps['protocol_source_sha256'],'protocol digest mismatch')
control={n:sha(ROOT/n) for n in bundle.SOURCES}
for n,d in control.items():require(sha(old/'source'/n)==d,'control runtime changed')
save(release/'unchanged-control.json',dict(previous_package=str(old),source_files=control,source_count=len(control),reused_acceptance=read(ROOT/'build/service.release.json')['acceptance_record']))
native=dict(version=1,source_files={n:sha(ROOT/n) for n in sorted(bundle.NATIVE_SOURCES)})
for target,key in [('q4t-guardian','guardian_sha256'),('q4t-ingress','ingress_sha256')]:
 require(sha(build/target)==sha(old/'bin'/target),'native byte drift')
 native[key]=sha(build/target)
native['cuda_probe_sha256']=sha(old/'bin/q4t-cuda-probe')
native['provenance']=dict(native_build=str(base/'native-build-01/build.log'),guardian_and_ingress_byte_identical=True,cuda_probe_reused_from=str(old/'native-build.json'),cuda_probe_source_unchanged=sha(ROOT/'src/service/cuda_probe.cu')==read(old/'native-build.json')['source_files']['src/service/cuda_probe.cu'])
require(native['provenance']['cuda_probe_source_unchanged'],'probe source changed')
save(release/'native-build.json',native)
decision=dict(status='PASS_TARGETED_LEGACY_TOOLS_DELIVERY',acceptance='ACCEPT_TARGETED_LEGACY_TOOLS',binary_sha256=plan['binary_sha256'],protocol_sha256=protocol,source_commit=plan['source_commit'],source_files=len(plan['sources']),runtime_sources=len(runtime),source_snapshot=str(snapshot),model_starts=1,chat_requests=7,driver_seconds=result['seconds'],lease_seconds=events[-1]['monotonic']-events[0]['monotonic'],nvml_queries=len(queries),nvml_all_ok=True,recorded_processes_exited=True,ports_free=True,native_byte_identical=True,cuda_probe_reused=native['cuda_probe_sha256'],cpu=dict(cpp=31,schema_native=53,python_clients=9,js=6,driver=3),performance_run=False,full_model_quality_run=False,business=str(business/'result.json'),business_sha256=sha(business/'result.json'),lease_sha256=sha(business/'lease/exit.json'),inherited_engine=sha(previous/'q4t'),inherited_service_manifest=sha(old/'manifest.json'),model_kernels_unchanged=True,remaining_master_tasks_open=True,legacy_tools_targeted_accepted=True)
save(release/'decision.json',decision)
declarations=engine/'declarations';(declarations/'schemas').mkdir(parents=True)
for dst,src in [('capabilities.json','capabilities.json'),('configuration.json','config.json')]:shutil.copyfile(business/src,declarations/dst)
for name,source in dict(config='serve-config-v1.schema.json',message='serve-message-profile-v3.schema.json',preflight='serve-preflight-v1.schema.json',reasoning='serve-reasoning-v1.schema.json',response='serve-transport-v1.schema.json',stop='serve-stop-v1.schema.json',thinking='serve-thinking-v1.schema.json').items():
 p=ROOT/'docs'/source
 require(p.is_file(),'missing schema '+str(p));shutil.copyfile(p,declarations/'schemas'/(name+'.json'))
meta=copy.deepcopy(old_meta)
meta.update(version='serve-legacy-tools-20261108',binary_sha256=plan['binary_sha256'],source_commit=plan['source_commit'],source_note='Legacy functions/function_call share normalization, template/grammar/preflight and original error paths; JSON/SSE return the legacy dialect and clients publish role=function results only after a successful terminal and EOF. Custom and all remaining master tasks continue.',build_cache=str(engine/'CMakeCache.txt'),build_cache_sha256=sha(engine/'CMakeCache.txt'),tested_source_manifest=str(engine/'runtime-source.json'),tested_source_manifest_sha256=sha(engine/'runtime-source.json'),acceptance=decision['acceptance'],acceptance_decision=str(release/'decision.json'),acceptance_decision_sha256=sha(release/'decision.json'),inherited_release=dict(path=str(previous/'q4t.release.json'),sha256=sha(previous/'q4t.release.json')),previous_binary=str(previous/'q4t'),previous_release=str(previous/'q4t.release.json'),observed_configuration=read(declarations/'configuration.json'))
meta['defaults']['chat_temperature']=1;meta['defaults']['chat_top_p']=1
meta['scope']=dict(production='Existing text/MTP and opt-in image backend retained. This change implements legacy functions/function_call and legacy history migration.',correctness='31 C++,53 schema,9 Python clients,6 JS,3 driver; 1 start/7 actual Chat requests.',performance='Functional tool compatibility; no performance acceptance claim. Existing sampling/prediction/MTP behavior retained.',installation_health='Same accepted ELF and unchanged native/control runtime; immutable verification plus help/status, no new GPU starts.',boundaries='Remaining master tasks open; user owns long stability.')
cp=meta['chat_protocol'];cp.update(copy.deepcopy(caps));cp['protocol_sha256']=protocol
cp['stream']=copy.deepcopy(caps['chat']);cp['declaration_evidence']='Actual seven-request legacy tool/history acceptance; prior unchanged native/TLS/recovery and sampling/prediction evidence reused.'
cp['sdk_interoperability_acceptance']=dict(new_sdk_qualification=True,sdk=plan['sdk'],current='JS OpenAI SDK actual legacy function JSON/SSE, incremental arguments, real records closure, old-history modern migration, truncation/cancellation and normal MTP; Python terminal barrier and bridge CPU. Remaining A12 open.')
for name in pkg.DECLARATION_SCHEMAS:
 p=declarations/'schemas'/(name+'.json');cp[name+'_schema']=str(p);cp[name+'_schema_sha256']=sha(p)
meta['release_declarations']=dict(profile=pkg.DECLARATION_PROFILE,directory=str(declarations),binary_sha256=plan['binary_sha256'],protocol_sha256=protocol,files={n:sha(declarations/n) for n in pkg.DECLARATION_FILES})
meta['tool_stream_acceptance'].pop('legacy_custom_pending',None)
meta['tool_stream_acceptance'].update(legacy_pending=False,custom_pending=True)
meta['legacy_tools_acceptance']=dict(profile='q4t-legacy-functions-v1',stage=str(release/'decision.json'),functions=True,function_call=True,legacy_history=True,modern_migration=True,custom_pending=True,full_openai_compatibility=False)
pkg.verify_declarations(meta,required=True);save(engine/'q4t.release.json',meta)
receipt=bundle.prepare(engine/'q4t',engine/'q4t.release.json',plan['model'],build/'q4t-guardian',old/'bin/q4t-cuda-probe',None,old/'gateway-policy.json',old/'engine/client-policy.json',release/'bundle',native_receipt=release/'native-build.json',native_ingress=build/'q4t-ingress')
save(release/'receipt.json',receipt);bundle.verify(receipt['package'],receipt['manifest_sha256'])
print(json.dumps(dict(receipt=receipt,protocol=protocol,lease_seconds=decision['lease_seconds'],nvml_queries=len(queries)),indent=2))
