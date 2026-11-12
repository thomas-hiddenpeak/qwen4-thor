"""Seal accepted native moderation and its feature-capable retained fallback."""
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

BASE = ROOT/'.q4t-work/serve-moderations-20261111'
OUT = BASE/'release-01'
BUSINESS = BASE/'business-01'
WRITE = BASE/'write-business-01'
BASELINE = BASE/'accepted-baseline'
BUILD = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
DEFAULT = ROOT/'build'
STATE = DEFAULT/'service-native-state'
SCHEMAS = dict(config='serve-config-v1', message='serve-message-profile-v3',
    preflight='serve-preflight-v1', reasoning='serve-reasoning-v1',
    response='serve-transport-v1', stop='serve-stop-v1', thinking='serve-thinking-v1',
    completion_request='serve-completion-request-v1',
    completion_response='serve-completion-transport-v1',
    application='serve-application-v1', openapi='serve-openapi-bundle-v1')


def state_hashes():
    return {str(f.relative_to(STATE)):p.sha(f) for f in STATE.rglob('*')
        if f.is_file() and (f.name == 'ledger.json' or
                           f.relative_to(STATE).parts[0].startswith('client-'))}


def source_hashes():
    names = subprocess.check_output(['git','ls-files','src','include','cmake',
        'CMakeLists.txt'], text=True).splitlines()
    return {name:p.sha(ROOT/name) for name in names}


