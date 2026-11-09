"""Seal N04 accepted runtime/declarations and a compatible retained v8 copy."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path.cwd().resolve();sys.path.insert(0,str(ROOT))
from tools.serve_service import package as p
from tools.serve_recovery import bundle

BASE = ROOT/'.q4t-work/serve-access-20261109'
OUT = BASE/'release-01'
BUILD = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
EVIDENCE = ROOT/'docs/evidence'


def copy_file(source, target):
    source,target=Path(source),Path(target)
    p.require(not target.exists(),'destination exists')
    target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    target.chmod(0o500 if os.access(source,os.X_OK) else 0o400)
    p.require(p.sha(source)==p.sha(target),'copy changed')


def main():
    p.require(not OUT.exists(),'release exists')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit sources before release')
    plan=p.read(BASE/'business-04/plan.json')
    accepted=p.read(EVIDENCE/'serve-access-business-20261109/decision.json')
    p.require(accepted['status']=='PASS_TARGETED_NATIVE_ACCESS_QUOTA_CONTINUITY' and
              accepted['lease_status']=='COMPLETED' and accepted['all_nvml_ok'],'business acceptance')
    p.require(all(p.sha(ROOT/n)==h for n,h in plan['sources'].items()),'tested sources changed')
    p.require(all(p.sha(r['path'])==r['sha256'] for r in plan['protected']),'defaults changed')
    p.require(p.sha(BUILD/'q4t')==accepted['binary_sha256'],'accepted engine changed')
    native=p.read(BASE/'native-build-02/native-build.json')
    p.require(all(p.sha(ROOT/n)==h for n,h in native['source_files'].items()),'native sources changed')
    for name,field in [('q4t-ingress','ingress_sha256'),('q4t-guardian','guardian_sha256'),
                       ('q4t-cuda-probe','cuda_probe_sha256')]:
        p.require(p.sha(BUILD/name)==native[field],'native binary changed')
    old=(ROOT/'build/service').resolve(strict=True)
    old_manifest=bundle.verify(old,p.sha(old/'manifest.json'))
    p.require(old_manifest['version']==6,'unexpected default')
    candidate=Path(plan['primary_package']['package'])
    p.require(all(p.sha(ROOT/n)==p.sha(candidate/'source'/n) for n in bundle.SOURCES),
              'production controller sources changed after business')
    groups=[
        'serve-access-primitive-20261109','serve-access-store-20261109',
        'serve-access-native-20261109','serve-access-io-20261109','serve-access-managed-20261109',
        'serve-client-policy-20261109','serve-limits-store-20261109','serve-policy-admin-20261109',
        'serve-policy-runtime-20261109','serve-policy-coordination-20261109',
        'serve-usage-primitive-20261109','serve-usage-store-20261109','serve-usage-runtime-20261109',
        'serve-usage-management-20261109','serve-deployment-20261109','serve-initialization-20261109',
        'serve-access-driver-20261109','serve-access-driver-fix-20261109',
        'serve-access-config-fix-20261109','serve-access-checkpoint-20261109',
        'serve-access-business-20261109','serve-native-observation-delivery-20261109',
        'serve-cache-progress-delivery-20261109']
    indices={}
    for group in groups:
        directory=EVIDENCE/group;index=p.read(directory/'index.json');rows=index.get('files',index)
        rows=({r['path']:r['sha256'] for r in rows} if isinstance(rows,list) else rows)
        p.require(all(p.sha(directory/n)==(h['sha256'] if isinstance(h,dict) else h)
                      for n,h in rows.items()),'evidence changed '+group)
        indices[group]=dict(index_sha256=p.sha(directory/'index.json'),files=len(rows))
    caps=p.read(EVIDENCE/'serve-access-business-20261109/capabilities.json')
    config=p.read(EVIDENCE/'serve-access-business-20261109/configuration.json')
    protocol_sources=p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    protocol=hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(protocol_sources.items())).encode()).hexdigest()
    p.require(protocol==caps['protocol_source_sha256']==config['protocol_source_sha256'] and
        all(p.sha(ROOT/n)==h for n,h in protocol_sources.items()),'compiled protocol source mismatch')
    OUT.mkdir(mode=0o700);engine=OUT/'engine';engine.mkdir()
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    for name in ('q4t','CMakeCache.txt'):copy_file(BUILD/name,engine/name)
    for name in ('q4t','q4t.release.json','service.release.json'):
        copy_file(ROOT/'build'/name,OUT/'previous'/name)
    runtime={n:p.sha(ROOT/n) for n in subprocess.check_output(
        ['git','ls-files','src','include','cmake','CMakeLists.txt'],cwd=ROOT,text=True).splitlines()}
    p.save(engine/'runtime-source.json',runtime)
    p.save(OUT/'source-snapshot.json',dict(source_commit=commit,original_test_commit=plan['source_commit'],
        files=plan['sources'],production_runtime_unchanged_after_business=True))
    control={n:p.sha(ROOT/n) for n in bundle.SOURCES}
    changed={n:dict(before=p.sha(old/'source'/n) if (old/'source'/n).exists() else None,after=h)
        for n,h in control.items() if not (old/'source'/n).exists() or p.sha(old/'source'/n)!=h}
    p.save(OUT/'control-review.json',dict(source_commit=commit,previous_package=str(old),files=control,
        changed=changed,native=native,evidence=indices,defaults=plan['protected'],
        accepted_candidate=str(candidate),runtime_sources_identical_to_business=True,
        actual_configuration_provenance='Unmodified actual native TLS N04 observation; initial alpha then appended beta. '
            'Default preserves prior two-client policy and plain loopback listener; corresponding CPU/previous '
            'listener evidence is reused. Neither profile is relabeled as the other.',
        rollback_scope='Compatible retained v8 package with identical accepted code, not downgrade to legacy v6; '
            'R06 automatic older-version rollback remains a separate required task.'))
    decision=dict(status='PASS_TARGETED_NATIVE_ACCESS_QUOTA_DELIVERY',
        acceptance=accepted['acceptance'],binary_sha256=accepted['binary_sha256'],protocol_sha256=protocol,
        source_commit=commit,evidence=indices,actual_business=accepted,additional_gpu_tests=0,
        previous_application_and_cache_evidence_reused=True,performance_acceptance=False,
        full_model_quality_run=False,long_stability_accepted=False,remaining_master_tasks_open=True)
    p.save(OUT/'decision.json',decision)
    declarations=engine/'declarations';declarations.mkdir()
    p.save(declarations/'capabilities.json',caps);p.save(declarations/'configuration.json',config)
    schemas=dict(config='serve-config-v1.schema.json',message='serve-message-profile-v3.schema.json',
        preflight='serve-preflight-v1.schema.json',reasoning='serve-reasoning-v1.schema.json',
        response='serve-transport-v1.schema.json',stop='serve-stop-v1.schema.json',
        thinking='serve-thinking-v1.schema.json',completion_request='serve-completion-request-v1.schema.json',
        completion_response='serve-completion-transport-v1.schema.json',
        application='serve-application-v1.schema.json',openapi='serve-openapi-bundle-v1.json')
    for name,source in schemas.items():copy_file(ROOT/'docs'/source,declarations/'schemas'/(name+'.json'))
    metadata=p.read(ROOT/'build/q4t.release.json')
    metadata.update(version='serve-native-client-policy-20261109',source_commit=commit,
        binary_sha256=accepted['binary_sha256'],acceptance=decision['acceptance'],
        acceptance_decision=str(OUT/'decision.json'),acceptance_decision_sha256=p.sha(OUT/'decision.json'),
        build_cache=str(engine/'CMakeCache.txt'),build_cache_sha256=p.sha(engine/'CMakeCache.txt'),
        tested_source_manifest=str(engine/'runtime-source.json'),
        tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),observed_configuration=config,
        inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),sha256=p.sha(OUT/'previous/q4t.release.json')),
        previous_binary=str(OUT/'previous/q4t'),previous_release=str(OUT/'previous/q4t.release.json'),
        source_note='Native client key/policy publication and bounded quota continuity; actual TLS SDK/tools, '
            'auxiliary-loss isolation, exact and unknown handoff, compatible package switching. '
            'Public config schema corrected from actual response; all remaining master tasks continue.')
    metadata['chat_protocol'].update(caps,protocol_sha256=protocol,
        declaration_evidence='Exact N04 runtime discovery; corrected public config/schema; previous '
            'application/Responses/cache evidence reused for unchanged implementations.')
    for name in schemas:
        path=declarations/'schemas'/(name+'.json')
        metadata['chat_protocol'][name+'_schema']=str(path)
        metadata['chat_protocol'][name+'_schema_sha256']=p.sha(path)
    metadata['release_declarations']=dict(profile=p.APPLICATION_DECLARATION_PROFILE,
        directory=str(declarations),binary_sha256=accepted['binary_sha256'],protocol_sha256=protocol,
        files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    metadata['client_policy_acceptance']=dict(rotation=True,revocation=True,dynamic_limits=True,
        stable_client_identity=True,auxiliary_loss_same_model=True,managed_exact_usage_restore=True,
        unknown_restore_full_window=True,compatible_stopped_switch=True,
        live_same_minute_gpu_branch_observed=False,live_same_minute_cpu_contract_reused=True,
        default_profile_is_observed_profile=False)
    metadata['scope']['production']+='; native managed credential/policy update and quota handoff.'
    notices=metadata['third_party_notices'];notices['source_directory']=str(engine/'licenses/structured_output');notices['files']={}
    for file in sorted((ROOT/'third_party/licenses').rglob('*')):
        if file.is_file():
            name=str(file.relative_to(ROOT/'third_party/licenses'));copy_file(file,engine/'licenses'/name)
            if name.startswith('structured_output/'):
                notices['files'][name[len('structured_output/'):]]=dict(sha256=p.sha(file),text=file.read_text())
    metadata['application_notices']=dict(source_directory=str(engine/'licenses/openai-node'),
        files={str(f.relative_to(engine/'licenses/openai-node')):p.sha(f)
               for f in (engine/'licenses/openai-node').rglob('*') if f.is_file()})
    copy_file('/usr/share/doc/libidn2-0/copyright',engine/'licenses/system-libidn2-copyright')
    p.save(engine/'q4t.release.json',metadata);p.verify_declarations(metadata,required=True)
    native['business_evidence']=dict(path=str(EVIDENCE/'serve-access-business-20261109/decision.json'),
        sha256=p.sha(EVIDENCE/'serve-access-business-20261109/decision.json'))
    p.save(OUT/'native-build.json',native)
    receipt=bundle.prepare(engine/'q4t',engine/'q4t.release.json',p.read(old/'engine/manifest.json')['model_dir'],
        BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,old/'gateway-policy.json',old/'engine/client-policy.json',
        OUT/'bundle',native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress')
    p.save(OUT/'receipt.json',receipt)
    shutil.copytree(OUT/'bundle',OUT/'rollback-bundle')
    rollback=dict(receipt,package=str(OUT/'rollback-bundle'));p.save(OUT/'rollback-receipt.json',rollback)
    for r in (receipt,rollback):
        manifest=bundle.verify(r['package'],r['manifest_sha256']);p.require(manifest['version']==8,'not managed v8')
    for name in ('gateway-policy.json','engine/client-policy.json'):
        p.require(p.sha(OUT/'bundle'/name)==p.sha(old/name),'default key/policy changed')
    p.require(p.read(OUT/'bundle/engine/manifest.json')['settings']==p.read(old/'engine/manifest.json')['settings'],
              'default managed settings changed')
    p.require(all(p.sha(r['path'])==r['sha256'] for r in plan['protected']),'defaults changed during prepare')
    result=dict(status='PASS_ACCEPTED_N04_CANDIDATES_SEALED',package=receipt,rollback=rollback,
        engine_release_sha256=p.sha(engine/'q4t.release.json'),source_commit=commit,
        evidence_groups=len(indices),runtime_sources=len(runtime),protocol_sources=len(protocol_sources),
        default_changed=False,model_starts=0,chat_requests=0,standalone_cli_pending=True)
    p.save(OUT/'result.json',result);print(json.dumps(result))


if __name__=='__main__':
    os.umask(0o077);main()
