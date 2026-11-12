"""One STOPPED publication; retain rev92 and all charged recovery history."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle, ledger, profile_manager, profile_migration
from tools.serve_recovery import publication, version_contract, version_policy
from tools.serve_service import package as p

WORK = Path(__file__).resolve().parent
OUT = WORK/'delivery-01'
STATE = ROOT/'build/service-native-state'
DEFAULT = ROOT/'build'
PRIMARY = WORK/'candidate-01/bundle'


def preserved():
    return {str(f.relative_to(STATE)):p.sha(f) for f in STATE.rglob('*') if f.is_file()
        and (f.name=='ledger.json' or f.relative_to(STATE).parts[0].startswith(('client-','outer-')))}


def profiles():
    return p.read(STATE/'profile-selection/policy.json')['state']['policy']


def prepare():
    p.require(not OUT.exists(),'preserve previous delivery')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
              'commit reviewed sources first')
    current=publication.status(STATE)
    p.require(current['ready'] and current['revision']==92 and
        current['current']['manifest_sha256']=='32d3b81900e1339918dfed97663e441a6628a360137a5420da98c5b21eec5853',
        'default predecessor changed')
    p.require(len(list((DEFAULT/'.q4t-publication/generations').iterdir()))<32,
              'one generation slot required')
    previous=Path(current['current']['package'])
    old=bundle.verify(previous,current['current']['manifest_sha256'])
    new=bundle.verify(PRIMARY,p.sha(PRIMARY/'manifest.json'))
    for name,digest in new['files'].items():
        if name.startswith('source/'):
            p.require(p.sha(ROOT/name[7:])==digest,'control source changed: '+name)
    for name,digest in p.read(PRIMARY/'native-build.json')['source_files'].items():
        p.require(p.sha(ROOT/name)==digest,'native source changed: '+name)
    review=p.read(WORK/'candidate-01/prepare-review.json')
    actual=p.read(WORK/'cache-business-02/acceptance.json')
    p.require(actual['status']=='ACCEPT_TARGETED_CACHE_CANCELLATION_BUSINESS' and
        actual['binary_sha256']==p.sha(PRIMARY/'engine/q4t'),'actual engine differs')
    old_release=(DEFAULT/'service.release.json').resolve(strict=True)
    metadata=p.read(old_release)
    old_decision=Path(metadata['acceptance_record']).resolve(strict=True)
    identity={k:dict(path=str(path),sha256=p.sha(path)) for k,path in
              [('release',old_release),('decision',old_decision)]}
    root=dict(package=str(PRIMARY),manifest_sha256=p.sha(PRIMARY/'manifest.json'))
    fallback=dict(package=str(previous),manifest_sha256=current['current']['manifest_sha256'])
    version_contract.accepted_fallback(identity,version_contract.payload(fallback))
    registration=version_contract.prepare(root,root,fallback,str(uuid.uuid4()),
                                           fallback_acceptance=identity)
    old_version=p.read(STATE/'version-selection/policy.json')['state']['policy']
    version_policy.validate(old_version)
    p.require(old_version['selected']=='primary' and not old_version['faults'] and
        all(old_version[k] is None for k in ('switch','completed','terminal_fault')),
        'do not reset consumed recovery state')
    old_profiles=profiles()
    permit=profile_manager.compatible(old,new,previous,PRIMARY)
    stamp=old_profiles['clock']
    expected=profile_migration.candidate(old_profiles,permit,
        ledger.Stamp(stamp['boot_id'],stamp['monotonic_ns'])) if permit else old_profiles
    p.require(all(old_profiles[k]==expected[k] for k in ('history','pending','selected',
        'not_before_ns','light_since_ns','clock','profiles','configuration')),
        'profile migration alters capacity or budgets')
    OUT.mkdir(mode=0o700)
    shutil.copytree(STATE,OUT/'previous-state',symlinks=True)
    p.save(OUT/'registration.json',registration)
    rollback=dict(package=str(previous),manifest_sha256=fallback['manifest_sha256'],
        generation_sha256=current['current']['generation_sha256'],
        release=identity['release'],decision=identity['decision'],
        fallback_known_limit='rev92 predates cache cancellation cleanup repair and optional frontend route synchronization; retained for explicit rollback with its original evidence.',
        manual_root_rollback_requires_stopped=True,
        manual_root_handoff_requires_explicit_version_registration=True)
    p.save(OUT/'rollback.json',rollback)
    protected=preserved()
    plan=dict(commands=11,new_gpu_starts=0,source_commit=subprocess.check_output(
        ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),previous=current,
        previous_version=old_version,previous_profiles=old_profiles,expected_profiles=expected,
        profile_migration=permit,previous_acceptance=identity,preserved=protected,
        version_records={f.name:p.sha(f) for f in (STATE/'version-registrations').iterdir()},
        request_id=str(uuid.uuid4()),final_revision=94,source_review=review,
        recovery_mechanism_unchanged=True,manual_rollback_reexecuted=False)
    p.save(OUT/'plan.json',plan)
    decision=dict(status='PASS_TARGETED_CACHE_AND_OPTIONAL_ROUTES',
        acceptance='ACCEPT_TARGETED_CACHE_AND_OPTIONAL_ROUTES',
        manifest_sha256=root['manifest_sha256'],engine_binary_sha256=actual['binary_sha256'],
        actual_business=actual,source_review=review,
        unchanged_recovery_evidence=identity,
        optional_frontend_evidence='docs/evidence/serve-optional-route-parity-20261112/README.md',
        config_schema_evidence='docs/evidence/serve-cache-config-contract-20261112/README.md',
        new_gpu_starts=0,performance_acceptance=False,long_stability_accepted=False,
        remaining_master_tasks_open=True)
    p.save(OUT/'decision.json',decision)
    release=copy.deepcopy(metadata)
    base=p.read(PRIMARY/'engine/manifest.json')
    engine_metadata=p.read(PRIMARY/'engine/q4t.release.json')
    release.update(acceptance=decision['acceptance'],acceptance_record=str(OUT/'decision.json'),
        acceptance_sha256=p.sha(OUT/'decision.json'),package=str(PRIMARY),
        manifest_sha256=root['manifest_sha256'],engine_binary_sha256=base['files']['q4t'],
        engine_release_sha256=base['files']['q4t.release.json'],control_review=str(OUT/'plan.json'),
        previous_acceptance_record=str(old_decision),
        adaptive_profiles_registry=new['recovery_profiles'],
        adaptive_profiles_policy=new['recovery_profile_policy'],
        client_artifacts=engine_metadata['client_artifacts']['directory'],
        rollback_receipt=str(OUT/'rollback.json'),rollback_sha256=p.sha(OUT/'rollback.json'),
        version_recovery_registration=str(OUT/'registration.json'),
        version_recovery_registration_sha256=p.sha(OUT/'registration.json'),
        version_recovery_cycle_id=registration['registry']['cycle_id'],
        control_root_handoff_plan=str(OUT/'plan.json'),cache_cancellation_targeted_accepted=True,
        optional_frontend_route_inventory_accepted=True,
        scope='Cache cancellation cleanup repair and exact 66-operation optional frontend parity, plus repaired native config schema. Actual same-PID cache failure/recovery and primary/light business qualified; unchanged native lifecycle, TLS, retrieval and recovery evidence reused. All remaining master tasks stay active.')
    release.pop('previous_delivery_failure',None)
    p.save(OUT/'service.release.json',release)
    p.require(protected==preserved() and old_profiles==profiles(),'prepare mutated state')
    print(json.dumps(dict(status='PREPARED',commands=11,new_gpu_starts=0)))


def run():
    plan=p.read(OUT/'plan.json');rows=[];started=time.monotonic()
    p.require(publication.status(STATE)['revision']==92 and profiles()==plan['previous_profiles']
              and preserved()==plan['preserved'],'predecessor changed')
    expected=plan['previous_profiles']
    def call(label,args):
        p.require(len(rows)<plan['commands'],'command bound')
        begin=time.monotonic();argv=list(map(str,args))
        result=subprocess.run(argv,capture_output=True,stdin=subprocess.DEVNULL,timeout=45)
        (OUT/(label+'.stdout')).write_bytes(result.stdout)
        (OUT/(label+'.stderr')).write_bytes(result.stderr)
        row=dict(label=label,argv=argv,returncode=result.returncode,seconds=time.monotonic()-begin)
        rows.append(row);p.save(OUT/(label+'.json'),row)
        p.require(result.returncode==0,'delivery failed: '+label)
        p.require(preserved()==plan['preserved'] and profiles()==expected,'protected state changed')
        p.require(all(p.sha(STATE/'version-registrations'/n)==h
            for n,h in plan['version_records'].items()),'prior version registration changed')
        return json.loads(result.stdout)
    def cli(label,action,*args):
        return call(label,[DEFAULT/'q4t','service',action,'--state',STATE,*args])
    try:
        p.require(cli('previous-check','check')['desired']=='STOPPED','default active')
        p.require(call('candidate-check',[PRIMARY/'engine/q4t','service','check','--package',PRIMARY,
            '--manifest-sha256',p.sha(PRIMARY/'manifest.json')])['status']=='PASS','package check')
        expected=plan['expected_profiles']
        pub=call('publish',[PRIMARY/'engine/q4t','service','publish','--state',STATE,
            '--default-root',DEFAULT,'--package',PRIMARY,'--manifest-sha256',p.sha(PRIMARY/'manifest.json'),
            '--service-release',OUT/'service.release.json','--expected-manifest-sha256',
            plan['previous']['current']['manifest_sha256'],'--expected-revision',92,
            '--request-id',plan['request_id']])
        p.require(pub['durable'] and pub['revision']==94,'publication not durable')
        registration=p.read(OUT/'registration.json')
        registered=cli('register','version-register','--registration',OUT/'registration.json',
            '--expected-cycle',plan['previous_version']['registry']['cycle_id'])
        p.require(registered['registered'] and not registered['fallback_consumed'],'registration failed')
        record=p.read(STATE/'version-registrations'/(registration['registry']['cycle_id']+'.json'))
        version=p.read(STATE/'version-selection/policy.json')['state']['policy']
        p.require(record['previous']==plan['previous_version'] and version==record['candidate'] and
            version['revision']==plan['previous_version']['revision']+1 and
            all(version[k]==plan['previous_version'][k] for k in
                ('selected','faults','switch','completed','terminal_fault')),'version budget changed')
        for action in ('check','status','observe'):
            value=cli('default-'+action,action)
            p.require(value['status']=='PASS' if action=='check' else value['state']=='STOPPED',
                      'final state differs')
        profile=cli('profiles','profiles-status')
        versions=cli('version','version-status')
        outer=cli('outer','outer-status')
        final=cli('publication','publish-status')
        p.require(profile['selected']=='primary' and not versions['fallback_consumed'] and
            outer['enabled'] and outer['charged_total']==0 and final['ready'] and
            final['revision']==94 and final['current']['manifest_sha256']==p.sha(PRIMARY/'manifest.json'),
            'final publication differs')
        p.save(OUT/'result.json',dict(status='PASS_TARGETED_CACHE_ROUTES_DELIVERED',commands=rows,
            seconds=time.monotonic()-started,publication=final,profiles=profile,versions=versions,
            outer=outer,core_client_outer_preserved=True,profile_budget_preserved=True,
            state='STOPPED',new_gpu_starts=0,remaining_master_tasks_open=True))
        print(json.dumps(dict(status='PASS_TARGETED_CACHE_ROUTES_DELIVERED',revision=94,commands=len(rows))))
    except BaseException as error:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,detail=str(error),
            category=type(error).__name__,seconds=time.monotonic()-started))
        raise


if __name__=='__main__':
    os.umask(0o077)
    {'prepare':prepare,'run':run}[sys.argv[1]]()
