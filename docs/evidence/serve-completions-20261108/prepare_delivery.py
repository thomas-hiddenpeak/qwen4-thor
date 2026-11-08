"""Seal the accepted unchanged A06 runtime and native service bundle."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path.cwd().resolve()
sys.path.insert(0, str(ROOT))
from tools.serve_service import package as p
from tools.serve_recovery import bundle
from tools.serve_completions.review import review

BASE = ROOT/'.q4t-work/serve-completions-20261108'
BUILD = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
OUT = BASE/'release-01'
read, save, sha, require = p.read, p.save, p.sha, p.require


def copy_file(source, destination):
    source, destination = Path(source), Path(destination)
    require(not destination.exists(), 'refuse overwrite: '+str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o500 if os.access(source, os.X_OK) else 0o400)
    require(sha(source) == sha(destination), 'copy identity')


def main():
    require(not OUT.exists(), 'release already exists')
    require(not subprocess.check_output(['git', 'status', '--porcelain']),
            'commit sources before packaging')
    reviewed = review(BASE/'business-01')
    require(reviewed == read(BASE/'business-review-01.json'), 'review receipt drift')
    plan = read(BASE/'business-01/plan.json')
    for name, digest in plan['protected'].items():
        require(sha(name) == digest, 'default changed before delivery')
    caps, config = (read(BASE/'business-01'/n) for n in ('capabilities.json','config.json'))
    protocol = caps['protocol_source_sha256']
    require(config['protocol_source_sha256'] == protocol and
            caps['completions']['profile'] == 'q4t-completions-v1', 'protocol discovery')
    protocol_sources = read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    digest = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in
                                   sorted(protocol_sources.items())).encode()).hexdigest()
    require(digest == protocol and all(sha(ROOT/n) == h for n,h in protocol_sources.items()),
            'embedded source identity')
    old = (ROOT/'build/service').resolve(strict=True)
    bundle.verify(old, sha(old/'manifest.json'))
    control = {n:sha(ROOT/n) for n in bundle.SOURCES}
    changed_control = {n:dict(before=sha(old/'source'/n), after=h)
                       for n,h in control.items() if sha(old/'source'/n) != h}
    require(set(changed_control) == {'tools/serve_gateway/gateway.py',
        'tools/serve_recovery/bundle.py','tools/serve_recovery/gateway_config.py',
        'tools/serve_recovery/service.py','tools/serve_service/package.py',
        'tools/serve_service/service.py'}, 'unexpected control change')
    checks = ['cpu-01','cpu-03','http-cpu-01','schema-cpu-01','sdk-cpu-01',
              'native-cpu-01','tls-cpu-01','python-cpu-01','declaration-cpu-01',
              'accounting-cpu-01','source-check-01','build-05']
    require(all(read(BASE/name/'result.json')['passed'] for name in checks), 'CPU prerequisite')
    first = read(BASE/'cpu-02/result.json')
    require(not first['passed'] and first['tests'] == 28 and len(first['passed_names']) == 26,
            'retain two repaired fixture failures')
    require(sha(BUILD/'q4t') == reviewed['binary_sha256'], 'ELF changed')
    OUT.mkdir(mode=0o700)
    engine = OUT/'engine'; engine.mkdir()
    names = sorted(set(plan['sources']) | {'tools/serve_completions/review.py'})
    for name in names: copy_file(ROOT/name, BASE/'source'/name)
    commit = subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()
    runtime = {n:sha(ROOT/n) for n in subprocess.check_output(
        ['git','ls-files','src','include','cmake','CMakeLists.txt'], text=True).splitlines()}
    save(engine/'runtime-source.json', runtime)
    save(OUT/'source-snapshot.json', dict(commit=commit, original_test_commit=plan['source_commit'],
         current={n:sha(ROOT/n) for n in names}, runtime_unchanged=True))
    for name in ('q4t','CMakeCache.txt'): copy_file(BUILD/name, engine/name)
    for name in ('q4t','q4t.release.json','service.release.json'):
        copy_file(ROOT/'build'/name, OUT/'previous'/name)
    declarations = engine/'declarations'; declarations.mkdir()
    save(declarations/'capabilities.json', caps)
    save(declarations/'configuration.json', config)
    schema_paths = dict(config='serve-config-v1.schema.json',
        message='serve-message-profile-v3.schema.json', preflight='serve-preflight-v1.schema.json',
        reasoning='serve-reasoning-v1.schema.json', response='serve-transport-v1.schema.json',
        stop='serve-stop-v1.schema.json', thinking='serve-thinking-v1.schema.json',
        completion_request='serve-completion-request-v1.schema.json',
        completion_response='serve-completion-transport-v1.schema.json')
    for name, file in schema_paths.items():
        copy_file(ROOT/'docs'/file, declarations/'schemas'/(name+'.json'))
    decision = dict(status='PASS_TARGETED_COMPLETIONS_DELIVERY', acceptance='ACCEPT_TARGETED_COMPLETIONS',
        binary_sha256=reviewed['binary_sha256'], protocol_sha256=protocol,
        source_commit=commit, original_test_commit=plan['source_commit'],
        source_files=len(names), runtime_sources=len(runtime), protocol_sources=len(protocol_sources),
        review=str(BASE/'business-review-01.json'), review_sha256=sha(BASE/'business-review-01.json'),
        totals={k:reviewed[k] for k in ('starts','chats','seconds','lease_seconds','nvml_samples')},
        cpu=dict(distinct_cpp=50, schema_native=51, official_sdk=6, native_ingress=3,
                 native_tls_cors=1, driver_managed_nginx=7, declaration=3, accounting_negative=1),
        original_failed_outcome_retained=True, original_failure='driver counter expectation only',
        runtime_unchanged_after_business=True, additional_gpu_tests=0,
        native_binary_changed=True, native_targeted_cpu_accepted=True,
        control_runtime_changed=list(sorted(changed_control)), native_gpu_restart_repeated=False,
        performance_acceptance=False, full_model_quality_run=False,
        long_stability_accepted=False, remaining_master_tasks_open=True)
    save(OUT/'decision.json', decision)
    save(OUT/'control-review.json', dict(previous_package=str(old), files=control,
        changed=changed_control, unchanged_count=len(control)-len(changed_control),
        coverage='Actual native routes/TLS/CORS plus managed command and portable declaration tests; '
            'controller/Guardian/model recovery policy unchanged.',
        cpu={n:sha(BASE/n/'result.json') for n in checks}))
    metadata = read(ROOT/'build/q4t.release.json')
    metadata.update(version='serve-completions-20261108', binary_sha256=reviewed['binary_sha256'],
        source_commit=commit, source_note='Native raw Completions/batch/n/best_of/echo/prompt scores/FIM '
        'and stable Models registry. Actual JSON/SSE, score ranking, cancel/Chat accepted. '
        'Original final driver counter assertion retained and independently reconciled.',
        build_cache=str(engine/'CMakeCache.txt'), build_cache_sha256=sha(engine/'CMakeCache.txt'),
        tested_source_manifest=str(engine/'runtime-source.json'),
        tested_source_manifest_sha256=sha(engine/'runtime-source.json'),
        acceptance=decision['acceptance'], acceptance_decision=str(OUT/'decision.json'),
        acceptance_decision_sha256=sha(OUT/'decision.json'),
        inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),
                               sha256=sha(OUT/'previous/q4t.release.json')),
        previous_binary=str(OUT/'previous/q4t'), previous_release=str(OUT/'previous/q4t.release.json'),
        observed_configuration=config)
    metadata['chat_protocol'].update(caps)
    metadata['chat_protocol'].update(protocol_sha256=protocol,
        declaration_evidence='Exact runtime observed capabilities/config and seven real requests. '
            'Unchanged A05 media/schema behavior inherits its earlier targeted evidence.')
    for name in schema_paths:
        file=declarations/'schemas'/(name+'.json')
        metadata['chat_protocol'][name+'_schema']=str(file)
        metadata['chat_protocol'][name+'_schema_sha256']=sha(file)
    metadata['release_declarations']=dict(profile=p.COMPLETION_DECLARATION_PROFILE,
        directory=str(declarations), binary_sha256=reviewed['binary_sha256'], protocol_sha256=protocol,
        files={n:sha(declarations/n) for n in p.declaration_files(p.COMPLETION_DECLARATION_PROFILE)})
    metadata['scope']=dict(production='Existing text/MTP and opt-in vision/schema tools; '
        'native OpenAI Completions/Models with optional aliases and native ingress.',
        correctness='Targeted CPU and seven actual business requests; no full model-quality rerun.',
        performance='Functional compatibility work; no performance acceptance claim.',
        installation_health='Portable verified package and stopped-service help/status; no model start.',
        boundaries='Responses, persistent APIs and other master-plan tasks continue; user owns long stability.')
    metadata['completions_acceptance']=dict(profile='q4t-completions-v1', decision=str(OUT/'decision.json'),
        prompt_logprobs=True, best_of=True, batch=True, suffix_fim=True,
        model_registry=True, full_openai_compatibility=False)
    notices=metadata['third_party_notices']
    notices['source_directory']=str(engine/'licenses/structured_output'); notices['files']={}
    for file in sorted((ROOT/'third_party/licenses/structured_output').rglob('*')):
        if file.is_file():
            name=str(file.relative_to(ROOT/'third_party/licenses/structured_output'))
            copy_file(file, engine/'licenses/structured_output'/name)
            notices['files'][name]=dict(sha256=sha(file), text=file.read_text())
    copy_file('/usr/share/doc/libidn2-0/copyright', engine/'licenses/system-libidn2-copyright')
    save(engine/'q4t.release.json', metadata)
    p.verify_declarations(metadata, required=True)
    native=dict(version=1, source_files={n:sha(ROOT/n) for n in sorted(bundle.NATIVE_SOURCES)},
        guardian_sha256=sha(BUILD/'q4t-guardian'), ingress_sha256=sha(BUILD/'q4t-ingress'),
        cuda_probe_sha256=sha(old/'bin/q4t-cuda-probe'), provenance=dict(
            build=str(BASE/'build-05/result.json'),
            native_cpu=[str(BASE/n/'result.json') for n in ('native-cpu-01','tls-cpu-01')],
            cuda_probe_reused_from=str(old/'native-build.json')))
    require(native['guardian_sha256'] == sha(old/'bin/q4t-guardian'), 'unexpected Guardian ELF change')
    require(native['source_files']['src/service/cuda_probe.cu'] ==
        read(old/'native-build.json')['source_files']['src/service/cuda_probe.cu'], 'CUDA probe drift')
    save(OUT/'native-build.json', native)
    receipt=bundle.prepare(engine/'q4t',engine/'q4t.release.json',plan['model'],
        BUILD/'q4t-guardian',old/'bin/q4t-cuda-probe',None,
        old/'gateway-policy.json',old/'engine/client-policy.json',OUT/'bundle',
        native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress')
    save(OUT/'receipt.json',receipt)
    bundle.verify(receipt['package'], receipt['manifest_sha256'])
    save(BASE/'independent-review.json',dict(status='PASS_COMPLETIONS_DELIVERY_REVIEW',
        source_commit=commit, source_files=len(names), tested_runtime_commit=plan['source_commit'],
        totals=decision['totals'], default_unchanged=True, release=receipt))
    print(json.dumps(dict(release=receipt,totals=decision['totals'],changed_control=len(changed_control))))


if __name__ == '__main__': main()
