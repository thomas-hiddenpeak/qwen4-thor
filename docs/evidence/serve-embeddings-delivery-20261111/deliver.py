"""Publish accepted native embeddings; retain its exact capable recovery package."""
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

BASE = ROOT/'.q4t-work/serve-embeddings-20261111'
OUT = BASE/'release-01'
BUSINESS = BASE/'business-02'
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
    accepted = p.read(BUSINESS/'acceptance-final.json')
    p.require(accepted['status']=='ACCEPT_NATIVE_EMBEDDINGS_INTERFACE_BUSINESS' and
        accepted['all_queries_ok'] and not accepted['env_abort'] and
        all(accepted['postflight'].values()), 'actual business not accepted')
    plan = json.loads((BUSINESS/'plan.json').read_text())
    p.require(all(p.sha(ROOT/n)==h for n,h in plan['sources'].items()), 'source drift')
    p.require(all(p.sha(n)==h for n,h in plan['protected'].items()), 'default drift')
    identity = p.read(BASELINE/'identity.json')
    runtime = source_hashes()
    p.require(runtime==p.read(BASELINE/'runtime-source.json'), 'accepted runtime drift')
    p.require(p.sha(BASELINE/'q4t')==p.sha(BUILD/'q4t')==
        accepted['candidate_sha256']==identity['binary_sha256'], 'binary drift')
    for name in ('guardian-runtime-01','guardian-journal-01'):
        p.require(p.read(BASE/(name+'.json'))['exit_code']==0, 'Guardian CPU not accepted')
    current = publication.status(STATE)
    old = (DEFAULT/'service').resolve(strict=True)
    old_hash = p.sha(old/'manifest.json')
    p.require(current['ready'] and current['revision']==48 and
        old_hash==current['current']['manifest_sha256']==
        '9f3c44f0ca846be9b1542dee290778b5622b3d33d14fe62bba1afe1d77f7f164',
        'predecessor changed')
    bundle.verify(old, old_hash)
    controls = {n for n in bundle.SOURCES if p.sha(ROOT/n)!=p.sha(old/'source'/n)}
    p.require(controls=={'tools/serve_gateway/gateway.py'}, 'control source drift')
    native_old = p.read(old/'native-build.json')
    changes = {n for n,h in native_old['source_files'].items() if p.sha(ROOT/n)!=h}
    p.require(changes=={'CMakeLists.txt','include/q4t/io/json.h','src/io/json.cpp',
        'src/server/http_request.cpp','src/service/ingress_main.cpp'}, 'native source drift')
    p.require(not bundle.NATIVE_SOURCES-native_old['source_files'].keys(), 'native inventory drift')
    p.require(p.sha(BUILD/'q4t-ingress')==
        '6b676fda21b43a90367502efd1a5920f4ad21752585b84ace91a157040b57d51', 'ingress drift')
    p.require(p.sha(BUILD/'q4t-cuda-probe')==native_old['cuda_probe_sha256'], 'probe changed')
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
        native_changed=sorted(changes),business_state_before=before,previous=current,
        actual_business=accepted,controls_review='Optional nginx route and local protocol capacity only.',
        native_review='Original JSON parser semantics covered by9 CPU cases, actual SDK TLS and7 journal/2 Guardian control tests. Guardian rebuilt for changed common JSON; CUDA probe unchanged.',
        recovery_review='First capable embeddings binary retained exactly. No earlier distinct version can execute embeddings; recover by republishing this accepted package without discarding state. Previous ee550 is archival for new embedding tasks, not a compatible execution rollback.',
        retrieval_quality='Actual project retrieval0/2 remains an implementation gap; interface/numerical acceptance does not close A10.',
        model_starts=0,chats=0,gpu_queries=0,full_performance=False))
    old_engine = p.read(old/'engine/manifest.json')
    metadata = copy.deepcopy(p.read(OUT/'previous/q4t.release.json'))
    directory, engine = OUT/'candidate', OUT/'candidate/engine'
    engine.mkdir(parents=True,mode=0o700)
    for name in ('q4t','CMakeCache.txt'): copy_file(BUILD/name,engine/name)
    p.save(engine/'runtime-source.json',runtime)
    protocol_sources = p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    p.require(all(runtime.get(n)==h for n,h in protocol_sources.items()), 'protocol/source mismatch')
    protocol = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(protocol_sources.items())).encode()).hexdigest()
    caps,config = p.read(BUSINESS/'capabilities-1.json'),p.read(BUSINESS/'config-1.json')
    p.require(caps['protocol_source_sha256']==config['protocol_source_sha256']==protocol,
              'actual declaration identity')
    decision = dict(status='PASS_TARGETED_EMBEDDINGS_INTERFACE',
        acceptance='ACCEPT_NATIVE_EMBEDDINGS_INTERFACE_BUSINESS',binary_sha256=p.sha(engine/'q4t'),
        engine_source_commit=plan['source_commit'],package_source_commit=commit,
        protocol_sha256=protocol,actual_business=accepted,source_review_sha256=p.sha(OUT/'review.json'),
        full_model_quality=False,retrieval_quality_accepted=False,performance_acceptance=False,
        long_stability_accepted=False,remaining_master_tasks_open=True)
    p.save(directory/'decision.json',decision)
    metadata.update(version='serve-embeddings-20261111',source_commit=plan['source_commit'],
        binary_sha256=decision['binary_sha256'],acceptance=decision['acceptance'],
        acceptance_decision=str(directory/'decision.json'),acceptance_decision_sha256=p.sha(directory/'decision.json'),
        build_cache=str(engine/'CMakeCache.txt'),build_cache_sha256=p.sha(engine/'CMakeCache.txt'),
        tested_source_manifest=str(engine/'runtime-source.json'),tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),
        observed_configuration=config,previous_binary=str(OUT/'previous/q4t'),
        previous_release=str(OUT/'previous/q4t.release.json'),
        inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),sha256=p.sha(OUT/'previous/q4t.release.json')),
        source_note='Real native LAST vectors, raw token input, FP32/base64, SDK, actual TPM and Batch/cancel/restart accepted. Retrieval0/2 remains open; no retrieval-quality or complete OpenAI claim. Exact first capable package is the recovery anchor; older ee550 cannot execute new embeddings.')
    declarations = engine/'declarations';declarations.mkdir()
    p.save(declarations/'capabilities.json',caps);p.save(declarations/'configuration.json',config)
    metadata['chat_protocol'].update(caps,protocol_sha256=protocol,
        declaration_evidence='Actual isolated text MTP discovery; deployed output256, object64/256MiB and original client policies preserved.')
    for name,stem in SCHEMAS.items():
        relative='docs/'+stem+('.json' if name=='openapi' else '.schema.json')
        dest=declarations/'schemas'/(name+'.json');dest.parent.mkdir(exist_ok=True)
        copy_file(ROOT/relative,dest);metadata['chat_protocol'][name+'_schema']=str(dest)
        metadata['chat_protocol'][name+'_schema_sha256']=p.sha(dest)
    metadata['release_declarations']=dict(profile=p.APPLICATION_DECLARATION_PROFILE,
        directory=str(declarations),binary_sha256=decision['binary_sha256'],protocol_sha256=protocol,
        files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    metadata['embeddings_acceptance']=dict(native_final_mixer_last=True,actual_sdk=True,
        float_base64_fp32=True,one_rpm_actual_input_tpm=True,batch_execution=True,
        actual_prefill_cancellation=True,same_version_restart=True,
        retrieval_quality_accepted=False,retrieval_top1_correct=0,retrieval_total=2,
        retrieval_implementation_task='A10',actual_business=accepted,
        exact_capable_recovery_binary_sha256=identity['binary_sha256'],older_capable_version_available=False)
    metadata['batch_acceptance']['real_endpoints'].append('/v1/embeddings')
    metadata['batch_acceptance'].pop('remaining_four_backend_execution_pending',None)
    metadata['batch_acceptance']['remaining_three_backend_execution_pending']=True
    metadata['batch_acceptance']['embeddings_execution']=True
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
        p.require(p.sha(ROOT/name)==digest,'accepted client changed')
        copy_file(ROOT/name,directory/'clients'/name)
    clients['directory']=str(directory/'clients')
    p.save(engine/'q4t.release.json',metadata);p.verify_declarations(metadata,required=True)
    app={k:v for k,v in old_engine['settings']['application_state'].items() if k!='profile'}
    receipt=bundle.prepare(engine/'q4t',engine/'q4t.release.json',old_engine['model_dir'],
        BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,old/'gateway-policy.json',
        old/'engine/client-policy.json',directory/'bundle',native_receipt=OUT/'native-build.json',
        native_ingress=BUILD/'q4t-ingress',capacity_config=dict(version=1),application_state=app)
    p.save(directory/'receipt.json',receipt)
    packaged=Path(receipt['package'])
    p.require(p.read(packaged/'engine/manifest.json')['settings']==old_engine['settings'],'default settings drift')
    for name in ('gateway-policy.json','engine/client-policy.json'):
        p.require(p.sha(packaged/name)==p.sha(old/name),'credential seeds changed')
    recovery=dict(package=str(packaged),manifest_sha256=receipt['manifest_sha256'],
        original_accepted_binary=identity,kind='same_version_capable_republication_anchor',
        older_capable_version_available=False,previous_default_compatible_with_new_embeddings=False,
        command='q4t service publish --state STATE --default-root ROOT --package ANCHOR --manifest-sha256 ANCHOR_SHA --service-release ANCHOR_SERVICE_RELEASE --expected-manifest-sha256 CURRENT_MANIFEST --expected-revision CURRENT_REVISION --request-id NEW_UUID',
        preservation='Retain original task IDs, deadlines, results and quotas; do not reset state.')
    p.save(OUT/'recovery-anchor.json',recovery)
    service=p.read(OUT/'previous/service.release.json')
    service.update(acceptance=decision['acceptance'],acceptance_record=str(directory/'decision.json'),
        acceptance_sha256=p.sha(directory/'decision.json'),package=str(packaged),
        manifest_sha256=receipt['manifest_sha256'],engine_binary_sha256=receipt['engine_binary_sha256'],
        engine_release_sha256=p.sha(packaged/'engine/q4t.release.json'),engine_metadata_unchanged=False,
        previous_acceptance_record=service['acceptance_record'],control_review=str(OUT/'review.json'),
        client_artifacts=str(directory/'clients'),embeddings_interface_targeted_accepted=True,
        embeddings_retrieval_quality_accepted=False,
        rollback_receipt=str(OUT/'recovery-anchor.json'),rollback_sha256=p.sha(OUT/'recovery-anchor.json'),
        scope=service['scope']+' Native embeddings interface/compute/Batch accepted. Retrieval representation quality remains open; same-version capable recovery anchor retained, no older embeddings executor claimed.')
    p.save(directory/'service.release.json',service)
    rows=[]
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
        cli=packaged/'engine/q4t'
        p.require(run('previous-check',[cli,'service','check','--state',STATE,'--package',
            old,'--manifest-sha256',old_hash])['desired']=='STOPPED','service already active')
        for suffix,entry in [('native',cli),('legacy',packaged/'bin/q4t')]:
            p.require(run('candidate-'+suffix+'-check',[entry,'service','check'])['status']=='PASS','sealed check')
        request=str(uuid.uuid4());digest=receipt['manifest_sha256']
        p.save(OUT/'publish-request.json',dict(request_id=request,expected_revision=48,
            expected_manifest_sha256=old_hash,target_manifest_sha256=digest))
        published=run('candidate-publish',[cli,'service','publish','--state',STATE,
            '--default-root',DEFAULT,'--package',packaged,'--manifest-sha256',digest,
            '--service-release',directory/'service.release.json','--expected-manifest-sha256',
            old_hash,'--expected-revision',48,'--request-id',request])
        p.require(published['durable'] and published['revision']==50,'publication result')
        for action in ('check','status','observe'):
            value=run('candidate-default-'+action,[DEFAULT/'q4t','service',action,'--state',STATE])
            p.require(value['status']=='PASS' if action=='check' else value['state']=='STOPPED','default '+action)
        final=run('candidate-publication',[DEFAULT/'q4t','service','publish-status','--state',STATE])
        p.require(final['ready'] and final['revision']==50 and final['current']['manifest_sha256']==digest,'selected publication')
        p.save(OUT/'result.json',dict(status='PASS_EMBEDDINGS_INTERFACE_DELIVERED',current=final['current'],
            recovery_anchor=recovery,publication_revision=50,commands=rows,state='STOPPED',
            business_state_preserved=state_hashes()==before,model_starts=0,chats=0,gpu_queries=0,
            default_changed=True,main_merged=False,retrieval_quality_open=True,remaining_master_tasks_open=True))
    except BaseException as error:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,category=type(error).__name__,
            detail=str(error),business_state_preserved=state_hashes()==before,
            recovery='Inspect publication/intents; no blind mutation retry or model restart.'))
        raise
    print(json.dumps(dict(status='PASS_EMBEDDINGS_INTERFACE_DELIVERED',revision=50,commands=len(rows))))


if __name__=='__main__':
    os.umask(0o077)
    main()
