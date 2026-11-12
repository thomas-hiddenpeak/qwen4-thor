"""Prepare an isolated accepted engine package; no default/state mutation."""
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

WORK = ROOT / '.q4t-work/serve-refusal-20261112'
OUT = WORK / 'candidate-03'
BUSINESS = WORK / 'business-01'
PROFILES = WORK / 'profiles-01'
BUILD = ROOT / '.q4t-work/serve-custom-tools-20261108/engine-build'
OLD = (ROOT / 'build/service').resolve(strict=True)
helpers = runpy.run_path(str(ROOT / '.q4t-work/serve-embeddings-20261111/deliver.py'))


def copy_file(source, target):
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def main():
    p.require(not OUT.exists(), 'preserve existing candidate attempt')
    p.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit reviewed sources first')
    accepted, plan = p.read(BUSINESS/'acceptance-final.json'), p.read(BUSINESS/'plan.json')
    p.require(accepted['status'] == 'ACCEPT_NATIVE_REFUSAL_BUSINESS' and
              not accepted['env_abort'] and all(accepted['postflight'].values()),
              'actual business absent')
    runtime = helpers['source_hashes']()
    p.require(all(plan['sources'].get(n) == h for n,h in runtime.items()),
              'tested C++ source changed')
    p.require(p.sha(BUILD/'q4t') == plan['binary_sha256'], 'tested engine changed')
    for name, digest in plan['protected'].items():
        p.require(p.sha(name) == digest, 'default identity changed')
    original = bundle.verify(OLD, p.sha(OLD/'manifest.json'))
    native = p.read(OLD/'native-build.json')
    p.require(native['source_files'].keys() == bundle.NATIVE_SOURCES and
              all(p.sha(ROOT/n) == h for n,h in native['source_files'].items()),
              'native helper source changed')
    control_delta = {n for n in bundle.SOURCES if p.sha(ROOT/n) != p.sha(OLD/'source'/n)}
    p.require(control_delta == {'tools/serve_recovery/profile_manager.py',
                               'tools/serve_recovery/profile_migration.py'},
              'unreviewed service control changes')
    old_engine = p.read(OLD/'engine/manifest.json')
    OUT.mkdir(mode=0o700)
    engine = OUT/'engine'
    for name in ('q4t', 'CMakeCache.txt'):
        copy_file(BUILD/name, engine/name)
    (engine/'q4t').chmod(0o500)
    p.save(engine/'runtime-source.json', runtime)
    compiled = p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    p.require(all(runtime.get(n) == h for n,h in compiled.items()), 'compiled protocol changed')
    protocol = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(compiled.items())).encode()).hexdigest()
    qualification = p.read(PROFILES/'acceptance.json')
    p.require(qualification['status'] == 'ACCEPT_PRIMARY_LIGHT_QUALIFICATION' and
              qualification['environment']['cleanup_complete'], 'actual profiles absent')
    caps = p.read(PROFILES/'primary-capabilities.json')
    config = p.read(PROFILES/'primary-config.json')
    p.require(caps['protocol_source_sha256'] == config['protocol_source_sha256'] == protocol,
              'actual protocol identity differs')
    decision = dict(status='PASS_TARGETED_NATIVE_REFUSAL',
        acceptance=accepted['status'], binary_sha256=plan['binary_sha256'],
        protocol_sha256=protocol, actual_business=accepted,
        source_commit=plan['source_commit'], full_model_quality=False,
        performance_acceptance=False, profile_qualification_pending=False,
        profile_qualification=qualification,
        default_changed=False, remaining_master_tasks_open=True)
    p.save(OUT/'decision.json', decision)
    metadata = copy.deepcopy(p.read(OLD/'engine/q4t.release.json'))
    metadata.update(version='serve-native-refusal-20261112',
        source_commit=plan['source_commit'], binary_sha256=plan['binary_sha256'],
        acceptance=accepted['status'], acceptance_decision=str(OUT/'decision.json'),
        acceptance_decision_sha256=p.sha(OUT/'decision.json'),
        build_cache=str(engine/'CMakeCache.txt'), build_cache_sha256=p.sha(engine/'CMakeCache.txt'),
        tested_source_manifest=str(engine/'runtime-source.json'),
        tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),
        observed_configuration=config,
        previous_binary=str(OLD/'engine/q4t'), previous_release=str(OLD/'engine/q4t.release.json'),
        inherited_release=dict(path=str(OLD/'engine/q4t.release.json'),
                               sha256=p.sha(OLD/'engine/q4t.release.json')),
        source_note='Actual model refusal/JSON/SSE/logprobs/history and tool/schema business accepted. Primary/light exact managed settings and native embedding32D qualified. Prior unchanged numerical/embedding evidence retained as historical provenance. This isolated package awaits compatible recovery delivery; existing default unchanged.')
    declarations = engine/'declarations'
    declarations.mkdir(mode=0o700)
    p.save(declarations/'capabilities.json', caps)
    p.save(declarations/'configuration.json', config)
    metadata['chat_protocol'].update(caps, protocol_sha256=protocol,
        declaration_evidence='Actual candidate primary managed configuration with native embedding enabled; both primary/light qualifications and prior six refusal/protection generations passed.')
    for name, stem in helpers['SCHEMAS'].items():
        source = ROOT/'docs'/(stem+('.json' if name == 'openapi' else '.schema.json'))
        target = declarations/'schemas'/(name+'.json')
        copy_file(source, target)
        metadata['chat_protocol'][name+'_schema'] = str(target)
        metadata['chat_protocol'][name+'_schema_sha256'] = p.sha(target)
    metadata['release_declarations'] = dict(profile=p.APPLICATION_DECLARATION_PROFILE,
        directory=str(declarations), binary_sha256=plan['binary_sha256'],
        protocol_sha256=protocol,
        files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    for source in (ROOT/'third_party/licenses').rglob('*'):
        if source.is_file():
            copy_file(source, engine/'licenses'/source.relative_to(ROOT/'third_party/licenses'))
    metadata['third_party_notices']['source_directory'] = str(engine/'licenses/structured_output')
    metadata['application_notices']['source_directory'] = str(engine/'licenses/openai-node')
    for name, record in metadata['file_input_system_dependencies'].items():
        source = Path(record['artifact'])
        p.require(p.sha(source) == record['sha256'], 'system notice changed')
        target = engine/'licenses/system'/name
        copy_file(source, target)
        record['artifact'] = str(target)
    clients = metadata['client_artifacts']
    for name in clients['files']:
        copy_file(ROOT/name, OUT/'clients'/name)
        clients['files'][name] = p.sha(ROOT/name)
    clients['directory'] = str(OUT/'clients')
    metadata['refusal_acceptance'] = dict(actual_business=accepted,
        cpu_evidence='docs/evidence/serve-refusal-cpu-20261112/README.md',
        client_evidence='docs/evidence/serve-refusal-cpu-20261112/client-test-plan-01.json',
        candidate_only=True, profile_qualification=qualification)
    p.save(engine/'q4t.release.json', metadata)
    p.verify_declarations(metadata, required=True)
    copy_file(OLD/'native-build.json', OUT/'native-build.json')
    settings = old_engine['settings']
    profile = dict(version=1, **{k:v for k,v in settings['profile'].items()
                                if k in capacity.BOUNDS or k == 'mtp_enabled'})
    kwargs = {key:OLD/'engine'/name for key,name in
        [('access_program_policy','access-program-policy.json'),
         ('file_input_policy','file-input-policy.json')] if (OLD/'engine'/name).is_file()}
    receipt = bundle.prepare(engine/'q4t', engine/'q4t.release.json', old_engine['model_dir'],
        OLD/'bin/q4t-guardian', OLD/'bin/q4t-cuda-probe', None,
        OLD/'gateway-policy.json', OLD/'engine/client-policy.json', OUT/'unqualified-bundle',
        native_receipt=OUT/'native-build.json', native_ingress=OLD/'bin/q4t-ingress',
        device_control=OLD/'bin/q4t-device-control', capacity_config=profile,
        application_state={k:v for k,v in settings['application_state'].items() if k != 'profile'},
        embedding_model_dir=Path(settings['native_embedding']['directory']),
        recovery_profiles=PROFILES/'qualified/registry.json',
        recovery_profile_policy=original['recovery_profile_policy'], **kwargs)
    p.save(OUT/'receipt.json', receipt)
    p.require(p.read(Path(receipt['package'])/'engine/manifest.json')['settings'] == settings,
              'managed settings drift')
    p.save(OUT/'prepare-review.json', dict(previous=str(OLD),
        previous_manifest_sha256=p.sha(OLD/'manifest.json'), engine_sha256=plan['binary_sha256'],
        protocol_sha256=protocol, control_delta=sorted(control_delta), native_source_delta=[],
        actual_gpu_starts=0, default_changed=False, profile_qualification_pending=False,
        native_embedding_preserved=True, package_sha256=receipt['manifest_sha256']))
    print(receipt['manifest_sha256'])


if __name__ == '__main__':
    os.umask(0o077)
    main()
