from pathlib import Path
import copy, hashlib, json, os, re, shutil, subprocess, sys
sys.dont_write_bytecode=True
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle
from tools.serve_service import package as pkg
sys.path.insert(0,str(ROOT/'tools/evalscope'))
from tools.evalscope import gpu_exclusive as guard
from tools.serve_custom_tools.acceptance import previous_attempts
base=ROOT/'.q4t-work/serve-custom-tools-20261108';business=base/'business-03';build=base/'engine-build'
read=pkg.read;save=pkg.save;sha=pkg.sha;require=pkg.require
plan=read(business/'plan.json');result=read(business/'result.json');cleanup=read(business/'cleanup.json');outer=read(business/'lease/exit.json')
require(result['status']=='PASS_CUSTOM_TOOLS_BUSINESS' and result['counts']['starts']==1 and result['counts']['chats']==8,'business failed')
require(outer['status']=='COMPLETED' and outer['child_returncode']==0,'lease did not pass')
priors=[base/'business-01',base/'business-02']
require(plan['previous_attempts']==previous_attempts(priors),'prior failure evidence changed')
preserved=read(base/'candidate-before-framing/receipt.json')
require(sha(preserved['preserved'])==preserved['sha256'],'prior candidate lost')
for prior in priors:
 prior_plan=read(prior/'plan.json');prior_result=read(prior/'result.json')
 require(prior_result['binary_sha256']==preserved['sha256'],'prior binary differs')
 for name,digest in prior_plan['sources'].items():
  require(hashlib.sha256(subprocess.check_output(['git','show',prior_plan['source_commit']+':'+name])).hexdigest()==digest,'prior Git identity '+name)
require(not cleanup['remaining'] and cleanup['model_exit']==0 and cleanup['ports_released'],'cleanup failed')
for name,digest in plan['sources'].items():
 require(sha(ROOT/name)==digest,'source drift '+name)
 require(hashlib.sha256(subprocess.check_output(['git','show',plan['source_commit']+':'+name])).hexdigest()==digest,'Git identity '+name)
for name,digest in plan['protected'].items():require(sha(name)==digest,'default changed')
require(sha(build/'q4t')==plan['binary_sha256']==result['binary_sha256'],'ELF differs')
processes=guard.processes()
for stage in [*priors,business]:
 for name,identity in read(stage/'cleanup.json')['owned'].items():
  current=processes.get(int(name))
  require(current is None or current['start_ticks']!=identity['start_ticks'],'owned process survives')
queries=[json.loads(line) for stage in [*priors,business] for line in (stage/'lease/nvml-queries.jsonl').read_text().splitlines()]
require(queries and all(v['status']=='OK' for v in queries),'NVML unsuccessful')
lease_seconds=0
for stage in [*priors,business]:
 events=[json.loads(line) for line in (stage/'lease/events.jsonl').read_text().splitlines()]
 require(events[0]['event']=='lease_acquired' and events[-1]['event']=='lease_released','lease bounds')
 lease_seconds+=events[-1]['monotonic']-events[0]['monotonic']
for folder in ('build-06','build-07','native-build-01'):
 for log in (base/folder).glob('*.log'):require(not re.search(r'\b(?:warning|error):',log.read_text()),'build warnings '+str(log))
