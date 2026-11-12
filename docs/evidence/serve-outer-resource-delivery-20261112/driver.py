"""Exact accepted package publication; STOPPED only, no GPU work."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle,publication,version_contract
from tools.serve_service import package as p
PRIMARY=Path('/home/rm01/models/dev/qwen4-thor')
DEFAULT=PRIMARY/'build';STATE=DEFAULT/'service-native-state'
BASE=ROOT/'.q4t-work/serve-outer-delivery-20261112';OUT=BASE/'release-01'
TEST=ROOT/'.q4t-work/serve-outer-business-20261112/business-04'
OLD='a3bb4ec872a784d2af3ecf46496b16851b57f5f1f8b42f9e3f48fdd4485bb5ac'

def state_hashes():
    return {str(f.relative_to(STATE)):p.sha(f) for f in STATE.rglob('*') if f.is_file() and
        (f.name=='ledger.json' or f.relative_to(STATE).parts[0].startswith(('client-','profile-selection')))}

def prepare():
    p.require(not OUT.exists(),'retain previous delivery attempt')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit reviewed sources first')
    accepted=p.read(TEST/'acceptance-final.json');plan=p.read(TEST/'plan.json')
    p.require(accepted['status']=='ACCEPT_BOUNDED_OUTER_RESOURCE_VERSION_BUSINESS' and
        all(accepted['postflight'].values()) and accepted['cumulative_counts']['starts']==6 and
        accepted['cumulative_counts']['chats']==8,'actual business not accepted')
    for row in plan['protected']+plan['prior_evidence']:
        p.require(p.sha(row['path'])==row['sha256'],'frozen prior/default evidence changed')
    tested=Path(plan['package']['package']);manifest=bundle.verify(tested,plan['package']['manifest_sha256'])
    for name,digest in manifest['files'].items():
        if name.startswith('source/'):
            p.require(p.sha(ROOT/name[7:])==digest,'post-business runtime source changed: '+name)
    for name,digest in p.read(tested/'native-build.json')['source_files'].items():
        p.require(p.sha(ROOT/name)==digest,'post-business native source changed: '+name)
    current=publication.status(STATE)
    p.require(current['ready'] and current['revision']==64 and
        current['current']['manifest_sha256']==OLD,'default predecessor changed')
    previous_version=p.read(STATE/'version-selection/policy.json')['state']['policy']
    p.require(previous_version['revision']==0 and previous_version['switch'] is None and
        previous_version['terminal_fault'] is None and previous_version['selected']=='primary',
        'default version cycle is not the untouched accepted predecessor')
    p.require(not (STATE/'outer-initialization.json').exists(),'outer already configured elsewhere')
    before=state_hashes();OUT.mkdir(mode=0o700)
    generation=(DEFAULT/'q4t').resolve().parent
    for name in ('q4t','q4t.release.json','service.release.json','service.acceptance.json'):
        dest=OUT/'previous'/name;dest.parent.mkdir(exist_ok=True);shutil.copyfile(generation/name,dest)
    shutil.copytree(tested,OUT/'bundle')
    target=OUT/'bundle';digest=p.sha(target/'manifest.json')
    p.require(digest==plan['package']['manifest_sha256'] and bundle.verify(target,digest)==manifest,
        'relocated package differs from actual business')
    fallback=plan['registration']['payloads']['fallback']['descriptor']
    fallback_manifest=bundle.verify(fallback['package'],fallback['manifest_sha256'])
    root=dict(package=str(target),manifest_sha256=digest)
    registration=version_contract.prepare(root,root,fallback,str(uuid.uuid4()),
        fallback_acceptance=plan['registration']['fallback_acceptance'])
    p.save(OUT/'registration.json',registration)
    original=p.read(OUT/'previous/service.release.json')
    review=dict(source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        previous=current,previous_version=previous_version,original_state=before,
        actual_business=accepted,exact_tested_manifest=True,post_business_runtime_delta=[],new_gpu_starts=0)
    p.save(OUT/'review.json',review)
    def metadata(package,sha,manifest,kind,stem):
        decision=dict(status='PASS_'+kind,acceptance='ACCEPT_'+kind,manifest_sha256=sha,
            engine_binary_sha256=accepted['package']['engine_binary_sha256'],actual_business=accepted,
            control_review=str(OUT/'review.json'),new_gpu_starts=0,remaining_master_tasks_open=True,
            full_performance=False,long_stability_accepted=False)
        p.save(OUT/(stem+'decision.json'),decision)
        release=copy.deepcopy(original)
        release.update(acceptance=decision['acceptance'],acceptance_record=str(OUT/(stem+'decision.json')),
            acceptance_sha256=p.sha(OUT/(stem+'decision.json')),previous_acceptance_record=original['acceptance_record'],
            package=str(package),manifest_sha256=sha,guardian_sha256=p.sha(Path(package)/'bin/q4t-guardian'),
            cuda_probe_sha256=p.sha(Path(package)/'bin/q4t-cuda-probe'),
            ingress_sha256=p.sha(Path(package)/'bin/q4t-ingress'),recovery_bundle_profile=manifest['profile'],
            control_review=str(OUT/'review.json'),gpu_starts_for_install=0)
        return release
    old=metadata(fallback['package'],fallback['manifest_sha256'],fallback_manifest,
        'RETAINED_RESOURCE_ADAPTER','fallback-')
    old.update(version_recovery_targeted_accepted=False,
        scope='Retained independently accepted v12 root with exactly verified resource/profile adapters; real fallback tool/SSE business accepted. Manual stopped rollback anchor.')
    p.save(OUT/'fallback-service.release.json',old)
    anchor=dict(package=fallback['package'],manifest_sha256=fallback['manifest_sha256'],
        service_release=str(OUT/'fallback-service.release.json'),service_release_sha256=p.sha(OUT/'fallback-service.release.json'),
        generation_receipt=str(OUT/'retained-anchor-generation.json'),manual_root_rollback_requires_stopped=True)
    p.save(OUT/'recovery-anchor.json',anchor)
    release=metadata(target,digest,manifest,'NATIVE_OUTER_RESOURCE_RECOVERY','')
    release.update(native_outer_recovery_targeted_accepted=True,native_outer_enabled=True,
        resource_observation_targeted_accepted=True,resource_domain_optional=True,physical_ram_limit=False,
        cold_nvml_unavailable_business_accepted=False,host_boot_test_accepted=False,
        scope='Native optional outer supervision, existing device group preservation, resource observation and retained-payload recovery. No nginx or systemd requirement. Actual normal-NVML business accepted; cold admission, physical RAM hard cap and other master tasks remain required.',
        version_recovery_registration=str(OUT/'registration.json'),
        version_recovery_registration_sha256=p.sha(OUT/'registration.json'),
        version_recovery_cycle_id=registration['registry']['cycle_id'],
        rollback_receipt=str(OUT/'recovery-anchor.json'),rollback_sha256=p.sha(OUT/'recovery-anchor.json'))
    release['limitations']=['Remaining OpenAI inference/application tasks stay active in the master plan.',
        'NVML-unavailable cold admission, physical DRAM hard boundary and actual whole-host reboot qualification remain active.',
        'Long stability remains user-run; TLS/CORS/proxy/host integration stays deployment-selected.']
    p.save(OUT/'service.release.json',release)
    p.save(OUT/'requests.json',{k:str(uuid.uuid4()) for k in ('anchor','candidate','rollback','forward')})
    p.save(BASE/'plan.json',dict(commands=19,new_gpu_starts=0,from_revision=64,final_revision=72,
        candidate=digest,retained=fallback['manifest_sha256'],scope='STOPPED publish, rollback/forward, explicit unused version-cycle update and native outer enable; no host startup unit or resource installation'))
    print(json.dumps(dict(status='PREPARED',manifest_sha256=digest,commands=19,new_gpu_starts=0)))

def run():
    started=time.monotonic();review=p.read(OUT/'review.json');before=review['original_state']
    p.require(state_hashes()==before and publication.status(STATE)['revision']==64,'default changed before delivery')
    target=OUT/'bundle';digest=p.sha(target/'manifest.json');registration=p.read(OUT/'registration.json')
    fallback=registration['payloads']['fallback']['descriptor'];ids=p.read(OUT/'requests.json');cli=target/'engine/q4t'
    rows=[];version_hashes=None
    def call(label,args):
        p.require(len(rows)<19,'delivery command bound');argv=list(map(str,args));began=time.monotonic()
        result=subprocess.run(argv,capture_output=True,stdin=subprocess.DEVNULL,timeout=45)
        (OUT/(label+'.stdout')).write_bytes(result.stdout);(OUT/(label+'.stderr')).write_bytes(result.stderr)
        row=dict(label=label,argv=argv,returncode=result.returncode,seconds=time.monotonic()-began)
        rows.append(row);p.save(OUT/(label+'.json'),row)
        p.require(result.returncode==0,'delivery failed: '+label)
        p.require(state_hashes()==before,'original core/client/profile state changed')
        if version_hashes is not None:
            p.require(all(p.sha(STATE/n)==h for n,h in version_hashes.items()),'manual rollback altered version history')
        return json.loads(result.stdout)
    def publish(label,package,sha,release,expected,revision,rid):
        return call(label,[cli,'service','publish','--state',STATE,'--default-root',DEFAULT,
            '--package',package,'--manifest-sha256',sha,'--service-release',release,
            '--expected-manifest-sha256',expected,'--expected-revision',revision,'--request-id',rid])
    try:
        p.require(call('previous-check',[cli,'service','check','--state',STATE,'--package',review['previous']['current']['package'],
            '--manifest-sha256',OLD])['desired']=='STOPPED','default is active')
        for label,package,sha in [('candidate',target,digest),('fallback',fallback['package'],fallback['manifest_sha256'])]:
            p.require(call(label+'-check',[cli,'service','check','--package',package,'--manifest-sha256',sha])['status']=='PASS','package verification')
        anchor=publish('anchor-publish',fallback['package'],fallback['manifest_sha256'],OUT/'fallback-service.release.json',OLD,64,ids['anchor'])
        p.require(anchor['durable'] and anchor['revision']==66,'anchor publication');generation=anchor['current']['generation_sha256']
        p.save(OUT/'retained-anchor-generation.json',dict(generation_sha256=generation,manifest_sha256=fallback['manifest_sha256']))
        value=publish('candidate-publish',target,digest,OUT/'service.release.json',fallback['manifest_sha256'],66,ids['candidate'])
        p.require(value['durable'] and value['revision']==68,'candidate publication')
        value=call('version-register',[DEFAULT/'q4t','service','version-register','--state',STATE,'--registration',OUT/'registration.json',
            '--expected-cycle',review['previous_version']['registry']['cycle_id']])
        p.require(value['registered'] and not value['fallback_consumed'],'unused version cycle')
        version_hashes={str(f.relative_to(STATE)):p.sha(f) for f in STATE.rglob('*') if f.is_file() and
            f.relative_to(STATE).parts[0].startswith(('version-selection','version-registrations'))}
        call('version-before',[DEFAULT/'q4t','service','version-status','--state',STATE])
        value=call('root-rollback',[cli,'service','publish-rollback','--state',STATE,'--default-root',DEFAULT,
            '--package',target,'--manifest-sha256',digest,'--generation-sha256',generation,
            '--expected-manifest-sha256',digest,'--expected-revision',68,'--request-id',ids['rollback']])
        p.require(value['durable'] and value['revision']==70,'manual rollback')
        p.require(call('old-root-check',[DEFAULT/'q4t','service','check','--state',STATE])['desired']=='STOPPED','old root check')
        value=publish('root-forward',target,digest,OUT/'service.release.json',fallback['manifest_sha256'],70,ids['forward'])
        p.require(value['durable'] and value['revision']==72,'manual forward')
        p.require(call('outer-init',[DEFAULT/'q4t','service','outer-init','--state',STATE])['initialization_complete'],'outer init')
        p.require(call('outer-enable',[DEFAULT/'q4t','service','outer-enable','--state',STATE])['enabled'],'outer enable')
        for action in ('check','status','observe'):
            value=call('default-'+action,[DEFAULT/'q4t','service',action,'--state',STATE])
            p.require(value['status']=='PASS' if action=='check' else value['state']=='STOPPED','stopped default health')
        profiles=call('default-profiles',[DEFAULT/'q4t','service','profiles-status','--state',STATE])
        p.require(profiles['selected']=='primary','profile selection changed')
        versions=call('default-version',[DEFAULT/'q4t','service','version-status','--state',STATE])
        p.require(versions['cycle_id']==registration['registry']['cycle_id'] and not versions['fallback_consumed'],'version selection')
        outer=call('default-outer',[DEFAULT/'q4t','service','outer-status','--state',STATE])
        p.require(outer['enabled'] and outer['charged_total']==0,'outer should be enabled but never started')
        final=call('publication',[DEFAULT/'q4t','service','publish-status','--state',STATE])
        p.require(final['ready'] and final['revision']==72 and final['current']['manifest_sha256']==digest,'final publication')
        p.save(OUT/'result.json',dict(status='PASS_NATIVE_OUTER_RESOURCE_DELIVERED',seconds=time.monotonic()-started,
            commands=rows,publication=final,retained_anchor_generation=generation,versions=versions,profiles=profiles,outer=outer,
            business_state_preserved=True,version_state_preserved_after_registration=True,engine_unchanged=True,
            state='STOPPED',new_gpu_starts=0,remaining_master_tasks_open=True))
        print(json.dumps(dict(status='PASS_NATIVE_OUTER_RESOURCE_DELIVERED',revision=72,commands=len(rows),manifest_sha256=digest)))
    except BaseException as error:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,category=type(error).__name__,detail=str(error),seconds=time.monotonic()-started));raise

if __name__=='__main__':
    os.umask(0o077)
    {'prepare':prepare,'run':run}[sys.argv[1]]()
