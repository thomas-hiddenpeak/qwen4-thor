"""Publish accepted native Batch execution with a compatible state/quota rollback."""
import copy
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

BASE = ROOT/'.q4t-work/serve-batches-20261110'
OUT = BASE/'release-01'
BUSINESS = BASE/'business-01'
BUILD = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
ROLLBACK_ROOT = BASE/'rollback-source'
ROLLBACK_BUILD = ROLLBACK_ROOT/'.q4t-work/engine-build'
DEFAULT = ROOT/'build'
STATE = DEFAULT/'service-native-state'
ROLLBACK_COMMIT = 'fc69f823723b40eec19dec9b2e861a2ace20e46f'
SCHEMAS = dict(config='serve-config-v1', message='serve-message-profile-v3',
    preflight='serve-preflight-v1', reasoning='serve-reasoning-v1',
    response='serve-transport-v1', stop='serve-stop-v1', thinking='serve-thinking-v1',
    completion_request='serve-completion-request-v1',
    completion_response='serve-completion-transport-v1',
    application='serve-application-v1', openapi='serve-openapi-bundle-v1')


def state_hashes():
    return {str(f.relative_to(STATE)): p.sha(f) for f in STATE.rglob('*')
            if f.is_file() and (f.name == 'ledger.json' or
                               f.relative_to(STATE).parts[0].startswith('client-'))}


def source_hashes(root):
    names = subprocess.check_output(['git', 'ls-files', '-z', 'src', 'include',
        'cmake', 'CMakeLists.txt'], cwd=root).decode().split('\0')
    return {n:p.sha(root/n) for n in names if n}


