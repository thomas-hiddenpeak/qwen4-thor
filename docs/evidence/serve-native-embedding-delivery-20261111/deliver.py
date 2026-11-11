"""Publish accepted native retrieval backend; preserve capability and user state."""
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path.cwd(); sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, publication
from tools.serve_service import package as p
from tools.serve_progress.prepare import copy_file
BASE = ROOT/'.q4t-work/native-embedding-20261111'
OUT = BASE/'release-01'; BUSINESS = BASE/'business-01'
BASELINE = BASE/'accepted-baseline'; ASSETS = BASE/'assets'
BUILD = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
DEFAULT = ROOT/'build'; STATE = DEFAULT/'service-native-state'
PRIOR_DRIVER = ROOT/'.q4t-work/serve-embeddings-20261111/deliver.py'
prior = runpy.run_path(str(PRIOR_DRIVER))
SCHEMAS = prior['SCHEMAS']; source_hashes = prior['source_hashes']
state_hashes = prior['state_hashes']
OLD_HASH = '441be95849e6113a03330e68411026d6e6d44dbf49738a395cb3742b28bfe1c3'


def main():
    p.require(not OUT.exists(), 'inspect any prior attempt before resuming')
    p.require(not subprocess.check_output(['git','status','--porcelain']), 'commit first')
    accepted = p.read(BUSINESS/'acceptance-final.json')
    p.require(accepted['status']=='ACCEPT_NATIVE_RETRIEVAL_BUSINESS' and
        accepted['all_queries_ok'] and not accepted['env_abort'] and
        all(accepted['postflight'].values()), 'business not accepted')
    plan = p.read(BUSINESS/'plan.json')
    p.require(all(p.sha(ROOT/n)==h for n,h in plan['sources'].items()), 'source drift')
    p.require(all(p.sha(n)==h for n,h in plan['protected'].items()), 'default drift')
    identity = p.read(BASELINE/'identity.json'); runtime = source_hashes()
    p.require(runtime==p.read(BASELINE/'runtime-source.json'), 'accepted runtime drift')
    p.require(p.sha(BASELINE/'q4t')==p.sha(BUILD/'q4t')==
        accepted['candidate_sha256']==identity['binary_sha256'], 'binary drift')
    current = publication.status(STATE); old = (DEFAULT/'service').resolve(strict=True)
    p.require(current['ready'] and current['revision']==50 and
        p.sha(old/'manifest.json')==current['current']['manifest_sha256']==OLD_HASH,
        'predecessor changed')
    bundle.verify(old, OLD_HASH)
    controls = {n for n in bundle.SOURCES if p.sha(ROOT/n)!=p.sha(old/'source'/n)}
    expected_controls = {'tools/serve_service/package.py','tools/serve_service/service.py',
        'tools/serve_recovery/bundle.py','tools/serve_recovery/service.py',
        'tools/serve_recovery/deployment.py'}
    p.require(controls==expected_controls, 'unreviewed control change')
    native_old = p.read(old/'native-build.json')
    changed = {n for n,h in native_old['source_files'].items() if p.sha(ROOT/n)!=h}
    p.require(changed=={'CMakeLists.txt'} and
        not bundle.NATIVE_SOURCES-native_old['source_files'].keys(), 'native source drift')
    for name,key in [('q4t-guardian','guardian_sha256'),('q4t-ingress','ingress_sha256'),
                     ('q4t-cuda-probe','cuda_probe_sha256')]:
        p.require(p.sha(BUILD/name)==native_old[key], 'unchanged native binary differs')
    for name,h in plan['asset_hashes'].items():p.require(p.sha(ASSETS/name)==h,'optional asset drift')
    before = state_hashes(); OUT.mkdir(mode=0o700)
    commit = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    native = dict(version=1,source_files={n:p.sha(ROOT/n) for n in sorted(bundle.NATIVE_SOURCES)},
        guardian_sha256=p.sha(BUILD/'q4t-guardian'),ingress_sha256=p.sha(BUILD/'q4t-ingress'),
        cuda_probe_sha256=p.sha(BUILD/'q4t-cuda-probe'))
    p.save(OUT/'native-build.json',native)
    for name in ['q4t','q4t.release.json','service.release.json']:
        copy_file((DEFAULT/name).resolve(strict=True),OUT/'previous'/name)
    review = dict(source_commit=commit,controls=sorted(controls),native_changed=sorted(changed),
        business_state_before=before,previous=current,actual_business=accepted,
        helper_source=str(PRIOR_DRIVER),helper_sha256=p.sha(PRIOR_DRIVER),
        control_review='Only explicit optional asset settings/CLI forwarding and capability-preserving deployment;5 targeted package/deployment tests passed.',
        native_review='CMake adds only independent diagnostic target/source. Necessary build produced exactly unchanged guardian/ingress/probe; previous targeted native evidence reused.',
        numerical_review='Actual operand review explains8 intermediate differences. Original FAILED retained.14 final vectors meet unchanged thresholds;5 actual retrieval queries pass.',
        recovery_review='Retain exact first accepted dedicated model package; old generic embedding package cannot execute the newly configured model. Same-version capable republication anchor, no invented older implementation.',
        model_starts=0,chats=0,gpu_queries=0,full_performance=False)
    p.save(OUT/'review.json',review)
    old_engine = p.read(old/'engine/manifest.json')
    metadata = copy.deepcopy(p.read(OUT/'previous/q4t.release.json'))
    directory = OUT/'candidate'; engine = directory/'engine';engine.mkdir(parents=True,mode=0o700)
    for name in ['q4t','CMakeCache.txt']:copy_file(BUILD/name,engine/name)
    p.save(engine/'runtime-source.json',runtime)
    sources = p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    p.require(all(runtime.get(n)==h for n,h in sources.items()), 'compiled protocol drift')
    protocol = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(sources.items())).encode()).hexdigest()
    caps,config = p.read(BUSINESS/'capabilities-1.json'),p.read(BUSINESS/'config-1.json')
    p.require(caps['protocol_source_sha256']==config['protocol_source_sha256']==protocol,'observed protocol')
    decision = dict(status='PASS_TARGETED_NATIVE_RETRIEVAL',acceptance=accepted['status'],
        binary_sha256=p.sha(engine/'q4t'),engine_source_commit=plan['source_commit'],package_source_commit=commit,
        protocol_sha256=protocol,actual_business=accepted,source_review_sha256=p.sha(OUT/'review.json'),
        full_model_quality=False,retrieval_quality_accepted=True,performance_acceptance=False,
        long_stability_accepted=False,remaining_master_tasks_open=True)
    p.save(directory/'decision.json',decision)
    metadata.update(version='serve-native-embedding-20261111',source_commit=plan['source_commit'],
        binary_sha256=decision['binary_sha256'],acceptance=decision['acceptance'],
        acceptance_decision=str(directory/'decision.json'),acceptance_decision_sha256=p.sha(directory/'decision.json'),
        build_cache=str(engine/'CMakeCache.txt'),build_cache_sha256=p.sha(engine/'CMakeCache.txt'),
        tested_source_manifest=str(engine/'runtime-source.json'),tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),
        observed_configuration=config,previous_binary=str(OUT/'previous/q4t'),previous_release=str(OUT/'previous/q4t.release.json'),
        inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),sha256=p.sha(OUT/'previous/q4t.release.json')),
        source_note='Native fixed Qwen3-Embedding0.6B, trained tokenizer/EOS, actual project retrieval5/5, same-PID missing-weight recovery, Batch/cancel/restart and healthy Chat accepted. Original intermediate comparison FAILED retained and explained by actual operand arithmetic review; final thresholds unchanged. No full OpenAI compatibility or performance claim.')
    declaration = engine/'declarations';declaration.mkdir()
    p.save(declaration/'capabilities.json',caps);p.save(declaration/'configuration.json',config)
    metadata['chat_protocol'].update(caps,protocol_sha256=protocol,
        declaration_evidence='Actual isolated native embedding+text MTP configuration; deployed text/output256/object64/256MiB and original client policies preserved; native asset selection added explicitly.')
    for name,stem in SCHEMAS.items():
        src=ROOT/'docs'/(stem+('.json' if name=='openapi' else '.schema.json'))
        dst=declaration/'schemas'/(name+'.json');dst.parent.mkdir(exist_ok=True);copy_file(src,dst)
        metadata['chat_protocol'][name+'_schema']=str(dst)
        metadata['chat_protocol'][name+'_schema_sha256']=p.sha(dst)
    metadata['release_declarations']=dict(profile=p.APPLICATION_DECLARATION_PROFILE,directory=str(declaration),
        binary_sha256=decision['binary_sha256'],protocol_sha256=protocol,
        files={n:p.sha(declaration/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    metadata['native_embedding_acceptance']=dict(model=p.NATIVE_EMBEDDING,assets=str(ASSETS),asset_hashes=plan['asset_hashes'],
        upstream='https://huggingface.co/Qwen/Qwen3-Embedding-0.6B',license='Apache-2.0',
        weights_redistributed=False,external_inference_runtime_required=False,
        tokenizer_profile='qwen3_embedding',final_vectors_accepted=14,retrieval_top1_correct=5,retrieval_total=5,
        intermediate_comparison_original_failed=True,arithmetic_review='docs/evidence/serve-native-embedding-rounding-review-20261111/decision.json',
        actual_business=accepted,exact_capable_recovery_binary_sha256=identity['binary_sha256'],
        older_capable_version_available=False)
    for file in (ROOT/'third_party/licenses').rglob('*'):
        if file.is_file():copy_file(file,engine/'licenses'/file.relative_to(ROOT/'third_party/licenses'))
    metadata['third_party_notices']['source_directory']=str(engine/'licenses/structured_output')
    metadata['application_notices']['source_directory']=str(engine/'licenses/openai-node')
    notices={}
    for name in ['libidn2-0','libssl3t64','poppler-utils','libarchive13t64','libxml2','libreoffice-core','libwebpdecoder3','zlib1g']:
        file=Path('/usr/share/doc')/name/'copyright';copy_file(file.resolve(strict=True),engine/'licenses/system'/name)
        notices[name]=dict(path=str(file),sha256=p.sha(file.resolve(strict=True)),artifact=str(engine/'licenses/system'/name),redistributed_binary=False)
    metadata['file_input_system_dependencies']=notices
    clients=metadata['client_artifacts']
    for name,digest in clients['files'].items():
        p.require(p.sha(ROOT/name)==digest,'accepted client changed');copy_file(ROOT/name,directory/'clients'/name)
    clients['directory']=str(directory/'clients')
    p.save(engine/'q4t.release.json',metadata);p.verify_declarations(metadata,required=True)
    app={k:v for k,v in old_engine['settings']['application_state'].items() if k!='profile'}
    receipt=bundle.prepare(engine/'q4t',engine/'q4t.release.json',old_engine['model_dir'],
        BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,old/'gateway-policy.json',old/'engine/client-policy.json',
        directory/'bundle',native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress',
        capacity_config=dict(version=1),application_state=app,embedding_model_dir=ASSETS)
    p.save(directory/'receipt.json',receipt);packaged=Path(receipt['package'])
    expected=dict(old_engine['settings'],native_embedding=dict(p.NATIVE_EMBEDDING,directory=str(ASSETS)))
    p.require(p.read(packaged/'engine/manifest.json')['settings']==expected,'unexpected deployed settings')
    for name in ['gateway-policy.json','engine/client-policy.json']:
        p.require(p.sha(packaged/name)==p.sha(old/name),'credential seeds changed')
    recovery=dict(package=str(packaged),manifest_sha256=receipt['manifest_sha256'],original_accepted_binary=identity,
        kind='same_version_capable_republication_anchor',older_capable_version_available=False,
        previous_default_compatible_with_dedicated_model=False,
        command='q4t service publish --state STATE --default-root ROOT --package ANCHOR --manifest-sha256 ANCHOR_SHA --service-release ANCHOR_SERVICE_RELEASE --expected-manifest-sha256 CURRENT_MANIFEST --expected-revision CURRENT_REVISION --request-id NEW_UUID',
        preservation='Retain original tasks/results/deadlines/quotas and fixed optional model identity. Assets may relocate; never silently drop model.')
    p.save(OUT/'recovery-anchor.json',recovery)
    service=p.read(OUT/'previous/service.release.json')
    service.update(acceptance=decision['acceptance'],acceptance_record=str(directory/'decision.json'),
        acceptance_sha256=p.sha(directory/'decision.json'),package=str(packaged),manifest_sha256=receipt['manifest_sha256'],
        engine_binary_sha256=receipt['engine_binary_sha256'],engine_release_sha256=p.sha(packaged/'engine/q4t.release.json'),
        engine_metadata_unchanged=False,previous_acceptance_record=service['acceptance_record'],control_review=str(OUT/'review.json'),
        client_artifacts=str(directory/'clients'),native_retrieval_targeted_accepted=True,
        embeddings_retrieval_quality_accepted=True,rollback_receipt=str(OUT/'recovery-anchor.json'),
        rollback_sha256=p.sha(OUT/'recovery-anchor.json'),scope=service['scope']+' Dedicated native retrieval now accepted5/5; same-PID asset recovery and actual Batch usage/restart accepted. Remaining Serve master tasks continue.')
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
        p.require(run('previous-check',[cli,'service','check','--state',STATE,'--package',old,
            '--manifest-sha256',OLD_HASH])['desired']=='STOPPED','service active')
        for suffix,entry in [('native',cli),('legacy',packaged/'bin/q4t')]:
            p.require(run('candidate-'+suffix+'-check',[entry,'service','check'])['status']=='PASS','sealed check')
        request=str(uuid.uuid4());digest=receipt['manifest_sha256']
        p.save(OUT/'publish-request.json',dict(request_id=request,expected_revision=50,expected_manifest_sha256=OLD_HASH,target_manifest_sha256=digest))
        value=run('candidate-publish',[cli,'service','publish','--state',STATE,'--default-root',DEFAULT,
            '--package',packaged,'--manifest-sha256',digest,'--service-release',directory/'service.release.json',
            '--expected-manifest-sha256',OLD_HASH,'--expected-revision',50,'--request-id',request])
        p.require(value['durable'] and value['revision']==52,'publication result')
        for action in ['check','status','observe']:
            value=run('candidate-default-'+action,[DEFAULT/'q4t','service',action,'--state',STATE])
            p.require(value['status']=='PASS' if action=='check' else value['state']=='STOPPED','default '+action)
        final=run('candidate-publication',[DEFAULT/'q4t','service','publish-status','--state',STATE])
        p.require(final['ready'] and final['revision']==52 and final['current']['manifest_sha256']==digest,'selected publication')
        p.save(OUT/'result.json',dict(status='PASS_NATIVE_RETRIEVAL_DELIVERED',current=final['current'],
            recovery_anchor=recovery,publication_revision=52,commands=rows,state='STOPPED',
            business_state_preserved=state_hashes()==before,model_starts=0,chats=0,gpu_queries=0,
            default_changed=True,main_merged=False,remaining_master_tasks_open=True))
    except BaseException as error:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,category=type(error).__name__,detail=str(error),
            business_state_preserved=state_hashes()==before,recovery='Inspect durable publication; no blind retry or automatic model restart.'))
        raise
    print(json.dumps(dict(status='PASS_NATIVE_RETRIEVAL_DELIVERED',revision=52,commands=len(rows))))


if __name__=='__main__':
    os.umask(0o077);main()
