"""Publish accepted A07.2 while retaining actual A07.1 rollback and business state."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, publication
from tools.serve_service import package as p
from tools.serve_progress.prepare import copy_file

BASE = ROOT/'.q4t-work/serve-responses-summary-20261109'
OUT = BASE/'release-01'
BUILD = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
DEFAULT = ROOT/'build'
STATE = DEFAULT/'service-native-state'
EVIDENCE = ROOT/'docs/evidence'


def state_hashes():
    return {str(f.relative_to(STATE)): p.sha(f) for f in STATE.rglob('*')
            if f.is_file() and (f.name == 'ledger.json' or
                               f.relative_to(STATE).parts[0].startswith('client-'))}


def main():
    p.require(not OUT.exists(), 'release exists; inspect before resuming')
    p.require(not subprocess.check_output(['git', 'status', '--porcelain']), 'commit sources first')
    accepted = p.read(EVIDENCE/'serve-responses-summary-business-20261109/decision.json')
    plan = p.read(BASE/'business-02/plan.json')
    p.require(accepted['status'] == 'PASS_RESPONSES_SUMMARY_TARGETED_BUSINESS' and
              accepted['all_nvml_ok'] and accepted['cleanup_confirmed'], 'business gate')
    p.require(all(p.sha(ROOT/n) == h for n, h in plan['sources'].items()), 'source drift')
    p.require(all(p.sha(n) == h for n, h in plan['protected'].items()), 'default drift')
    p.require(p.sha(BUILD/'q4t') == accepted['binary_sha256'], 'engine drift')
    current = publication.status(STATE)
    p.require(current['ready'] and current['revision'] == 10, 'publication predecessor changed')
    old = (DEFAULT/'service').resolve(strict=True)
    old_hash = p.sha(old/'manifest.json')
    p.require(old_hash == current['current']['manifest_sha256'] ==
              'c360746edc5a717eeef03b6de0261b0b72630cba6e0d1f5d5b5b5c9c8ad94f79', 'A07.1 predecessor required')
    bundle.verify(old, old_hash)
    p.require(all(p.sha(ROOT/n) == p.sha(old/'source'/n) for n in bundle.SOURCES),
              'control changed outside summary feature')
    native = p.read(ROOT/'.q4t-work/serve-responses-context-20261109/release-04/native-build.json')
    changed = {n for n, h in native['source_files'].items() if p.sha(ROOT/n) != h}
    p.require(changed == {'CMakeLists.txt'}, 'unexpected native source drift')
    build_diff = subprocess.check_output(['git','diff','d568fea9','--','CMakeLists.txt'],text=True)
    added = [v for v in build_diff.splitlines() if v.startswith('+') and not v.startswith('+++')]
    removed = [v for v in build_diff.splitlines() if v.startswith('-') and not v.startswith('---')]
    p.require(added == ['+    src/server/responses_summary.cpp'] * 2 and not removed,
              'native CMake changes beyond engine/host summary source registration')
    p.require(not (BASE/'native-build.stderr').read_bytes(), 'native build warning/error')
    native['source_files']['CMakeLists.txt'] = p.sha(ROOT/'CMakeLists.txt')
    native['provenance'] = 'Necessary native target build completed; CMake registers summary source only for engine/host tests. All native implementation inputs and three ELFs unchanged from accepted A07.1/N06.'
    for name, key in [('q4t-ingress', 'ingress_sha256'), ('q4t-guardian', 'guardian_sha256'),
                      ('q4t-cuda-probe', 'cuda_probe_sha256')]:
        p.require(p.sha(BUILD/name) == native[key], 'native binary drift')
    caps = p.read(BASE/'business-02/capabilities.json')
    config = p.read(BASE/'business-02/config.json')
    protocol_sources = p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    p.require(all(p.sha(ROOT/n) == h for n, h in protocol_sources.items()), 'embedded protocol drift')
    protocol = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(protocol_sources.items())).encode()).hexdigest()
    p.require(protocol == accepted['protocol_sha256'] == caps['protocol_source_sha256'] ==
              config['protocol_source_sha256'], 'actual protocol drift')
    before = state_hashes()
    OUT.mkdir(mode=0o700)
    engine = OUT/'engine'
    for n in ('q4t', 'CMakeCache.txt'):
        copy_file(BUILD/n, engine/n)
    for n in ('q4t', 'q4t.release.json', 'service.release.json'):
        copy_file((DEFAULT/n).resolve(strict=True), OUT/'previous'/n)
    runtime = {n:h for n,h in plan['sources'].items() if n.startswith(('src/', 'include/', 'cmake/')) or n == 'CMakeLists.txt'}
    p.save(engine/'runtime-source.json', runtime)
    commit = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    p.save(OUT/'review.json', dict(source_commit=commit, native=native,
        controls={n:p.sha(ROOT/n) for n in bundle.SOURCES}, prior=current,
        business_state_before=before, runtime_sources=runtime,
        numeric_defaults_preserved=True, actual_business='Direct native engine with synthetic policy, MTP3, context8192/prefill2048, output default384. Existing managed clients/default output256 remain; summary consumes the remaining explicit request budget.',
        reuse='Unchanged native control/ingress/Guardian and N07 rollback. New targeted CPU + actual Qwen summaries, SSE replay fix, client accounting and document tool business accepted; first three generations reused by unchanged generation source identity.',
        installation_checks=['sealed check','retained A07.1 selected check','sealed legacy check',
            'publish revision10','default check','default status','default observe','default publication'],
        model_starts=0, chats=0))
    decision = dict(status='PASS_TARGETED_RESPONSES_SUMMARY_RELEASE',
        acceptance=accepted['acceptance'], actual_business=accepted, source_commit=commit,
        binary_sha256=accepted['binary_sha256'], protocol_sha256=protocol,
        source_review_sha256=p.sha(OUT/'review.json'), full_model_quality=False,
        performance_acceptance=False, long_stability_accepted=False, remaining_master_tasks_open=True)
    p.save(OUT/'decision.json', decision)
    metadata = p.read(OUT/'previous/q4t.release.json')
    metadata.update(version='serve-responses-summary-20261109', source_commit=commit,
        binary_sha256=accepted['binary_sha256'], acceptance=accepted['acceptance'],
        acceptance_decision=str(OUT/'decision.json'), acceptance_decision_sha256=p.sha(OUT/'decision.json'),
        build_cache=str(engine/'CMakeCache.txt'), build_cache_sha256=p.sha(engine/'CMakeCache.txt'),
        tested_source_manifest=str(engine/'runtime-source.json'), tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),
        observed_configuration=config, inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),sha256=p.sha(OUT/'previous/q4t.release.json')),
        previous_binary=str(OUT/'previous/q4t'), previous_release=str(OUT/'previous/q4t.release.json'),
        source_note='Actual native Responses summaries share output budget and cancellation, count one request and charge extra model input/output. Retained SSE replay now completes HTTP framing. No model numerical/performance/full-quality claim.')
    declarations = engine/'declarations'
    declarations.mkdir(mode=0o700)
    p.save(declarations/'capabilities.json', caps)
    p.save(declarations/'configuration.json', config)
    metadata['chat_protocol'].update(caps, protocol_sha256=protocol,
        declaration_evidence='Actual A07.2 discovery on native engine with synthetic test client policy. Managed existing clients and default output256 retained separately from test default384.')
    for name in p.declaration_schemas(p.APPLICATION_DECLARATION_PROFILE):
        path = declarations/'schemas'/(name+'.json')
        copy_file(old/'engine/declarations/schemas'/(name+'.json'), path)
        metadata['chat_protocol'][name+'_schema'] = str(path)
        metadata['chat_protocol'][name+'_schema_sha256'] = p.sha(path)
    metadata['release_declarations'] = dict(profile=p.APPLICATION_DECLARATION_PROFILE,
        directory=str(declarations),binary_sha256=accepted['binary_sha256'],protocol_sha256=protocol,
        files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    metadata['responses_summary_acceptance'] = dict(modes=['concise','detailed','auto'],
        actual_model_summary=True, distinct_derived_item=True, shared_output_budget=True,
        shared_cancellation=True, single_request_admission=True, extra_input_output_charged=True,
        foreground_and_background=True, retained_sse_framing=True,
        evidence=str(EVIDENCE/'serve-responses-summary-business-20261109/decision.json'))
    metadata['scope']['production'] += '; actual Responses summaries with shared budget/client accounting and complete retained SSE framing.'
    notices = metadata['third_party_notices']
    notices['source_directory'] = str(engine/'licenses/structured_output')
    notices['files'] = {}
    for file in sorted((ROOT/'third_party/licenses').rglob('*')):
        if file.is_file():
            name = str(file.relative_to(ROOT/'third_party/licenses'))
            copy_file(file, engine/'licenses'/name)
            if name.startswith('structured_output/'):
                notices['files'][name[len('structured_output/'):]] = dict(sha256=p.sha(file),text=file.read_text())
    metadata['application_notices'] = dict(source_directory=str(engine/'licenses/openai-node'),files={
        str(f.relative_to(engine/'licenses/openai-node')):p.sha(f) for f in (engine/'licenses/openai-node').rglob('*') if f.is_file()})
    copy_file('/usr/share/doc/libidn2-0/copyright',engine/'licenses/system-libidn2-copyright')
    clients = metadata['client_artifacts']
    for name, digest in clients['files'].items():
        p.require(p.sha(ROOT/name) == digest, 'prior accepted client changed')
        copy_file(ROOT/name, OUT/'clients'/name)
    metadata['client_artifacts'] = dict(directory=str(OUT/'clients'),files=clients['files'],server_dependency=False)
    p.save(engine/'q4t.release.json', metadata)
    p.verify_declarations(metadata, required=True)
    p.save(OUT/'native-build.json', native)
    receipt = bundle.prepare(engine/'q4t',engine/'q4t.release.json',p.read(old/'engine/manifest.json')['model_dir'],
        BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,old/'gateway-policy.json',old/'engine/client-policy.json',
        OUT/'bundle',native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress',capacity_config=dict(version=1))
    p.save(OUT/'receipt.json', receipt)
    package = Path(receipt['package']);digest = receipt['manifest_sha256'];cli=package/'engine/q4t'
    p.require(bundle.verify(package,digest)['version'] == 10, 'package version')
    p.require(p.read(package/'engine/manifest.json')['settings'] == p.read(old/'engine/manifest.json')['settings'], 'default capacity drift')
    for name in ('gateway-policy.json','engine/client-policy.json'):
        p.require(p.sha(package/name) == p.sha(old/name), 'client seed drift')
    p.save(OUT/'rollback.json',dict(previous=current['current'],previous_revision=10,
        command='Use current q4t service publish-rollback with retained generation/current revision/new request UUID; never restore business journals.'))
    rows = []

    def run(label, args):
        argv = [str(x) for x in args];start=time.monotonic()
        value = subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,timeout=45)
        (OUT/(label+'.stdout')).write_bytes(value.stdout);(OUT/(label+'.stderr')).write_bytes(value.stderr)
        row = dict(label=label,argv=argv,returncode=value.returncode,seconds=time.monotonic()-start)
        rows.append(row);p.save(OUT/(label+'.json'),row)
        p.require(value.returncode == 0, 'command failed: '+label)
        p.require(state_hashes() == before, 'business state changed: '+label)
        return json.loads(value.stdout)

    state_args = ['--state', STATE]
    try:
        p.require(run('sealed-check',[cli,'service','check'])['status']=='PASS','sealed package')
        p.require(run('previous-check',[cli,'service','check',*state_args,'--package',old,
            '--manifest-sha256',old_hash])['desired']=='STOPPED','previous selected')
        p.require(run('legacy-check',[package/'bin/q4t','service','check'])['status']=='PASS','legacy sealed')
        service = p.read(OUT/'previous/service.release.json')
        service.update(acceptance=decision['acceptance'],acceptance_record=str(OUT/'decision.json'),
            acceptance_sha256=p.sha(OUT/'decision.json'),previous_acceptance_record=service['acceptance_record'],
            package=str(package),manifest_sha256=digest,engine_binary_sha256=receipt['engine_binary_sha256'],
            engine_release_sha256=p.sha(package/'engine/q4t.release.json'),engine_metadata_unchanged=False,
            control_review=str(OUT/'review.json'),client_artifacts=str(OUT/'clients'),responses_summary_targeted_accepted=True,
            rollback_receipt=str(OUT/'rollback.json'),rollback_sha256=p.sha(OUT/'rollback.json'),
            scope=service['scope']+' Actual native Responses summaries, single RPM/cumulative TPM and complete retained SSE framing.')
        p.save(OUT/'service.release.json',service)
        request = str(uuid.uuid4())
        p.save(OUT/'publish-request.json',dict(request_id=request,expected_revision=10,
            expected_manifest_sha256=old_hash,target_manifest_sha256=digest))
        published = run('publish',[cli,'service','publish',*state_args,'--default-root',DEFAULT,
            '--package',package,'--manifest-sha256',digest,'--service-release',OUT/'service.release.json',
            '--expected-manifest-sha256',old_hash,'--expected-revision',10,'--request-id',request])
        p.require(published['durable'] and published['revision']==12,'publish result')
        for name in ('check','status','observe'):
            value = run('default-'+name,[DEFAULT/'q4t','service',name,*state_args])
            p.require(value['status']=='PASS' if name=='check' else value['state']=='STOPPED','default '+name)
        final = run('default-publication',[DEFAULT/'q4t','service','publish-status',*state_args])
        p.require(final['ready'] and final['revision']==12 and final['current']['manifest_sha256']==digest,'publication selection')
        p.save(OUT/'result.json',dict(status='PASS_RESPONSES_SUMMARY_DELIVERED',current=final['current'],
            previous=current['current'],publication_revision=12,commands=rows,state='STOPPED',
            business_state_preserved=state_hashes()==before,model_starts=0,chats=0,gpu_queries=0,
            default_changed=True,main_merged=False,remaining_master_tasks_open=True))
    except BaseException as error:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,category=type(error).__name__,detail=str(error),
            business_state_preserved=state_hashes()==before,recovery='Inspect publication and use same request resume only if already prepared.'))
        raise
    print(json.dumps(dict(status='PASS_RESPONSES_SUMMARY_DELIVERED',package=digest,revision=12,commands=len(rows))))


if __name__ == '__main__':
    os.umask(0o077)
    main()