def main():
    p.require(not OUT.exists(), 'inspect prior release before resuming')
    p.require(not subprocess.check_output(['git','status','--porcelain']), 'commit first')
    accepted, follow = p.read(BUSINESS/'acceptance.json'), p.read(WRITE/'acceptance.json')
    p.require(accepted['status']=='ACCEPT_NATIVE_MODERATIONS_BUSINESS' and
        follow['status']=='ACCEPT_MODERATIONS_WRITE_BUSINESS' and
        all(v['all_queries_ok'] and not v['env_abort'] and all(v['postflight'].values())
            for v in (accepted,follow)), 'actual business not accepted')
    plan, old_plan = p.read(WRITE/'plan.json'), p.read(BUSINESS/'plan.json')
    p.require(all(p.sha(ROOT/n)==h for n,h in plan['sources'].items()), 'source drift')
    p.require(all(p.sha(n)==h for n,h in plan['protected'].items()), 'default drift')
    identity = p.read(BASELINE/'identity.json')
    runtime, previous_runtime = source_hashes(), p.read(BASELINE/'runtime-source.json')
    p.require(runtime.keys()==previous_runtime.keys() and
        {n for n,h in runtime.items() if h!=previous_runtime[n]}==
            {'src/server/chat_moderations.cpp'}, 'unreviewed fallback/runtime change')
    p.require(p.sha(BASELINE/'q4t')==accepted['candidate_sha256']==identity['binary_sha256']
        and p.sha(BUILD/'q4t')==follow['candidate_sha256'], 'binary drift')
    current = publication.status(STATE)
    old = (DEFAULT/'service').resolve(strict=True)
    old_hash = p.sha(old/'manifest.json')
    p.require(current['ready'] and current['revision']==44 and
        old_hash==current['current']['manifest_sha256']==
        '25061a30fdc43be7b8b1ae900f9fc2fd2b7df92bec8b006559d22268594d0ea4',
        'predecessor changed')
    bundle.verify(old, old_hash)
    controls = {n for n in bundle.SOURCES if p.sha(ROOT/n)!=p.sha(old/'source'/n)}
    p.require(controls=={'tools/serve_gateway/gateway.py'}, 'control source drift')
    native_old = p.read(old/'native-build.json')
    changes = {n for n,h in native_old['source_files'].items() if p.sha(ROOT/n)!=h}
    p.require(changes=={'CMakeLists.txt','src/server/http_request.cpp',
                       'src/service/ingress_main.cpp'}, 'native source drift')
    p.require(not bundle.NATIVE_SOURCES-native_old['source_files'].keys(), 'native inventory drift')
    p.require(p.sha(BUILD/'q4t-ingress')==
        '39757f08e8b3b67f4f1f82f44488d784a7ed1dddc2902db751d280ceda3b0660', 'ingress drift')
    for name,key in [('q4t-guardian','guardian_sha256'),('q4t-cuda-probe','cuda_probe_sha256')]:
        p.require(p.sha(BUILD/name)==native_old[key], 'Guardian/probe changed')
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
        native_changed=sorted(changes),native_added=[],business_state_before=before,
        previous=current,actual_business=accepted,write_business=follow,
        controls_review='Optional nginx Moderations route and validator only; unchanged native recovery/publication/credentials/quota ownership.',
        native_review='Native Moderations route and HTTP business body capacity; actual SDK TLS/auth/CORS/exact8KiB/429 no-retry accepted. Guardian/probe unchanged.',
        rollback_review='Exact accepted61649847 retained from the original build, not an independent rebuild. Supports actual Moderations and Batch. Only runtime difference from candidate is terminal SendSimple success counting; state/quota/model code identical. Old f23 default is archival, not a moderation executor.',
        failure_boundary='Retained fallback can overcount a failed terminal socket write as success; actual protocol/core behavior and resources remain accepted. Candidate fixes this metric.',
        model_starts=0,chats=0,gpu_queries=0,full_performance=False))
    old_engine = p.read(old/'engine/manifest.json')
    old_metadata = p.read(OUT/'previous/q4t.release.json')
    receipts = {}
    for label,build,observation,source_commit in [
        ('accepted-fallback',BASELINE,BUSINESS,old_plan['source_commit']),
        ('candidate',BUILD,WRITE,plan['source_commit'])]:
        directory, engine = OUT/label, OUT/label/'engine'
        engine.mkdir(parents=True,mode=0o700)
        for name in ('q4t','CMakeCache.txt'): copy_file(build/name,engine/name)
        sources = previous_runtime if label=='accepted-fallback' else runtime
        p.save(engine/'runtime-source.json',sources)
        protocol_sources = p.read(build/'generated/q4t/server/chat_protocol_sources.json')
        p.require(all(sources.get(n)==h for n,h in protocol_sources.items()), 'protocol/source mismatch')
        protocol = hashlib.sha256(''.join(n+':'+h+'\n'
            for n,h in sorted(protocol_sources.items())).encode()).hexdigest()
        caps,config = p.read(observation/'capabilities-1.json'),p.read(observation/'config-1.json')
        p.require(caps['protocol_source_sha256']==config['protocol_source_sha256']==protocol,
                  'actual declaration identity')
        decision = dict(status='PASS_TARGETED_MODERATIONS_'+label.upper().replace('-','_'),
            acceptance='ACCEPT_TARGETED_NATIVE_MODERATIONS',binary_sha256=p.sha(engine/'q4t'),
            engine_source_commit=source_commit,package_source_commit=commit,
            protocol_sha256=protocol,actual_business=accepted,
            write_correction_business=follow if label=='candidate' else None,
            source_review_sha256=p.sha(OUT/'review.json'),full_model_quality=False,
            performance_acceptance=False,long_stability_accepted=False,remaining_master_tasks_open=True)
        p.save(directory/'decision.json',decision)
        metadata = copy.deepcopy(old_metadata)
        metadata.update(version='serve-moderations-20261111-'+label,
            source_commit=source_commit,binary_sha256=decision['binary_sha256'],
            acceptance=decision['acceptance'],acceptance_decision=str(directory/'decision.json'),
            acceptance_decision_sha256=p.sha(directory/'decision.json'),
            build_cache=str(engine/'CMakeCache.txt'),build_cache_sha256=p.sha(engine/'CMakeCache.txt'),
            tested_source_manifest=str(engine/'runtime-source.json'),tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),
            observed_configuration=config,previous_binary=str(OUT/'previous/q4t'),
            previous_release=str(OUT/'previous/q4t.release.json'),
            inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),sha256=p.sha(OUT/'previous/q4t.release.json')),
            source_note='Native Moderations actual13-category text/image results, SDK, one-RPM/cumulative TPM, Batch and cancellation accepted. Default keeps text MTP; visual requires explicit existing media profile. Feature-capable accepted616 fallback retained; all remaining master work continues.')
        declarations = engine/'declarations'; declarations.mkdir()
        p.save(declarations/'capabilities.json',caps);p.save(declarations/'configuration.json',config)
        metadata['chat_protocol'].update(caps,protocol_sha256=protocol,
            declaration_evidence='Actual isolated text-MTP Moderations discovery. Existing deployed default output256, object64/256MiB, client policies and text profile preserved. Vision actual business is separately recorded.')
        for name,stem in SCHEMAS.items():
            relative = 'docs/'+stem+('.json' if name=='openapi' else '.schema.json')
            dest = declarations/'schemas'/(name+'.json'); dest.parent.mkdir(exist_ok=True)
            copy_file(ROOT/relative,dest)
            metadata['chat_protocol'][name+'_schema']=str(dest)
            metadata['chat_protocol'][name+'_schema_sha256']=p.sha(dest)
        metadata['release_declarations']=dict(profile=p.APPLICATION_DECLARATION_PROFILE,
            directory=str(declarations),binary_sha256=decision['binary_sha256'],protocol_sha256=protocol,
            files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
        metadata['standalone_moderations_acceptance']=dict(actual_text_array=True,
            actual_text_image_and_image_only=True,actual_model_identity=True,standard_sdk=True,
            native_tls_cpu=True,one_rpm_cumulative_tpm=True,batch_execution=True,
            cancel_during_preparation=True,cancel_gpu_prefill_claim=False,
            terminal_write_failure_count_corrected=label=='candidate',
            compatible_fallback_sha256=identity['binary_sha256'],cloud_calibration=False,
            actual_business=accepted,write_correction=follow if label=='candidate' else None)
        metadata['batch_acceptance']['real_endpoints'].append('/v1/moderations')
        metadata['batch_acceptance'].pop('remaining_five_backend_execution_pending',None)
        metadata['batch_acceptance']['remaining_four_backend_execution_pending']=True
        metadata['batch_acceptance']['moderations_execution']=True
        metadata['batch_acceptance']['moderations_compatible_fallback_sha256']=identity['binary_sha256']
        for file in (ROOT/'third_party/licenses').rglob('*'):
            if file.is_file():copy_file(file,engine/'licenses'/file.relative_to(ROOT/'third_party/licenses'))
        metadata['third_party_notices']['source_directory']=str(engine/'licenses/structured_output')
        metadata['application_notices']['source_directory']=str(engine/'licenses/openai-node')
        system_notices = {}
        for name in ('libidn2-0','libssl3t64','poppler-utils','libarchive13t64','libxml2',
                     'libreoffice-core','libwebpdecoder3','zlib1g'):
            file = Path('/usr/share/doc')/name/'copyright'
            copy_file(file.resolve(strict=True),engine/'licenses/system'/name)
            system_notices[name]=dict(path=str(file),sha256=p.sha(file.resolve(strict=True)),
                artifact=str(engine/'licenses/system'/name),redistributed_binary=False)
        metadata['file_input_system_dependencies']=system_notices
        clients = metadata['client_artifacts']
        for name,digest in clients['files'].items():
            p.require(p.sha(ROOT/name)==digest,'accepted client changed')
            copy_file(ROOT/name,directory/'clients'/name)
        clients['directory']=str(directory/'clients')
        p.save(engine/'q4t.release.json',metadata);p.verify_declarations(metadata,required=True)
        app = {k:v for k,v in old_engine['settings']['application_state'].items() if k!='profile'}
        receipt = bundle.prepare(engine/'q4t',engine/'q4t.release.json',old_engine['model_dir'],
            BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,old/'gateway-policy.json',
            old/'engine/client-policy.json',directory/'bundle',native_receipt=OUT/'native-build.json',
            native_ingress=BUILD/'q4t-ingress',capacity_config=dict(version=1),application_state=app)
        p.save(directory/'receipt.json',receipt);receipts[label]=receipt
        packaged = Path(receipt['package'])
        p.require(p.read(packaged/'engine/manifest.json')['settings']==old_engine['settings'], 'default settings drift')
        for name in ('gateway-policy.json','engine/client-policy.json'):
            p.require(p.sha(packaged/name)==p.sha(old/name),'credential seeds changed')
        service = p.read(OUT/'previous/service.release.json')
        service.update(acceptance=decision['acceptance'],acceptance_record=str(directory/'decision.json'),
            acceptance_sha256=p.sha(directory/'decision.json'),package=str(packaged),
            manifest_sha256=receipt['manifest_sha256'],engine_binary_sha256=receipt['engine_binary_sha256'],
            engine_release_sha256=p.sha(packaged/'engine/q4t.release.json'),engine_metadata_unchanged=False,
            previous_acceptance_record=service['acceptance_record'],control_review=str(OUT/'review.json'),
            client_artifacts=str(directory/'clients'),moderations_targeted_accepted=True,
            rollback_receipt=str(OUT/'rollback.json'),rollback_sha256=None,
            scope=service['scope']+' Standalone Moderations actual text/image/Batch, native SDK/TLS and quotas accepted; feature-capable fallback retained. Four Batch backends and all master tasks continue.')
        p.save(directory/'service.release.json',service)
    rows = []
    def run(label,args):
        start=time.monotonic();argv=[str(x) for x in args]
        value=subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,timeout=45)
        (OUT/(label+'.stdout')).write_bytes(value.stdout);(OUT/(label+'.stderr')).write_bytes(value.stderr)
        row=dict(label=label,argv=argv,returncode=value.returncode,seconds=time.monotonic()-start)
        rows.append(row);p.save(OUT/(label+'.json'),row)
        p.require(value.returncode==0,'failed command: '+label)
        p.require(state_hashes()==before,'business state changed: '+label)
        return json.loads(value.stdout)
    try:
        cli=Path(receipts['candidate']['package'])/'engine/q4t'
        p.require(run('previous-check',[cli,'service','check','--state',STATE,'--package',
            old,'--manifest-sha256',old_hash])['desired']=='STOPPED','service already active')
        for label,receipt in receipts.items():
            package=Path(receipt['package'])
            for suffix,entry in [('native',package/'engine/q4t'),('legacy',package/'bin/q4t')]:
                p.require(run(label+'-'+suffix+'-check',[entry,'service','check'])['status']=='PASS','sealed package check')
        previous=current
        for label,revision in [('accepted-fallback',46),('candidate',48)]:
            receipt=receipts[label];package=Path(receipt['package']);digest=receipt['manifest_sha256']
            request=str(uuid.uuid4())
            p.save(OUT/(label+'-publish-request.json'),dict(request_id=request,
                expected_revision=previous['revision'],expected_manifest_sha256=previous['current']['manifest_sha256'],
                target_manifest_sha256=digest))
            if label=='candidate':
                rollback=dict(reader=previous['current'],revision=previous['revision'],
                    original_accepted_binary=identity,all_moderations_backends_present=True,
                    persistent_and_quota_sources_identical=True,independent_build=False,
                    difference='Only terminal socket-write failure counting; no state/model/protocol result differences.',
                    command='q4t service publish-rollback --state STATE --default-root ROOT --generation-sha256 READER_GENERATION --expected-manifest-sha256 CURRENT_MANIFEST --expected-revision CURRENT_REVISION --request-id NEW_UUID',
                    preservation='Retain original task IDs, deadlines, results and quota; never delete state or reset budgets.')
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
        p.save(OUT/'result.json',dict(status='PASS_MODERATIONS_DELIVERED',current=previous['current'],
            compatible_rollback=p.read(OUT/'rollback.json'),publication_revision=48,commands=rows,
            state='STOPPED',business_state_preserved=state_hashes()==before,model_starts=0,chats=0,
            gpu_queries=0,default_changed=True,main_merged=False,remaining_master_tasks_open=True))
    except BaseException as error:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,category=type(error).__name__,
            detail=str(error),business_state_preserved=state_hashes()==before,
            recovery='Inspect publication and intents; no blind mutation retry or model restart.'))
        raise
    print(json.dumps(dict(status='PASS_MODERATIONS_DELIVERED',revision=48,commands=len(rows))))


if __name__=='__main__':
    os.umask(0o077)
    main()
