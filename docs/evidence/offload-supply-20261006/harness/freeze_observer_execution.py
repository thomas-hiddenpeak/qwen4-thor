"""Freeze one observer execution from completed build; no tests or HTTP."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import time
R=Path(__file__).resolve().parent;O=R/'observer-source';M=R.parent/'offload-mechanism-20261006'
sha=lambda path:hashlib.sha256(Path(path).read_bytes()).hexdigest()
read=lambda path:json.loads(Path(path).read_text())
assert not (R/'observer-execution-plan.json').exists()
build=read(R/'observer-build-identity.json');assert build['warnings']==0 and build['build_rc']==0
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=O,text=True).strip()==build['source_commit']
assert not subprocess.check_output(['git','status','--porcelain'],cwd=O)
files={}
def bind(path):
 path=Path(path).resolve();assert path.is_file();files[str(path)]=sha(path);return str(path)
roots=['observer-appendix.json','observer-implementation-admission.json','observer-implementation-clarification.json',
 'observer-serialization-clarification.json','observer-first-static-findings.json','observer-independent-static-review-r2.json',
 'observer-resource-independent-static-review.json','observer-implementation-commit.json','observer-implementation-push.json',
 'observer-source-identity.json','observer-source.tar','observer-build-identity.json','observer-build-attempt.json',
 'observer-dependency-admission.json','observer-reference-extraction.json','observer-reference-S.json','observer-reference-L.json',
 'observer_entry.py','observer_common.py','run_observer_stage.py','audit_observer_stage.py','run_observer_contracts.py',
 'observer_resource_audit.py','build_observer.py','freeze_observer_execution.py','model-entry.json',
 'result-independent-review.json','supply-analysis.json','supply-independent-summary.json']
for name in roots:bind(R/name)
for name in ['observer-configure-01','observer-build-01']:
 for suffix in ['-start.json','-exit.json','.log']:bind(R/(name+suffix))
for path in read(R/'observer-dependency-admission.json')['source_sha256']:bind(path)
for path in read(R/'observer-reference-extraction.json')['source_sha256']:bind(path)
for name in ['q4t','CMakeCache.txt','compile_commands.json']:bind(R/'observer-build'/name)
for name in ['phase_common.py','observation_contract.py']:bind(M/name)
for path in subprocess.check_output(['git','ls-files'],cwd=O,text=True).splitlines():
 if path.startswith(('tools/evalscope/','tools/trace/')) and path.endswith('.py'):bind(O/path)
for path in ['include/q4t/quant/moe_supply_observer.h','include/q4t/quant/moe_residency.h',
             'src/quant/moe_residency.cpp','src/server/offload_diagnostics.cpp','src/model/moe.cu',
             'src/model/model.cu','tools/trace/test_moe_supply_observer.cpp']:bind(O/path)
for path in [R.parent/'offload-decode-log-20261005/audit_resources.py',
             R.parent/'offload-partition-runtime-20261003/audit_raw_resources_frozen.py',
             R.parent/'offload-autonomous-20261003/raw-audit-execution-01/self-test-exit.json',
             R.parent/'offload-autonomous-20261003/raw-audit-execution-01/self-test.log']:bind(path)
groups=['q01-quality-on','s01-as-off','s02-as-on','s03-al-on','s04-al-off']
commands={};sequences={}
for group in groups:
 p=R/(group+'-command.json');bind(p);commands[group]=sha(p);command=read(p)
 assert command[:5]==['/usr/bin/python3','-B',str(R/'observer_entry.py'),'wrapper',group]
 assert command[command.index('--host-cache-max-bytes')+1]==str(16<<30)
 assert command[command.index('--port')+1]=='8185'
 for flag in ['--hot-list','--reference']:
  if flag in command:bind(command[command.index(flag)+1])
 model=Path(command[command.index('--model-dir')+1]);fixtures=Path(command[command.index('--fixtures')+1])
 for name in ['config.json','model.safetensors.index.json']:bind(model/name)
 if group==groups[0]:
  for name in ['manifest.json','requests.jsonl']:bind(fixtures/name)
 else:
  p=R/'observer-sequences'/(group+'.json');bind(p);sequences[group]=sha(p)
  for length in ([1024] if '-as-' in group else [8193,1024]):bind(fixtures/f'context-{length}/requests.jsonl')
reference=read(R/'q01-quality-on-command.json');quality_reference=reference[reference.index('--reference')+1]
audit_sources=[str(R/name) for name in ['audit_observer_stage.py','observer_common.py']]+[
 str(O/'tools/evalscope'/name) for name in ['supply_observer_protocol.py','audit_offload_diagnostics.py','offload_policy.py','request_policy_protocol.py']]+[
 str(O/'tools/trace'/name) for name in ['offload_trace.py','analyze.py']]+[str(M/'observation_contract.py')]
contracts=[str(O/'include/q4t/quant/moe_supply_observer.h'),str(O/'tools/trace/test_moe_supply_observer.cpp'),
 str(O/'tools/evalscope/supply_observer_protocol.py'),str(O/'tools/evalscope/test_supply_observer_protocol.py'),str(R/'run_observer_contracts.py')]
for path in audit_sources+contracts:assert path in files
for name in ['observer_entry.py','observer_common.py','run_observer_stage.py','audit_observer_stage.py','run_observer_contracts.py','observer_resource_audit.py']:
 ast.parse((R/name).read_text())
plan={'schema':1,'scope':'offload_decode_supply_observer_v1','created_t':time.time(),
 'runtime_source_commit':build['source_commit'],'runner_source_commit':build['source_commit'],
 'runtime_binary_path':build['binary'],'runtime_binary_sha256':build['binary_sha256'],
 'phase_plan_sha256':sha(R/'observer-appendix.json'),'service_count':5,'http_count':27,
 'group_ids':groups,'command_sha256':commands,'sequence_sha256':sequences,
 'quality_reference_path':quality_reference,'quality_reference_sha256':sha(quality_reference),
 'diagnostic_reference_paths':{k:str(R/('observer-reference-'+k+'.json')) for k in ['S','L']},
 'tool_sha256':{Path(p).name:h for p,h in files.items() if Path(p).parent==O/'tools/evalscope'},
 'audit_sources':audit_sources,'contract_sources':contracts,
 'contract_commands':[
  {'label':'observer-contracts-build-01','cwd':str(O),'argv':['/usr/bin/g++-14','-std=c++23','-O2','-Wall','-Wextra','-Werror','-I',str(O/'include'),str(O/'tools/trace/test_moe_supply_observer.cpp'),'-o',str(R/'contracts-tmp/test_moe_supply_observer')]},
  {'label':'observer-contracts-host-01','cwd':str(O),'argv':[str(R/'contracts-tmp/test_moe_supply_observer')]},
  {'label':'observer-contracts-protocol-01','cwd':str(O/'tools/evalscope'),'argv':['/usr/bin/python3','-B','-m','unittest','-v','test_supply_observer_protocol']}],
 'expected_host_contracts':23,'expected_protocol_contracts':23,
 'stage_order':['quality11_HTTP','quality11_audit','new46_contracts','s01-as-off','s02-as-on','s03-al-on','s04-al-off','resource_audit','independent_result_review','final_protection_delivery'],
 'frozen_files':files,'performance_acceptance':False,'new_runtime_execution_admitted':False,
 'source_preservation':'Offline R/source remains 8ea3d32 and its source/trace evidence bindings unchanged; observer is independent O+export.'}
with (R/'observer-execution-plan.json').open('x') as f:json.dump(plan,f,indent=2);f.write('\n')
print({'plan_sha256':sha(R/'observer-execution-plan.json'),'bindings':len(files),'runtime_binary_sha256':build['binary_sha256']})
