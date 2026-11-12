"""Seal namespace/result-contract acceptance with exact profiles; preserve default."""
import copy
import hashlib
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle
from tools.serve_service import capacity, package as p

WORK = Path(__file__).resolve().parent
OUT = WORK/'candidate-02'
BUSINESS = WORK/'business-03'
PROFILES = WORK/'profiles-02'
BUILD = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
OLD = (ROOT/'build/service').resolve(strict=True)
helpers = runpy.run_path(str(ROOT/'.q4t-work/serve-embeddings-20261111/deliver.py'))


def copy_file(source, target):
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def main():
    p.require(not OUT.exists(), 'preserve previous candidate')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
              'commit reviewed sources first')
    accepted = p.read(BUSINESS/'acceptance.json')
    p.require(accepted['status'] == 'ACCEPT_TOOL_IDENTITY_RESULT_CONTRACT_BUSINESS' and
              not accepted['env_abort'] and all(accepted['postflight'].values()),
              'actual namespace/result business absent')
    for name, digest in accepted['records'].items():
        p.require(p.sha(WORK/name) == digest, 'actual business changed')
    plan = p.read(PROFILES/'plan.json')
    current_plan = p.read(BUSINESS/'plan.json')
    runtime = helpers['source_hashes']()
    original = bundle.verify(OLD, p.sha(OLD/'manifest.json'))
    old_release = p.read(OLD/'engine/q4t.release.json')
    previous_runtime = p.read(old_release['tested_source_manifest'])
    runtime_delta = {n for n,h in runtime.items() if previous_runtime.get(n) != h}
    expected_delta = {
        'CMakeLists.txt', 'cmake/ChatProtocolIdentity.cmake',
        'cmake/HostTestSources.cmake',
        'include/q4t/server/conversation_state.h',
        'include/q4t/server/grammar_process.h',
        'include/q4t/server/responses_input.h',
        'include/q4t/server/responses_output.h',
        'include/q4t/server/responses_state.h',
        'include/q4t/server/schema_graph.h',
        'include/q4t/server/tool_result_contracts.h',
        'include/q4t/server/tool_result_schema.h',
        'src/main.cpp', 'src/server/chat_capabilities.cpp',
        'src/server/conversation_state.cpp',
        'src/server/file_legacy_sandbox.cpp', 'src/server/grammar_process.cpp',
        'src/server/responses_api.cpp', 'src/server/responses_execution.cpp',
        'src/server/responses_execution_compaction.cpp',
        'src/server/responses_execution_inputs.cpp',
        'src/server/responses_file_search_view.cpp',
        'src/server/responses_input.cpp', 'src/server/responses_output.cpp',
        'src/server/responses_state.cpp', 'src/server/schema_graph.cpp',
        'src/server/tool_result_contracts.cpp',
        'src/server/tool_result_schema.cpp', 'src/server/worker_io_broker.cpp',
        'include/q4t/server/cache_io.h', 'include/q4t/server/object_store.h',
        'src/server/cache_io.cpp', 'src/server/object_store.cpp'}
    p.require(runtime_delta == expected_delta, 'runtime drift')
    fixes = {'include/q4t/server/cache_io.h', 'include/q4t/server/object_store.h',
             'src/server/cache_io.cpp', 'src/server/object_store.cpp'}
    fix_path = ROOT/'docs/evidence/serve-controller-stop-race-20261113/acceptance.json'
    repair = p.read(fix_path)
    p.require(repair['status'] == 'PASS_TARGETED_CONTROLLER_STOP_RACE' and
        repair['positive_cpu_cases'] == 16 and repair['negative_controls_reproduced'] == 3,
        'controller repair not directly verified')
    inherited_delta = {n for n,h in runtime.items() if current_plan['sources'].get(n) != h}
    p.require(inherited_delta == fixes and all(repair['source_hashes'][n] == runtime[n]
              for n in fixes), 'namespace evidence changed outside reviewed stop repair')
    p.require(all(plan['sources'].get(n) == h for n,h in runtime.items()),
              'current qualified runtime changed')
    p.require(p.sha(WORK/'candidate-01/engine/q4t') == accepted['binary_sha256'] and
        p.sha(BUILD/'q4t') == plan['binary_sha256'], 'qualified engine changed')
    for name, digest in current_plan['protected'].items():
        p.require(p.sha(name) == digest, 'default changed')
    native_old = p.read(OLD/'native-build.json')
    native_delta = {n for n,h in native_old['source_files'].items() if p.sha(ROOT/n) != h}
    p.require(native_old['source_files'].keys() == bundle.NATIVE_SOURCES and
              native_delta == {'CMakeLists.txt'}, 'native drift')
    native = dict(native_old, source_files={n:p.sha(ROOT/n) for n in sorted(bundle.NATIVE_SOURCES)},
        reuse_review=dict(previous=str(OLD/'native-build.json'),
            sha256=p.sha(OLD/'native-build.json'), changed_sources=sorted(native_delta),
            rationale='The root CMake change only adds result-contract files to q4t_server and CPU tests. Native helper compilation/link sources are unchanged; exact accepted helper bytes are retained, not rebuilt.'))
    for field, filename in [('guardian_sha256','q4t-guardian'),
        ('cuda_probe_sha256','q4t-cuda-probe'), ('ingress_sha256','q4t-ingress'),
        ('device_control_sha256','q4t-device-control')]:
        p.require(native[field] == p.sha(OLD/'bin'/filename), 'retained helper changed')
    control_delta = {n for n in bundle.SOURCES if p.sha(ROOT/n) != p.sha(OLD/'source'/n)}
    p.require(control_delta == set(), 'control drift')
    OUT.mkdir(mode=0o700)
    engine = OUT/'engine'
    for name in ('q4t','CMakeCache.txt'):
        copy_file(BUILD/name, engine/name)
    (engine/'q4t').chmod(0o500)
    p.save(engine/'runtime-source.json', runtime)
    compiled = p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    p.require(all(runtime.get(n) == h for n,h in compiled.items()), 'compiled source changed')
    protocol = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(compiled.items())).encode()).hexdigest()
    qualification = p.read(PROFILES/'acceptance.json')
    p.require(qualification['status'] == 'ACCEPT_PRIMARY_LIGHT_QUALIFICATION' and
        qualification['environment']['cleanup_complete'], 'profiles absent')
    caps, config = [p.read(PROFILES/('primary-'+n+'.json')) for n in ('capabilities','config')]
    p.require(caps['protocol_source_sha256'] == config['protocol_source_sha256'] == protocol,
              'protocol observation differs')
    decision = dict(status='PASS_TOOL_IDENTITY_RESULT_CONTRACT',
        acceptance='ACCEPT_TOOL_IDENTITY_RESULT_CONTRACT', binary_sha256=plan['binary_sha256'],
        protocol_sha256=protocol, actual_business=accepted, runtime_delta=sorted(runtime_delta),
        source_commit=plan['source_commit'], full_model_quality=False,
        performance_acceptance=False, profile_qualification=qualification,
        controller_stop_repair=repair, controller_repair_evidence_sha256=p.sha(fix_path),
        inherited_namespace_source_delta=sorted(inherited_delta),
        retained_service_evidence=str(ROOT/'docs/evidence/serve-retrieval-managed-business-20261112/README.md'),
        optional_frontend_evidence=str(ROOT/'docs/evidence/serve-optional-route-parity-20261112/README.md'),
        default_changed=False, remaining_master_tasks_open=True)
    p.save(OUT/'decision.json', decision)
    metadata = copy.deepcopy(old_release)
    metadata.update(version='serve-tool-identity-20261113',
        source_commit=plan['source_commit'], binary_sha256=plan['binary_sha256'],
        acceptance=decision['acceptance'], acceptance_decision=str(OUT/'decision.json'),
        acceptance_decision_sha256=p.sha(OUT/'decision.json'),
        build_cache=str(engine/'CMakeCache.txt'),build_cache_sha256=p.sha(engine/'CMakeCache.txt'),
        tested_source_manifest=str(engine/'runtime-source.json'),
        tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),
        observed_configuration=config, previous_binary=str(OLD/'engine/q4t'),
        previous_release=str(OLD/'engine/q4t.release.json'),
        inherited_release=dict(path=str(OLD/'engine/q4t.release.json'),sha256=p.sha(OLD/'engine/q4t.release.json')),
        source_note='Native namespace/function/custom tools, public item projection, invocation-time output_schema and flat Responses grammar conversion (prior same implementation business retained except the directly verified four-source controller stop repair): targeted CPU/SDK/schema, actual JSON/SSE tool closure retained; current exact primary/light schema/tool/embedding and official SDK conversation persistence across shutdown accepted. Object and cache stop races repaired with three old-order negative controls and 16 passing CPU cases. Metered x86 codec ABI repair has direct CPU and actual x86 CI evidence. Unchanged model, native ingress, retrieval and recovery evidence retained; no full quality/performance claim.')
    declarations = engine/'declarations'
    declarations.mkdir(mode=0o700)
    p.save(declarations/'capabilities.json',caps)
    p.save(declarations/'configuration.json',config)
    metadata['chat_protocol'].update(caps,protocol_sha256=protocol,
        declaration_evidence='Exact current primary/light observations and actual stored state across shutdown; namespace/result/custom business reused only across the directly verified stop repair, with targeted CPU/SDK/schema; unchanged native recovery evidence reused.')
    for name,stem in helpers['SCHEMAS'].items():
        source = ROOT/'docs'/(stem+('.json' if name=='openapi' else '.schema.json'))
        target = declarations/'schemas'/(name+'.json')
        copy_file(source,target)
        metadata['chat_protocol'][name+'_schema'] = str(target)
        metadata['chat_protocol'][name+'_schema_sha256'] = p.sha(target)
    metadata['release_declarations'] = dict(profile=p.APPLICATION_DECLARATION_PROFILE,
        directory=str(declarations),binary_sha256=plan['binary_sha256'],protocol_sha256=protocol,
        files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    for source in (ROOT/'third_party/licenses').rglob('*'):
        if source.is_file():copy_file(source,engine/'licenses'/source.relative_to(ROOT/'third_party/licenses'))
    metadata['third_party_notices']['source_directory'] = str(engine/'licenses/structured_output')
    metadata['application_notices']['source_directory'] = str(engine/'licenses/openai-node')
    for name,record in metadata['file_input_system_dependencies'].items():
        source = Path(record['artifact'])
        p.require(p.sha(source)==record['sha256'],'notice changed')
        target=engine/'licenses/system'/name;copy_file(source,target);record['artifact']=str(target)
    clients=metadata['client_artifacts']
    for name in clients['files']:
        copy_file(ROOT/name,OUT/'clients'/name);clients['files'][name]=p.sha(ROOT/name)
    clients['directory']=str(OUT/'clients')
    metadata['tool_identity_result_acceptance']=decision
    p.save(engine/'q4t.release.json',metadata)
    p.verify_declarations(metadata,required=True)
    p.save(OUT/'native-build.json',native)
    settings=p.read(OLD/'engine/manifest.json')['settings']
    profile=dict(version=1,**{k:v for k,v in settings['profile'].items()
                             if k in capacity.BOUNDS or k=='mtp_enabled'})
    kwargs={key:OLD/'engine'/name for key,name in
        [('access_program_policy','access-program-policy.json'),('file_input_policy','file-input-policy.json')]
        if (OLD/'engine'/name).is_file()}
    receipt=bundle.prepare(engine/'q4t',engine/'q4t.release.json',
        p.read(OLD/'engine/manifest.json')['model_dir'],OLD/'bin/q4t-guardian',
        OLD/'bin/q4t-cuda-probe',None,OLD/'gateway-policy.json',OLD/'engine/client-policy.json',
        OUT/'bundle',native_receipt=OUT/'native-build.json',native_ingress=OLD/'bin/q4t-ingress',
        device_control=OLD/'bin/q4t-device-control',capacity_config=profile,
        application_state={k:v for k,v in settings['application_state'].items() if k!='profile'},
        embedding_model_dir=Path(settings['native_embedding']['directory']),
        recovery_profiles=PROFILES/'qualified/registry.json',
        recovery_profile_policy=original['recovery_profile_policy'],**kwargs)
    p.save(OUT/'receipt.json',receipt)
    p.require(p.read(Path(receipt['package'])/'engine/manifest.json')['settings']==settings,
              'managed settings drift')
    p.save(OUT/'prepare-review.json',dict(previous=str(OLD),
        previous_manifest_sha256=p.sha(OLD/'manifest.json'),engine_sha256=plan['binary_sha256'],
        protocol_sha256=protocol,control_delta=sorted(control_delta),
        native_source_delta=sorted(native_delta),runtime_delta=sorted(runtime_delta),
        new_gpu_starts=0,default_changed=False,package_sha256=receipt['manifest_sha256']))
    print(receipt['manifest_sha256'])


if __name__=='__main__':
    os.umask(0o077)
    main()
