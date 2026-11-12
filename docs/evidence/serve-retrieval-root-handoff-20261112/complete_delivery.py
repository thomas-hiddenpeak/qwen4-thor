"""Explicit STOPPED control-root registration; preserve failed original and budgets."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle,publication,version_contract,version_policy
from tools.serve_service import package as p
WORK=Path(__file__).resolve().parent
OLD=WORK/'delivery-01';OUT=WORK/'delivery-completion-01'
STATE=ROOT/'build/service-native-state';DEFAULT=ROOT/'build'
PRIMARY=WORK/'candidate-01/unqualified-bundle'
FALLBACK=WORK/'managed-packages-01/retained'
PS='32d3b81900e1339918dfed97663e441a6628a360137a5420da98c5b21eec5853'
FS='241a4ef423bdb989ced922041e1041b35ac3c8bee7076c2347eff4c308247434'


def preserved():
    return {str(f.relative_to(STATE)):p.sha(f) for f in STATE.rglob('*') if f.is_file() and
        (f.name=='ledger.json' or f.relative_to(STATE).parts[0].startswith(
            ('client-','outer-','profile-')))}


def prepare():
    p.require(not OUT.exists(),'preserve original completion')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
              'commit reviewed completion first')
    current=publication.status(STATE)
    p.require(current['ready'] and current['revision']==88 and
              current['current']['manifest_sha256']==PS,'primary changed')
    prior=p.read(STATE/'version-selection/policy.json')['state']['policy']
    version_policy.validate(prior)
    p.require(prior['selected']=='primary' and not prior['faults'] and
        all(prior[k] is None for k in ('switch','completed','terminal_fault')),
        'never reset consumed recovery state for delivery validation')
    for path,digest in [(PRIMARY,PS),(FALLBACK,FS)]:bundle.verify(path,digest)
    p.require(p.read(OLD/'result.json')['status']=='FAILED' and
        p.read(OLD/'safe-root-forward.json')['protected_state_unchanged'],
        'original failure/forward evidence required')
    actual=p.read(OLD/'review.json')['actual_business']
    roots=dict(primary=dict(package=str(PRIMARY),manifest_sha256=PS),
               fallback=dict(package=str(FALLBACK),manifest_sha256=FS))
    rollback=version_contract.prepare(roots['fallback'],roots['fallback'],roots['primary'],
        str(uuid.uuid4()),fallback_acceptance=actual['primary']['identity'])
    forward=version_contract.prepare(roots['primary'],roots['primary'],roots['fallback'],
        str(uuid.uuid4()),fallback_acceptance=actual['fallback']['identity'])
    OUT.mkdir(mode=0o700)
    for name,registration in [('rollback',rollback),('forward',forward)]:
        p.save(OUT/(name+'-registration.json'),registration)
    release=p.read(OLD/'primary-service.release.json')
    release.update(version_recovery_registration=str(OUT/'forward-registration.json'),
        version_recovery_registration_sha256=p.sha(OUT/'forward-registration.json'),
        version_recovery_cycle_id=forward['registry']['cycle_id'],
        control_root_handoff_plan=str(OUT/'plan.json'),
        previous_delivery_failure=str(OLD/'result.json'))
    p.save(OUT/'primary-service.release.json',release)
    p.save(OUT/'plan.json',dict(commands=20,new_gpu_starts=0,source_commit=
        subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        previous=current,previous_version=prior,preserved=preserved(),
        version_records={f.name:p.sha(f) for f in (STATE/'version-registrations').iterdir()},
        retired_anchor=p.read(OLD/'retained-anchor-generation.json')['generation_sha256'],
        rollback_request=str(uuid.uuid4()),forward_request=str(uuid.uuid4()),
        final_revision=92,existing_cycles_never_rewritten=True,automatic_budget_reset=False))
    print('PREPARED explicit root handoff; no model action')


def run():
    plan=p.read(OUT/'plan.json');rows=[];started=time.monotonic()
    p.require(publication.status(STATE)['revision']==88 and preserved()==plan['preserved'],
              'completion predecessor changed')
    previous=plan['previous_version']
    def call(label,args):
        p.require(len(rows)<plan['commands'],'completion command bound')
        begin=time.monotonic();args=list(map(str,args))
        r=subprocess.run(args,capture_output=True,stdin=subprocess.DEVNULL,timeout=45)
        (OUT/(label+'.stdout')).write_bytes(r.stdout);(OUT/(label+'.stderr')).write_bytes(r.stderr)
        row=dict(label=label,argv=args,returncode=r.returncode,seconds=time.monotonic()-begin)
        rows.append(row);p.save(OUT/(label+'.json'),row)
        p.require(r.returncode==0,'completion failed: '+label)
        p.require(preserved()==plan['preserved'],'protected state changed')
        p.require(all(p.sha(STATE/'version-registrations'/name)==h
            for name,h in plan['version_records'].items()),'prior registration changed')
        return json.loads(r.stdout)
    def cli(label,action,*args):
        return call(label,[DEFAULT/'q4t','service',action,'--state',STATE,*args])
    def register(name):
        nonlocal previous
        reg=p.read(OUT/(name+'-registration.json'))
        value=cli(name+'-register','version-register','--registration',OUT/(name+'-registration.json'),
                  '--expected-cycle',previous['registry']['cycle_id'])
        p.require(value['registered'] and not value['fallback_consumed'],'explicit registration failed')
        intent=p.read(STATE/'version-registrations'/(reg['registry']['cycle_id']+'.json'))
        p.require(intent['previous']==previous,'old cycle not retained')
        now=p.read(STATE/'version-selection/policy.json')['state']['policy']
        p.require(now==intent['candidate'] and now['revision']==previous['revision']+1,
                  'new cycle differs')
        p.require(all(now[k]==previous[k] for k in ('selected','faults','switch','completed','terminal_fault')),
                  'delivery registration resets consumed work')
        previous=now
    try:
        p.require(cli('primary-check','check')['desired']=='STOPPED','primary active')
        cli('primary-version','version-status')
        v=call('root-rollback',[PRIMARY/'engine/q4t','service','publish-rollback','--state',STATE,
            '--default-root',DEFAULT,'--package',PRIMARY,'--manifest-sha256',PS,
            '--generation-sha256',plan['retired_anchor'],'--expected-manifest-sha256',PS,
            '--expected-revision',88,'--request-id',plan['rollback_request']])
        p.require(v['revision']==90 and v['durable'],'rollback not durable')
        register('rollback')
        p.require(cli('old-root-check','check')['desired']=='STOPPED','old root active')
        p.require(cli('old-root-status','status')['state']=='STOPPED','old root status')
        p.require(cli('old-root-profiles','profiles-status')['selected']=='primary','profile selection')
        p.require(not cli('old-root-version','version-status')['fallback_consumed'],'old cycle consumed')
        outer=cli('old-root-outer','outer-status')
        p.require(outer['enabled'] and outer['charged_total']==0,'native outer lost')
        v=call('root-forward',[PRIMARY/'engine/q4t','service','publish','--state',STATE,
            '--default-root',DEFAULT,'--package',PRIMARY,'--manifest-sha256',PS,
            '--service-release',OUT/'primary-service.release.json','--expected-manifest-sha256',FS,
            '--expected-revision',90,'--request-id',plan['forward_request']])
        p.require(v['revision']==92 and v['durable'],'forward not durable')
        register('forward')
        for action in ('check','status','observe'):
            v=cli('default-'+action,action)
            p.require(v['status']=='PASS' if action=='check' else v['state']=='STOPPED','final health')
        profiles=cli('default-profiles','profiles-status')
        versions=cli('default-version','version-status')
        outer=cli('default-outer','outer-status')
        final=cli('publication','publish-status')
        p.require(profiles['selected']=='primary' and not versions['fallback_consumed'] and
            outer['enabled'] and outer['charged_total']==0 and final['ready'] and
            final['revision']==92 and final['current']['manifest_sha256']==PS,'final state differs')
        p.save(OUT/'result.json',dict(status='PASS_NATIVE_RETRIEVAL_DELIVERED',commands=rows,
            seconds=time.monotonic()-started,publication=final,profiles=profiles,versions=versions,
            outer=outer,core_client_profile_outer_preserved=True,old_version_cycles_retained=True,
            new_gpu_starts=0,state='STOPPED',original_failure=str(OLD/'result.json'),
            remaining_master_tasks_open=True))
        print(json.dumps(dict(status='PASS_NATIVE_RETRIEVAL_DELIVERED',commands=len(rows),revision=92)))
    except BaseException as e:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,seconds=time.monotonic()-started,
                                   category=type(e).__name__,detail=str(e)));raise


if __name__=='__main__':
    os.umask(0o077)
    {'prepare':prepare,'run':run}[sys.argv[1]]()