cpu=read(base/'cpu-01/result.json')
require(all(cpu[k]['exit_code']==0 for k in ('host','shapes','python','js','schema','sources')),'CPU failed')
require(read(base/'cpu-02/result.json')['exit_code']==0,'driver CPU failed')
require(all(row['exit_code']==0 for row in read(base/'cpu-03/result.json').values()),'grammar repair/bridge failed')
require(read(base/'cpu-04/result.json')['exit_code']==0,'repair driver failed')
cpu5=read(base/'cpu-05/result.json');cpu6=read(base/'cpu-06/result.json')
require(cpu5['host']['exit_code']==1 and cpu5['schema']['exit_code']==1 and cpu5['driver']['exit_code']==0 and cpu5['sources']['exit_code']==0,'original CPU failure changed')
passed=set(re.findall(r'^\[PASS\] (.+)$',(base/'cpu-05/host.log').read_text(),re.M))
failed=set(re.findall(r'^\[FAIL\] (.+)$',(base/'cpu-05/host.log').read_text(),re.M))
require(failed=={'custom_tools_native_mixed_incremental_types_follow_definitions'} and len(passed)==46,'unexpected host failure')
require(all(row['exit_code']==0 for row in cpu6.values()),'CPU repair failed')
require(failed<=set(re.findall(r'^\[PASS\] (.+)$',(base/'cpu-06/host.log').read_text(),re.M)),'failed test not repaired')
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
decision=dict(status='PASS_TARGETED_CUSTOM_TOOLS_DELIVERY',acceptance='ACCEPT_TARGETED_CUSTOM_TOOLS',binary_sha256=plan['binary_sha256'],protocol_sha256=protocol,source_commit=plan['source_commit'],source_files=len(plan['sources']),runtime_sources=len(runtime),source_snapshot=str(snapshot),model_starts=3,chat_requests=14,driver_seconds=result['seconds']+sum(read(p/'result.json')['seconds'] for p in priors),lease_seconds=lease_seconds,nvml_queries=len(queries),nvml_all_ok=True,recorded_processes_exited=True,ports_free=True,native_byte_identical=True,cuda_probe_reused=native['cuda_probe_sha256'],cpu=dict(cpp=47,schema_native=72,python_clients=17,js=9,driver=5),performance_run=False,full_model_quality_run=False,business=str(business/'result.json'),business_sha256=sha(business/'result.json'),lease_sha256=sha(business/'lease/exit.json'),inherited_engine=sha(previous/'q4t'),inherited_service_manifest=sha(old/'manifest.json'),model_kernels_unchanged=True,remaining_master_tasks_open=True,custom_tools_targeted_accepted=True)
decision.update(previous_failed_attempts=plan['previous_attempts'],reused_business_requests=[],same_binary_reused=False,cumulative_limits=plan['cumulative_limits'],repair='Native function/input model adapter; original mixed user task passes with both tool types; both old failed attempts retained')
save(release/'decision.json',decision)
declarations=engine/'declarations';(declarations/'schemas').mkdir(parents=True)
for dst,src in [('capabilities.json','capabilities.json'),('configuration.json','config.json')]:shutil.copyfile(business/src,declarations/dst)
for name,source in dict(config='serve-config-v1.schema.json',message='serve-message-profile-v3.schema.json',preflight='serve-preflight-v1.schema.json',reasoning='serve-reasoning-v1.schema.json',response='serve-transport-v1.schema.json',stop='serve-stop-v1.schema.json',thinking='serve-thinking-v1.schema.json').items():
 p=ROOT/'docs'/source
 require(p.is_file(),'missing schema '+str(p));shutil.copyfile(p,declarations/'schemas'/(name+'.json'))
