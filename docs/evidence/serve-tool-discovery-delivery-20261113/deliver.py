"""Publish exact accepted deferred discovery payloads, preserving the existing state."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle,ledger,profile_manager,profile_migration
from tools.serve_recovery import publication,version_contract
from tools.serve_service import package as p

WORK=Path(__file__).resolve().parent;OUT=WORK/'delivery-01'
DEFAULT=ROOT/'build';STATE=DEFAULT/'service-native-state'
OLD='eeba689ceb1a0d5df0cd1e351525af5ac1806ca421b9d7cb738ed20393b2fea1'
PRIMARY=WORK/'candidate-02/bundle'
FALLBACK=WORK/'retained-02/bundle'
PRIMARY_SHA='bd4b75a783e5e51038bcfdc68a1083ad22ceb0fec7db617f4a69f320220d57c2'
FALLBACK_SHA='11c09eb77d9d7eeb1a595b957ce8b4e8f59dbd9f6839c0a984eb018a3111d1bf'


def preserved_hashes():
    return {str(f.relative_to(STATE)):p.sha(f) for f in STATE.rglob('*') if f.is_file() and
        (f.name=='ledger.json' or f.relative_to(STATE).parts[0].startswith(('client-','outer-')))}


def profiles():return p.read(STATE/'profile-selection/policy.json')['state']['policy']


def prepare():
    p.require(not OUT.exists(),'preserve previous delivery attempt')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
              'commit reviewed sources before delivery')
    current=publication.status(STATE)
    p.require(current['ready'] and current['revision']==110 and
        current['current']['manifest_sha256']==OLD,'default predecessor changed')
    p.require(len(list((DEFAULT/'.q4t-publication/generations').iterdir()))<=30,
              'two active generation slots required')
    original_package=Path(current['current']['package'])
    original_manifest=bundle.verify(original_package,OLD)
    manifests={name:bundle.verify(path,digest) for name,path,digest in
        [('primary',PRIMARY,PRIMARY_SHA),('fallback',FALLBACK,FALLBACK_SHA)]}
    for name,digest in manifests['primary']['files'].items():
        if name.startswith('source/'):
            p.require(p.sha(ROOT/name[7:])==digest,'post-business runtime drift: '+name)
    for name,digest in p.read(PRIMARY/'native-build.json')['source_files'].items():
        p.require(p.sha(ROOT/name)==digest,'post-business native drift: '+name)
    actual={}
    for name,directory,package,digest in [('primary','managed-version-01',PRIMARY,PRIMARY_SHA),
        ('fallback','managed-retained-01',FALLBACK,FALLBACK_SHA)]:
        decision=p.read(WORK/directory/'decision.json');release=p.read(WORK/directory/'service-release.json')
        identity={key:dict(path=str(WORK/directory/file),sha256=p.sha(WORK/directory/file))
            for key,file in [('release','service-release.json'),('decision','decision.json')]}
        selected=version_contract.payload(dict(package=str(package),manifest_sha256=digest))
        version_contract.accepted_fallback(identity,selected)
        p.require(decision['manifest_sha256']==digest and release['manifest_sha256']==digest,
                  'actual package differs')
        actual[name]=dict(decision=decision,identity=identity)
    before=preserved_hashes();old_profiles=profiles()
    old_version=p.read(STATE/'version-selection/policy.json')['state']['policy']
    original_version=p.read(ROOT/'docs/evidence/serve-async-tools-delivery-20261113/delivery-01/result.json')['versions']
    p.require(old_version['revision']==original_version['revision'] and
        old_version['registry']['cycle_id']==original_version['cycle_id'] and
        old_version['registry']['root_manifest_sha256']==OLD and old_version['switch'] is None and
        old_version['terminal_fault'] is None and old_version['selected']=='primary',
        'default version cycle has unexpected consumption')
    authorization=profile_manager.compatible(original_manifest,manifests['fallback'],
        original_package,FALLBACK)
    stamp=old_profiles['clock']
    expected_profiles=profile_migration.candidate(old_profiles,authorization,
        ledger.Stamp(stamp['boot_id'],stamp['monotonic_ns']))
    protected_keys=('history','pending','selected','not_before_ns','light_since_ns','clock','profiles','configuration')
    p.require(all(old_profiles[k]==expected_profiles[k] for k in protected_keys),
              'profile upgrade changes budget/capacity')
    OUT.mkdir(mode=0o700)
    shutil.copytree(STATE,OUT/'previous-state',symlinks=True)
    generation=(DEFAULT/'q4t').resolve().parent
    (OUT/'previous').mkdir(mode=0o700)
    for name in ('q4t','q4t.release.json','service.release.json','service.acceptance.json'):
        shutil.copyfile(generation/name,OUT/'previous'/name)
    root=dict(package=str(PRIMARY),manifest_sha256=PRIMARY_SHA)
    fallback=dict(package=str(FALLBACK),manifest_sha256=FALLBACK_SHA)
    registration=version_contract.prepare(root,root,fallback,str(uuid.uuid4()),
        fallback_acceptance=actual['fallback']['identity'])
    p.save(OUT/'registration.json',registration)
    review=dict(source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        previous=current,previous_version=old_version,preserved_state=before,
        previous_profiles=old_profiles,expected_profiles=expected_profiles,
        profile_upgrade_permit=authorization,actual_business=actual,
        exact_tested_manifests=True,exact_tested_engine_and_native=True,
        post_business_control_delta=[],
        control_patch_review=p.read(WORK/'candidate-02/prepare-review.json'),new_gpu_starts=0)
    p.save(OUT/'review.json',review)
    original=p.read(OUT/'previous/service.release.json')
    for name,package,digest in [('fallback',FALLBACK,FALLBACK_SHA),('primary',PRIMARY,PRIMARY_SHA)]:
        manifest=manifests[name];base=p.read(package/'engine/manifest.json')
        actual_decision=actual[name]['decision'];kind='TOOL_DISCOVERY_'+name.upper()+'_DELIVERY'
        decision=dict(status='PASS_'+kind,acceptance='ACCEPT_'+kind,manifest_sha256=digest,
            engine_binary_sha256=base['files']['q4t'],actual_business=actual_decision['actual_business'],
            original_decision=actual[name]['identity']['decision'],control_review=str(OUT/'review.json'),
            new_gpu_starts=0,remaining_master_tasks_open=True,performance_acceptance=False,
            long_stability_accepted=False)
        p.save(OUT/(name+'-decision.json'),decision)
        engine_metadata=p.read(package/'engine/q4t.release.json')
        release=copy.deepcopy(original)
        release.update(acceptance=decision['acceptance'],acceptance_record=str(OUT/(name+'-decision.json')),
            acceptance_sha256=p.sha(OUT/(name+'-decision.json')),package=str(package),manifest_sha256=digest,
            engine_binary_sha256=base['files']['q4t'],engine_release_sha256=base['files']['q4t.release.json'],
            guardian_sha256=p.sha(package/'bin/q4t-guardian'),
            cuda_probe_sha256=p.sha(package/'bin/q4t-cuda-probe'),ingress_sha256=p.sha(package/'bin/q4t-ingress'),
            recovery_bundle_profile=manifest['profile'],control_review=str(OUT/'review.json'),
            engine_metadata_unchanged=False,engine_qualification_receipt_immutable=True,
            previous_acceptance_record=original['acceptance_record'],
            adaptive_profiles_registry=manifest['recovery_profiles'],
            adaptive_profiles_policy=manifest['recovery_profile_policy'],
            client_artifacts=engine_metadata['client_artifacts']['directory'],
            tool_discovery_targeted_accepted=True,
            tool_discovery_evidence=str(WORK/'managed-version-01/decision.json'),
            qualified_capability_upgrade_accepted=True,
            gpu_starts_for_install=0,engine_default_replaced=True,
            scope='Actual server/client deferred discovery and developer additional_tools, ordered definitions and invocation-time result contracts across native JSON/SSE; qualified primary/light and whole-payload fallback. Native ingress is independent of nginx. Remaining master tasks stay active.')
        if name=='primary':
            release.update(version_recovery_targeted_accepted=True,
                version_recovery_registration=str(OUT/'registration.json'),
                version_recovery_registration_sha256=p.sha(OUT/'registration.json'),
                version_recovery_cycle_id=registration['registry']['cycle_id'])
        else:
            for key in ('version_recovery_registration','version_recovery_registration_sha256',
                        'version_recovery_cycle_id'):
                release.pop(key,None)
            release.update(version_recovery_targeted_accepted=False,
                native_outer_recovery_targeted_accepted=False,native_outer_enabled=False,
                scope='Independently accepted older v12 control payload with the same new deferred discovery engine. '
                    'Automatic recovery retains the modern fixed root. Explicit standalone rollback '
                    'uses this older control entry; it does not claim native outer supervision.')
        p.save(OUT/(name+'-service.release.json'),release)
    anchor=dict(package=str(FALLBACK),manifest_sha256=FALLBACK_SHA,
        service_release=str(OUT/'fallback-service.release.json'),
        service_release_sha256=p.sha(OUT/'fallback-service.release.json'),
        generation_receipt=str(OUT/'retained-anchor-generation.json'),
        manual_root_rollback_requires_stopped=True)
    p.save(OUT/'recovery-anchor.json',anchor)
    release=p.read(OUT/'primary-service.release.json')
    release.update(rollback_receipt=str(OUT/'recovery-anchor.json'),
                   rollback_sha256=p.sha(OUT/'recovery-anchor.json'))
    p.save(OUT/'primary-service.release.json',release)
    p.save(OUT/'requests.json',{k:str(uuid.uuid4()) for k in ('anchor','candidate','rollback','forward')})
    p.save(OUT/'plan.json',dict(commands=17,new_gpu_starts=0,from_revision=110,final_revision=118,
        candidate=PRIMARY_SHA,retained=FALLBACK_SHA,core_client_outer_unchanged=True,
        profile_binding_upgraded_without_budget_reset=True,host_boot_install=False))
    p.require(before==preserved_hashes() and profiles()==old_profiles,'default changed during preparation')
    print(json.dumps(dict(status='PREPARED',commands=17,new_gpu_starts=0)))


def run():
    started=time.monotonic();review=p.read(OUT/'review.json');before=review['preserved_state']
    p.require(before==preserved_hashes() and publication.status(STATE)['revision']==110 and
        profiles()==review['previous_profiles'],'default changed before delivery')
    registration=p.read(OUT/'registration.json');ids=p.read(OUT/'requests.json')
    cli=PRIMARY/'engine/q4t';rows=[];expected=review['previous_profiles'];version_hashes=None
    def call(label,args):
        p.require(len(rows)<17,'delivery command bound');argv=list(map(str,args));began=time.monotonic()
        result=subprocess.run(argv,capture_output=True,stdin=subprocess.DEVNULL,timeout=45)
        (OUT/(label+'.stdout')).write_bytes(result.stdout);(OUT/(label+'.stderr')).write_bytes(result.stderr)
        row=dict(label=label,argv=argv,returncode=result.returncode,seconds=time.monotonic()-began)
        rows.append(row);p.save(OUT/(label+'.json'),row)
        p.require(result.returncode==0,'delivery failed: '+label)
        p.require(before==preserved_hashes(),'core/client/outer state changed')
        p.require(profiles()==expected,'profile identity/budget differs from explicit migration')
        if version_hashes is not None:
            p.require(all(p.sha(STATE/n)==h for n,h in version_hashes.items()),'version history changed')
        return json.loads(result.stdout)
    def publish(label,package,digest,release,old,revision,key):
        return call(label,[cli,'service','publish','--state',STATE,'--default-root',DEFAULT,
            '--package',package,'--manifest-sha256',digest,'--service-release',release,
            '--expected-manifest-sha256',old,'--expected-revision',revision,'--request-id',ids[key]])
    try:
        p.require(call('previous-check',[cli,'service','check','--state',STATE,'--package',
            review['previous']['current']['package'],'--manifest-sha256',OLD])['desired']=='STOPPED','active default')
        for name,package,digest in [('candidate',PRIMARY,PRIMARY_SHA),('fallback',FALLBACK,FALLBACK_SHA)]:
            p.require(call(name+'-check',[cli,'service','check','--package',package,
                '--manifest-sha256',digest])['status']=='PASS','package verification')
        expected=review['expected_profiles']
        value=publish('anchor-publish',FALLBACK,FALLBACK_SHA,OUT/'fallback-service.release.json',OLD,110,'anchor')
        p.require(value['durable'] and value['revision']==112,'anchor publication')
        generation=value['current']['generation_sha256']
        p.save(OUT/'retained-anchor-generation.json',dict(generation_sha256=generation,manifest_sha256=FALLBACK_SHA))
        value=publish('candidate-publish',PRIMARY,PRIMARY_SHA,OUT/'primary-service.release.json',FALLBACK_SHA,112,'candidate')
        p.require(value['durable'] and value['revision']==114,'candidate publication')
        value=call('version-register',[DEFAULT/'q4t','service','version-register','--state',STATE,
            '--registration',OUT/'registration.json','--expected-cycle',review['previous_version']['registry']['cycle_id']])
        p.require(value['registered'] and not value['fallback_consumed'],'explicit version cycle')
        version_hashes={str(f.relative_to(STATE)):p.sha(f) for f in STATE.rglob('*') if f.is_file() and
            f.relative_to(STATE).parts[0].startswith(('version-selection','version-registrations'))}
        call('version-before',[DEFAULT/'q4t','service','version-status','--state',STATE])
        value=call('root-rollback',[cli,'service','publish-rollback','--state',STATE,'--default-root',DEFAULT,
            '--package',PRIMARY,'--manifest-sha256',PRIMARY_SHA,'--generation-sha256',generation,
            '--expected-manifest-sha256',PRIMARY_SHA,'--expected-revision',114,'--request-id',ids['rollback']])
        p.require(value['durable'] and value['revision']==116,'root rollback')
        p.require(call('old-root-check',[DEFAULT/'q4t','service','check','--state',STATE])['desired']=='STOPPED',
                  'retained root check')
        value=publish('root-forward',PRIMARY,PRIMARY_SHA,OUT/'primary-service.release.json',FALLBACK_SHA,116,'forward')
        p.require(value['durable'] and value['revision']==118,'root forward')
        for action in ('check','status','observe'):
            value=call('default-'+action,[DEFAULT/'q4t','service',action,'--state',STATE])
            p.require(value['status']=='PASS' if action=='check' else value['state']=='STOPPED','default health')
        selected_profiles=call('default-profiles',[DEFAULT/'q4t','service','profiles-status','--state',STATE])
        p.require(selected_profiles['selected']=='primary','profile selection changed')
        versions=call('default-version',[DEFAULT/'q4t','service','version-status','--state',STATE])
        p.require(versions['cycle_id']==registration['registry']['cycle_id'] and
            not versions['fallback_consumed'],'version registration changed')
        outer=call('default-outer',[DEFAULT/'q4t','service','outer-status','--state',STATE])
        p.require(outer['enabled'] and outer['charged_total']==0,'outer state changed')
        final=call('publication',[DEFAULT/'q4t','service','publish-status','--state',STATE])
        p.require(final['ready'] and final['revision']==118 and
            final['current']['manifest_sha256']==PRIMARY_SHA,'final publication')
        p.save(OUT/'result.json',dict(status='PASS_TOOL_DISCOVERY_DELIVERED',seconds=time.monotonic()-started,
            commands=rows,publication=final,retained_anchor_generation=generation,profiles=selected_profiles,
            versions=versions,outer=outer,core_client_outer_state_preserved=True,profile_budget_preserved=True,
            state='STOPPED',new_gpu_starts=0,remaining_master_tasks_open=True))
        print(json.dumps(dict(status='PASS_TOOL_DISCOVERY_DELIVERED',revision=118,commands=len(rows))))
    except BaseException as error:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,category=type(error).__name__,
            detail=str(error),seconds=time.monotonic()-started));raise


if __name__=='__main__':
    os.umask(0o077)
    {'prepare':prepare,'run':run}[sys.argv[1]]()