def main():
    p.require(not OUT.exists(), 'release exists; inspect before resuming')
    p.require(not subprocess.check_output(['git','status','--porcelain']), 'commit first')
    accepted = p.read(BUSINESS/'acceptance.json')
    plan = p.read(BUSINESS/'plan.json')
    p.require(accepted['status']=='ACCEPT_BATCH_BUSINESS_AND_FORMAT_COMPATIBILITY'
        and accepted['all_queries_ok'] and not accepted['env_abort']
        and all(accepted['postflight'].values()), 'business acceptance')
    drift = {n for n,h in plan['sources'].items() if p.sha(ROOT/n)!=h}
    p.require(not drift, 'unreviewed source drift')
    p.require(all(p.sha(n)==h for n,h in plan['protected'].items()), 'default drift')
    p.require(p.sha(BUILD/'q4t')==accepted['candidate_sha256'] and
        p.sha(ROLLBACK_BUILD/'q4t')==accepted['compatible_reader_sha256'], 'engine drift')
    p.require(subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROLLBACK_ROOT,
        text=True).strip()==ROLLBACK_COMMIT and not subprocess.check_output(
        ['git','status','--porcelain'],cwd=ROLLBACK_ROOT), 'rollback source drift')
    current = publication.status(STATE)
    old = (DEFAULT/'service').resolve(strict=True)
    old_hash = p.sha(old/'manifest.json')
    p.require(current['ready'] and current['revision']==40 and
        old_hash==current['current']['manifest_sha256']==
        '0dc8948d8847ea11776313477b67da0d93687b3608568c38bd888be0834700ea',
        'publication predecessor changed')
    bundle.verify(old, old_hash)
    controls = {n for n in bundle.SOURCES if p.sha(ROOT/n)!=p.sha(old/'source'/n)}
    p.require(controls=={'tools/serve_recovery/usage_policy.py'}, 'control source drift')
    prior_native = p.read(old/'native-build.json')
    changes = {n for n,h in prior_native['source_files'].items() if p.sha(ROOT/n)!=h}
    p.require(changes=={'CMakeLists.txt','include/q4t/server/application_routes.h',
        'include/q4t/server/http_connection.h','src/service/ingress_main.cpp'}, 'native drift')
    p.require(not bundle.NATIVE_SOURCES-prior_native['source_files'].keys(), 'native inventory drift')
    p.require(p.sha(BUILD/'q4t-ingress')==
        '1b3ea6309fbf142804be7ec5832f4bd234124eecf4f63abd33db08b2da8bb484', 'ingress drift')
    for name,key in [('q4t-guardian','guardian_sha256'),('q4t-cuda-probe','cuda_probe_sha256')]:
        p.require(p.sha(BUILD/name)==prior_native[key], 'unchanged Guardian/probe required')
    before = state_hashes()
    OUT.mkdir(mode=0o700)
    commit = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    native = dict(version=1,source_files={n:p.sha(ROOT/n) for n in sorted(bundle.NATIVE_SOURCES)},
        guardian_sha256=p.sha(BUILD/'q4t-guardian'),cuda_probe_sha256=p.sha(BUILD/'q4t-cuda-probe'),
        ingress_sha256=p.sha(BUILD/'q4t-ingress'))
    p.save(OUT/'native-build.json',native)
    for name in ('q4t','q4t.release.json','service.release.json'):
        copy_file((DEFAULT/name).resolve(strict=True), OUT/'previous'/name)
    p.save(OUT/'review.json',dict(source_commit=commit,controls=sorted(controls),
        native_changed=sorted(changes),native_added=[],
        business_state_before=before,previous=current,accepted_business=accepted,
        controls_review='Only usage-policy v2 dual-domain validation and cross-boot expiry; exact CPU evidence retained. Existing controller and publication code unchanged.',
        native_review='Batch routing and independent observation/cancellation accepted by actual SDK core/TLS-prefix41HTTP CPU evidence. Unchanged Guardian/probe reused.',
        binary_reuse='No compiled source or binary changed after real business. Sealed native CLI dispatches hash-verified package sources, not standalone build service-tools.',
        rollback_review='Separately built fc69f823 engine read all four original Batch roots and five byte-identical JSONL Files, original background Response and v2 dual quota snapshot, then executed a new Batch. Current accepted ingress/control source and existing explicit client limits.',
        model_starts=0,chats=0,gpu_queries=0,full_performance=False))
    old_engine = p.read(old/'engine/manifest.json')
    old_metadata = p.read(OUT/'previous/q4t.release.json')
    receipts = {}

    for label,build,source,observation in [('rollback-reader',ROLLBACK_BUILD,ROLLBACK_ROOT,2),
                                         ('candidate',BUILD,ROOT,1)]:
        directory = OUT/label
        engine = directory/'engine'
        engine.mkdir(parents=True,mode=0o700)
        for name in ('q4t','CMakeCache.txt'):
            copy_file(build/name,engine/name)
        runtime = source_hashes(source)
        p.save(engine/'runtime-source.json',runtime)
        protocol_sources = p.read(build/'generated/q4t/server/chat_protocol_sources.json')
        p.require(all(p.sha(source/n)==h for n,h in protocol_sources.items()), 'embedded protocol drift')
        protocol = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(protocol_sources.items())).encode()).hexdigest()
        caps = p.read(BUSINESS/f'capabilities-{observation}.json')
        config = p.read(BUSINESS/f'config-{observation}.json')
        p.require(caps['protocol_source_sha256']==config['protocol_source_sha256']==protocol, 'observed protocol drift')
        decision = dict(status='PASS_TARGETED_BATCH_'+label.upper().replace('-','_'),
            acceptance='ACCEPT_TARGETED_BATCH_EXECUTION',binary_sha256=p.sha(engine/'q4t'),
            engine_source_commit=ROLLBACK_COMMIT if observation==2 else '1d50d9dc784db0ff42d4e466309852d0b549ae06',
            package_source_commit=commit,protocol_sha256=protocol,actual_business=accepted,
            source_review_sha256=p.sha(OUT/'review.json'),full_model_quality=False,
            performance_acceptance=False,long_stability_accepted=False,remaining_master_tasks_open=True)
        p.save(directory/'decision.json',decision)
        metadata = copy.deepcopy(old_metadata)
        metadata.update(version='serve-batches-20261111-'+label,
            source_commit=ROLLBACK_COMMIT if observation==2 else commit,
            binary_sha256=decision['binary_sha256'],acceptance=decision['acceptance'],
            acceptance_decision=str(directory/'decision.json'),acceptance_decision_sha256=p.sha(directory/'decision.json'),
            build_cache=str(engine/'CMakeCache.txt'),build_cache_sha256=p.sha(engine/'CMakeCache.txt'),
            tested_source_manifest=str(engine/'runtime-source.json'),tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),
            observed_configuration=config,previous_binary=str(OUT/'previous/q4t'),
            previous_release=str(OUT/'previous/q4t.release.json'),
            inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),sha256=p.sha(OUT/'previous/q4t.release.json')),
            source_note='Native Batch Chat/Completions/Responses, original background terminal, tools SSE, failed/cancelled actual usage and cross-version receipts/dual quota accepted. Engine source and package sources tracked separately. Prior file-inputs default is archival; it cannot consume new Batch/quota state.')
        declarations = engine/'declarations';declarations.mkdir()
        p.save(declarations/'capabilities.json',caps);p.save(declarations/'configuration.json',config)
        metadata['chat_protocol'].update(caps,protocol_sha256=protocol,
            declaration_evidence='Actual isolated MTP Batch business discovery. Package preserves existing default text-MTP profile, output256, client policies and object64/256MiB; test output1536 and object256/512MiB do not change deployment defaults.')
        for name,stem in SCHEMAS.items():
            relative = 'docs/'+stem+('.json' if name=='openapi' else '.schema.json')
            dest = declarations/'schemas'/(name+'.json');dest.parent.mkdir(exist_ok=True)
            if observation==2:
                dest.write_bytes(subprocess.check_output(['git','show',ROLLBACK_COMMIT+':'+relative]))
                dest.chmod(0o400)
            else:copy_file(ROOT/relative,dest)
            metadata['chat_protocol'][name+'_schema']=str(dest)
            metadata['chat_protocol'][name+'_schema_sha256']=p.sha(dest)
        metadata['release_declarations']=dict(profile=p.APPLICATION_DECLARATION_PROFILE,
            directory=str(declarations),binary_sha256=decision['binary_sha256'],protocol_sha256=protocol,
            files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
        metadata['file_input_acceptance']['batch_pending']=True
        metadata['file_input_acceptance']['text_batch_delivered']=True
        metadata['batch_acceptance']=dict(
            real_endpoints=['/v1/chat/completions','/v1/completions','/v1/responses'],
            actual_tool_sse=True, background_original_terminal=True,
            failed_cancelled_actual_usage=True, original_results_preserved=True,
            dual_quota_cross_version=True, compatible_reader_sha256=accepted['compatible_reader_sha256'],
            original_deadlines=True, finite_capacity_failure_cpu=True,
            auxiliary_initialization_recovery_cpu=observation==1,
            remaining_five_backend_execution_pending=True, full_performance=False)
        for file in (ROOT/'third_party/licenses').rglob('*'):
            if file.is_file():copy_file(file,engine/'licenses'/file.relative_to(ROOT/'third_party/licenses'))
        metadata['third_party_notices']['source_directory']=str(engine/'licenses/structured_output')
        metadata['application_notices']['source_directory']=str(engine/'licenses/openai-node')
        system_notices={}
        for name in ('libidn2-0','libssl3t64','poppler-utils','libarchive13t64','libxml2',
                     'libreoffice-core','libwebpdecoder3','zlib1g'):
            file=Path('/usr/share/doc')/name/'copyright'
            copy_file(file.resolve(strict=True),engine/'licenses/system'/name)
            system_notices[name]=dict(path=str(file),sha256=p.sha(file.resolve(strict=True)),
                artifact=str(engine/'licenses/system'/name),redistributed_binary=False)
        metadata['file_input_system_dependencies']=system_notices
        clients=metadata['client_artifacts']
        for name,digest in clients['files'].items():
            p.require(p.sha(ROOT/name)==digest,'accepted client drift')
            copy_file(ROOT/name,directory/'clients'/name)
        clients['directory']=str(directory/'clients')
        p.save(engine/'q4t.release.json',metadata);p.verify_declarations(metadata,required=True)
        app={k:v for k,v in old_engine['settings']['application_state'].items() if k!='profile'}
        receipt=bundle.prepare(engine/'q4t',engine/'q4t.release.json',old_engine['model_dir'],
            BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,old/'gateway-policy.json',
            old/'engine/client-policy.json',directory/'bundle',native_receipt=OUT/'native-build.json',
            native_ingress=BUILD/'q4t-ingress',capacity_config=dict(version=1),application_state=app)
        p.save(directory/'receipt.json',receipt);receipts[label]=receipt
        packaged=Path(receipt['package'])
        p.require(p.read(packaged/'engine/manifest.json')['settings']==old_engine['settings'],'default settings drift')
        for name in ('gateway-policy.json','engine/client-policy.json'):
            p.require(p.sha(packaged/name)==p.sha(old/name),'credential seed drift')
        service=p.read(OUT/'previous/service.release.json')
        service.update(acceptance=decision['acceptance'],acceptance_record=str(directory/'decision.json'),
            acceptance_sha256=p.sha(directory/'decision.json'),package=str(packaged),
            manifest_sha256=receipt['manifest_sha256'],engine_binary_sha256=receipt['engine_binary_sha256'],
            engine_release_sha256=p.sha(packaged/'engine/q4t.release.json'),engine_metadata_unchanged=False,
            previous_acceptance_record=service['acceptance_record'],control_review=str(OUT/'review.json'),
            client_artifacts=str(directory/'clients'),batch_targeted_accepted=True,
            rollback_receipt=str(OUT/'rollback.json'),rollback_sha256=None,
            scope=service['scope']+' Native Batch execution, actual failed/cancelled usage, original backend terminal, tools SSE, state/quota compatible rollback accepted. Defaults/credentials/usage preserved. Remaining five endpoint backends and all master tasks continue.')
        # The selected generation is bound after the reader has been published.
        p.save(directory/'service.release.json',service)

    rows=[]
    def run(label,args):
        start=time.monotonic();argv=[str(x) for x in args]
        value=subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,timeout=45)
        (OUT/(label+'.stdout')).write_bytes(value.stdout);(OUT/(label+'.stderr')).write_bytes(value.stderr)
        row=dict(label=label,argv=argv,returncode=value.returncode,seconds=time.monotonic()-start)
        rows.append(row);p.save(OUT/(label+'.json'),row)
        p.require(value.returncode==0,'command failed: '+label)
        p.require(state_hashes()==before,'business state changed: '+label)
        return json.loads(value.stdout)
    try:
        cli=Path(receipts['candidate']['package'])/'engine/q4t'
        p.require(run('previous-check',[cli,'service','check','--state',STATE,
            '--package',old,'--manifest-sha256',old_hash])['desired']=='STOPPED','prior service active')
        for label,receipt in receipts.items():
            package=Path(receipt['package'])
            for suffix,entry in [('native',package/'engine/q4t'),('legacy',package/'bin/q4t')]:
                p.require(run(label+'-'+suffix+'-check',[entry,'service','check'])['status']=='PASS','sealed check')
        previous=current
        for label,revision in [('rollback-reader',42),('candidate',44)]:
            receipt=receipts[label];package=Path(receipt['package']);digest=receipt['manifest_sha256']
            request=str(uuid.uuid4())
            p.save(OUT/(label+'-publish-request.json'),dict(request_id=request,
                expected_revision=previous['revision'],expected_manifest_sha256=previous['current']['manifest_sha256'],
                target_manifest_sha256=digest))
            if label=='candidate':
                rollback=dict(reader=previous['current'],revision=previous['revision'],
                    old_file_inputs_batch_format_compatible=False,original_business_records_must_be_preserved=True,
                    command='q4t service publish-rollback --state STATE --default-root ROOT --generation-sha256 READER_GENERATION --expected-manifest-sha256 CURRENT_MANIFEST --expected-revision CURRENT_REVISION --request-id NEW_UUID',
                    capacity='Use retained fc69 reader with current original Batch/Files/Responses and dual-domain quota; existing policies fit both. Never delete business records or silently reset minute budgets.')
                p.save(OUT/'rollback.json',rollback)
                path=OUT/label/'service.release.json';service=p.read(path)
                service['rollback_sha256']=p.sha(OUT/'rollback.json');p.save(path,service)
            published=run(label+'-publish',[cli,'service','publish','--state',STATE,
                '--default-root',DEFAULT,'--package',package,'--manifest-sha256',digest,
                '--service-release',OUT/label/'service.release.json','--expected-manifest-sha256',
                previous['current']['manifest_sha256'],'--expected-revision',previous['revision'],'--request-id',request])
            p.require(published['durable'] and published['revision']==revision,'publication result')
            for action in ('check','status','observe'):
                value=run(label+'-default-'+action,[DEFAULT/'q4t','service',action,'--state',STATE])
                p.require(value['status']=='PASS' if action=='check' else value['state']=='STOPPED','default '+action)
            previous=run(label+'-publication',[DEFAULT/'q4t','service','publish-status','--state',STATE])
            p.require(previous['ready'] and previous['revision']==revision and
                previous['current']['manifest_sha256']==digest,'selected publication')
        p.save(OUT/'result.json',dict(status='PASS_BATCH_EXECUTION_DELIVERED',current=previous['current'],
            compatible_rollback=p.read(OUT/'rollback.json'),publication_revision=44,commands=rows,
            state='STOPPED',business_state_preserved=state_hashes()==before,model_starts=0,chats=0,
            gpu_queries=0,default_changed=True,main_merged=False,remaining_master_tasks_open=True))
    except BaseException as error:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,category=type(error).__name__,detail=str(error),
            business_state_preserved=state_hashes()==before,recovery='Inspect publication and original intents before resuming. No automatic replay or model restart.'))
        raise
    print(json.dumps(dict(status='PASS_BATCH_EXECUTION_DELIVERED',revision=44,commands=len(rows))))


if __name__=='__main__':
    os.umask(0o077)
    main()