meta=copy.deepcopy(old_meta)
meta.update(version='serve-custom-tools-20261108',binary_sha256=plan['binary_sha256'],source_commit=plan['source_commit'],source_note='Custom tools implement text/regex/Lark, mixed function batches, complete history and standard custom.input JSON/SSE. Rust regex syntax is statically linked inside the existing bounded worker; separate client registries enforce the terminal/EOF barrier. All remaining master tasks continue.',build_cache=str(engine/'CMakeCache.txt'),build_cache_sha256=sha(engine/'CMakeCache.txt'),tested_source_manifest=str(engine/'runtime-source.json'),tested_source_manifest_sha256=sha(engine/'runtime-source.json'),acceptance=decision['acceptance'],acceptance_decision=str(release/'decision.json'),acceptance_decision_sha256=sha(release/'decision.json'),inherited_release=dict(path=str(previous/'q4t.release.json'),sha256=sha(previous/'q4t.release.json')),previous_binary=str(previous/'q4t'),previous_release=str(previous/'q4t.release.json'),observed_configuration=read(declarations/'configuration.json'))
meta['defaults']['chat_temperature']=1;meta['defaults']['chat_top_p']=1
meta['scope']=dict(production='Existing text/MTP and opt-in image backend retained. This change implements custom text/regex/Lark and mixed-tool history and response streaming.',correctness='47 C++,72 schema,17 Python clients,9 JS,5 driver; final 1 start/8 Chat passes; both prior mixed-call failures retained, cumulative 3 starts/14 Chat.',performance='Functional tool compatibility; no performance acceptance claim. Existing sampling/prediction/MTP behavior retained.',installation_health='Same accepted ELF and unchanged native/control runtime; immutable verification plus help/status, no new GPU starts.',boundaries='Remaining master tasks open; user owns long stability.')
cp=meta['chat_protocol'];cp.update(copy.deepcopy(caps));cp['protocol_sha256']=protocol
cp['stream']=copy.deepcopy(caps['chat']);cp['declaration_evidence']='Final custom tool/history acceptance 1 start/8 Chat after native model framing repair. Two prior failed attempts retained with old candidate ELF; previous unchanged function/legacy/native/TLS/recovery and sampling/prediction evidence reused.'
cp['sdk_interoperability_acceptance']=dict(new_sdk_qualification=True,sdk=plan['sdk'],current='JS OpenAI SDK custom text/regex/Lark JSON/SSE, incremental raw inputs, actual records closure, mixed custom/function, truncation/cancellation and normal MTP; Python terminal barrier and bridge CPU. Remaining A12 open.')
for name in pkg.DECLARATION_SCHEMAS:
 p=declarations/'schemas'/(name+'.json');cp[name+'_schema']=str(p);cp[name+'_schema_sha256']=sha(p)
meta['release_declarations']=dict(profile=pkg.DECLARATION_PROFILE,directory=str(declarations),binary_sha256=plan['binary_sha256'],protocol_sha256=protocol,files={n:sha(declarations/n) for n in pkg.DECLARATION_FILES})
meta['tool_stream_acceptance'].update(legacy_pending=False,custom_pending=False)
meta['legacy_tools_acceptance']['custom_pending']=False
meta['custom_tools_acceptance']=dict(profile='q4t-custom-tools-v1',stage=str(release/'decision.json'),formats=['text','regex','lark'],mixed_function=True,custom_history=True,incremental_input=True,full_openai_compatibility=False)
# Carry original license texts in the immutable release metadata itself, so
# every copied release package retains notices even without the source tree.
notice_root=ROOT/'third_party/licenses/structured_output'
notices={str(p.relative_to(notice_root)):dict(sha256=sha(p),text=p.read_bytes().decode('utf-8')) for p in sorted(notice_root.rglob('*')) if p.is_file()}
meta['third_party_notices']=dict(source_directory='third_party/licenses/structured_output',files=notices,static_dependencies=['xgrammar ca1acf28','regex-syntax 0.8.8','Rust standard library 1.90.0'])
shutil.copytree(notice_root,engine/'licenses/structured_output')
pkg.verify_declarations(meta,required=True);save(engine/'q4t.release.json',meta)
receipt=bundle.prepare(engine/'q4t',engine/'q4t.release.json',plan['model'],build/'q4t-guardian',old/'bin/q4t-cuda-probe',None,old/'gateway-policy.json',old/'engine/client-policy.json',release/'bundle',native_receipt=release/'native-build.json',native_ingress=build/'q4t-ingress')
shutil.copytree(notice_root,release/'bundle/engine/licenses/structured_output')
save(release/'receipt.json',receipt);bundle.verify(receipt['package'],receipt['manifest_sha256'])
print(json.dumps(dict(receipt=receipt,protocol=protocol,lease_seconds=decision['lease_seconds'],nvml_queries=len(queries)),indent=2))
