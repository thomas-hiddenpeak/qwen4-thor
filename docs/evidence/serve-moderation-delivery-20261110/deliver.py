"""Publish accepted A07.7b native moderation, retaining A07.7a and business journals."""
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

BASE = ROOT/'.q4t-work/serve-moderation-20261110'
PREVIOUS_REVISION, NEXT_REVISION = 22, 24
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
    accepted = p.read(EVIDENCE/'serve-moderation-business-20261110/decision.json')
    plan = p.read(BASE/'business-01/plan.json')
    p.require(accepted['status'] == 'PASS_RESPONSES_MODERATION_TARGETED_BUSINESS' and
              accepted['all_nvml_ok'] and accepted['cleanup_confirmed'], 'business gate')
    p.require(all(p.sha(ROOT/n) == h for n, h in plan['sources'].items()), 'source drift')
    p.require(all(p.sha(n) == h for n, h in plan['protected'].items()), 'default drift')
    p.require(p.sha(BUILD/'q4t') == accepted['binary_sha256'], 'engine drift')
    current = publication.status(STATE)
    p.require(current['ready'] and current['revision'] == PREVIOUS_REVISION, 'publication predecessor changed')
    old = (DEFAULT/'service').resolve(strict=True)
    old_hash = p.sha(old/'manifest.json')
    p.require(old_hash == current['current']['manifest_sha256'] ==
              '0837b2a7e2556fbb62255512d9f93b71eae57855b7fde8e09fb7f6c4bc8f9e16', 'A07.7a predecessor required')
    bundle.verify(old, old_hash)
    p.require(all(p.sha(ROOT/n) == p.sha(old/'source'/n) for n in bundle.SOURCES),
              'control changed outside moderation feature')
    native = p.read(BASE/'native-receipt.json')
    prior_native = p.read(ROOT/'.q4t-work/serve-reasoning-pro-20261110/native-receipt.json')
    changed = {n for n,h in prior_native['source_files'].items() if p.sha(ROOT/n) != h}
    p.require(changed == {'CMakeLists.txt'}, 'unexpected native source drift')
    build_diff = subprocess.check_output(['git','diff','ab4b3927','--','CMakeLists.txt'],text=True)
    p.require(all(p.sha(ROOT/n) == h for n,h in native['source_files'].items()), 'native source drift')
    p.require(not (BASE/'engine-build-02.stderr').read_bytes(), 'native build warning/error')
    for name,key in [('q4t-ingress','ingress_sha256'),('q4t-guardian','guardian_sha256'),
                     ('q4t-cuda-probe','cuda_probe_sha256')]:
        p.require(p.sha(BUILD/name) == native[key] == prior_native[key], 'native binary drift')
    caps = p.read(BASE/'business-01/capabilities-1.json')
    config = p.read(BASE/'business-01/config-1.json')
    protocol_sources = p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    p.require(all(p.sha(ROOT/n) == h for n, h in protocol_sources.items()), 'embedded protocol drift')
    protocol = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(protocol_sources.items())).encode()).hexdigest()
    p.require(protocol == accepted['protocol_sha256'] == caps['protocol_source_sha256'] ==
              config['protocol_source_sha256'], 'actual protocol drift')
    before = state_hashes()
    OUT.mkdir(mode=0o700)
    (OUT/'native-cmake-review.diff').write_text(build_diff)
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
        numeric_defaults_preserved=True, actual_business='One model start, ten external requests, twenty-eight actual model steps. Real JSON classification and input/output blocking, private SSE and background observations, compaction/pro/summary, encrypted state and actual document tool loop. A fixed cumulative-input client cannot execute its output classifier; score reports that error and retains the correct answer, and ordinary Chat continues with the same PID. Prewarm classifies input only and generates zero business output. Exact per-client/global token accounting includes auxiliary work. Test policies16RPM/131072TPM and limited client are isolated from defaults.',
        reuse='Unchanged native ingress/Guardian/probe/control source and binary reuse accepted A07.7a. Moderation29 C++/2schema/6driver CPU and actual ten-request business accepted. First compile/fixture/inventory preparation failures retained; no model retry or full correctness/performance/long-stability claim.',
        installation_checks=['sealed check','retained A07.7a selected check','sealed legacy check',
            'publish revision22','default check','default status','default observe','default publication'],
        model_starts=0, chats=0))
    decision = dict(status='PASS_TARGETED_RESPONSES_MODERATION_RELEASE',
        acceptance='ACCEPT_TARGETED_RESPONSES_MODERATION', actual_business=accepted, source_commit=commit,
        binary_sha256=accepted['binary_sha256'], protocol_sha256=protocol,
        source_review_sha256=p.sha(OUT/'review.json'), full_model_quality=False,
        performance_acceptance=False, long_stability_accepted=False, remaining_master_tasks_open=True)
    p.save(OUT/'decision.json', decision)
    metadata = p.read(OUT/'previous/q4t.release.json')
    metadata.update(version='serve-moderation-20261110', source_commit=commit,
        binary_sha256=accepted['binary_sha256'], acceptance='ACCEPT_TARGETED_RESPONSES_MODERATION',
        acceptance_decision=str(OUT/'decision.json'), acceptance_decision_sha256=p.sha(OUT/'decision.json'),
        build_cache=str(engine/'CMakeCache.txt'), build_cache_sha256=p.sha(engine/'CMakeCache.txt'),
        tested_source_manifest=str(engine/'runtime-source.json'), tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),
        observed_configuration=config, inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),sha256=p.sha(OUT/'previous/q4t.release.json')),
        previous_binary=str(OUT/'previous/q4t'), previous_release=str(OUT/'previous/q4t.release.json'),
        source_note='Native actual Qwen classification of message/tool content with 13 category scores and independent score/block policies. Blocked input never enters business generation; held output is approved before SSE/state publication. Classifier failure does not restart model; score preserves completed business output. Independent bounded classifier output, separate actual usage, cumulative client TPM and one RPM; no cloud model equivalence, calibration, full-model-quality, performance or long-stability claim.')
    declarations = engine/'declarations'
    declarations.mkdir(mode=0o700)
    p.save(declarations/'capabilities.json', caps)
    p.save(declarations/'configuration.json', config)
    metadata['chat_protocol'].update(caps, protocol_sha256=protocol,
        declaration_evidence='Actual A07.7b moderation discovery with isolated synthetic clients. Existing managed policy and default output256 remain; test default384 and classifier512 are separately recorded.')
    for name in p.declaration_schemas(p.APPLICATION_DECLARATION_PROFILE):
        path = declarations/'schemas'/(name+'.json')
        source = {'config': ROOT/'docs/serve-config-v1.schema.json',
                  'application': ROOT/'docs/serve-application-v1.schema.json',
                  'openapi': ROOT/'docs/serve-openapi-bundle-v1.json'}.get(
                      name, old/'engine/declarations/schemas'/(name+'.json'))
        copy_file(source, path)
        metadata['chat_protocol'][name+'_schema'] = str(path)
        metadata['chat_protocol'][name+'_schema_sha256'] = p.sha(path)
    metadata['release_declarations'] = dict(profile=p.APPLICATION_DECLARATION_PROFILE,
        directory=str(declarations),binary_sha256=accepted['binary_sha256'],protocol_sha256=protocol,
        files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    metadata['responses_moderation_acceptance'] = dict(
        executor='native_actual_served_qwen', categories=13, threshold=0.5,
        independent_score_and_block=True, model_identity_preserved=True,
        input_before_business=True, output_before_publication=True,
        sse_get_background_private_until_approved=True,
        actual_tool_arguments_and_results=True, metadata_excluded_from_scores=True,
        auxiliary_output_limit_per_step=512, maximum_classifier_steps=2,
        separate_usage=True, exact_cumulative_tpm=True, single_rpm=True,
        actual_compaction_pro_summary_encryption_combination=True,
        prewarm_only_input_review=True, classifier_failure_no_model_restart=True,
        score_failure_keeps_actual_answer=True, genuine_device_protection_preserved=True,
        cloud_model_equivalence=False, calibrated_safety_quality_claim=False)
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
    copy_file('/usr/share/doc/libssl3t64/copyright',engine/'licenses/system-openssl-copyright')
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
    p.save(OUT/'rollback.json',dict(previous=current['current'],previous_revision=PREVIOUS_REVISION,
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
            control_review=str(OUT/'review.json'),client_artifacts=str(OUT/'clients'),responses_moderation_targeted_accepted=True,
            rollback_receipt=str(OUT/'rollback.json'),rollback_sha256=p.sha(OUT/'rollback.json'),
            scope=service['scope']+' Native actual moderation, input/output blocking, private publication, auxiliary accounting and healthy same-PID continuation on classifier failure.')
        p.save(OUT/'service.release.json',service)
        request = str(uuid.uuid4())
        p.save(OUT/'publish-request.json',dict(request_id=request,expected_revision=PREVIOUS_REVISION,
            expected_manifest_sha256=old_hash,target_manifest_sha256=digest))
        published = run('publish',[cli,'service','publish',*state_args,'--default-root',DEFAULT,
            '--package',package,'--manifest-sha256',digest,'--service-release',OUT/'service.release.json',
            '--expected-manifest-sha256',old_hash,'--expected-revision',PREVIOUS_REVISION,'--request-id',request])
        p.require(published['durable'] and published['revision']==NEXT_REVISION,'publish result')
        for name in ('check','status','observe'):
            value = run('default-'+name,[DEFAULT/'q4t','service',name,*state_args])
            p.require(value['status']=='PASS' if name=='check' else value['state']=='STOPPED','default '+name)
        final = run('default-publication',[DEFAULT/'q4t','service','publish-status',*state_args])
        p.require(final['ready'] and final['revision']==NEXT_REVISION and final['current']['manifest_sha256']==digest,'publication selection')
        p.save(OUT/'result.json',dict(status='PASS_RESPONSES_MODERATION_DELIVERED',current=final['current'],
            previous=current['current'],publication_revision=NEXT_REVISION,commands=rows,state='STOPPED',
            business_state_preserved=state_hashes()==before,model_starts=0,chats=0,gpu_queries=0,
            default_changed=True,main_merged=False,remaining_master_tasks_open=True))
    except BaseException as error:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,category=type(error).__name__,detail=str(error),
            business_state_preserved=state_hashes()==before,recovery='Inspect publication and use same request resume only if already prepared.'))
        raise
    print(json.dumps(dict(status='PASS_RESPONSES_MODERATION_DELIVERED',package=digest,revision=NEXT_REVISION,commands=len(rows))))


if __name__ == '__main__':
    os.umask(0o077)
    main()
