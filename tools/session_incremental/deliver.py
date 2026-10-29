"""Accepted-only install with a sealed rollback pair and bounded health."""
import argparse
import copy
import importlib.util
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'.q4t-work/session-incremental-delivery-20261029'
PACKAGE=BASE/'delivery-package'
OUT=BASE/'deployment'
MARKER=ROOT/'build/session-incremental-recovery.json'


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


h=module('incremental_deploy_helpers',ROOT/'tools/verify/startup_delivery/deploy.py')
health=module('incremental_health',Path(__file__).with_name('health.py'))
common=health.common


def prepare():
    decision=h.read(BASE/'decision.json')
    prior=h.read(BASE/'plan.json')
    h.require(decision['passed'] and decision['engineering_decision']=='ACCEPT_BOUNDED_INCREMENTAL_PRIVATE_SNAPSHOTS',
              'bounded acceptance required')
    h.require(h.read(BASE/'archive-receipt.json')['passed'],'independent evidence archive')
    common.legacy.verify_files(prior)
    h.require(time.time()<prior['task_deadline_unix'],'delivery deadline')
    old=h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',prior['parent_sha256'])
    PACKAGE.mkdir()
    for n in ['previous','candidate','tested-source','evidence']:(PACKAGE/n).mkdir()
    copies=[(ROOT/'build/q4t','previous/q4t'),(ROOT/'build/q4t.release.json','previous/q4t.release.json'),
        (Path(old['build_cache']),'previous/CMakeCache.txt'),(Path(prior['candidate']),'candidate/q4t'),
        (BASE/'build/CMakeCache.txt','candidate/CMakeCache.txt'),(BASE/'decision.json','decision.json'),
        (BASE/'archive-receipt.json','evidence/archive-receipt.json'),
        (BASE/'dependency-review.json','evidence/dependency-review.json'),
        (ROOT/'docs/SESSION_INCREMENTAL_DELIVERY_PLAN_2026-10-29.md','acceptance-contract.md'),
        (Path(__file__),'deliver.py'),(Path(__file__).with_name('health.py'),'health.py')]
    for source,target in copies:h.copy_checked(h.identity(source),PACKAGE/target)
    shutil.copytree(prior['artifact'],PACKAGE/'artifact')
    for n in ['head.bf16','token_ids.bin','manifest.json']:
        h.require(h.sha(PACKAGE/'artifact'/n)==prior['files'][str(Path(prior['artifact'])/n)],'subset identity')
    runtime={str(Path(p).relative_to(ROOT)):digest for p,digest in prior['files'].items()
        if Path(p).is_relative_to(ROOT/'src') or Path(p).is_relative_to(ROOT/'include') or Path(p)==ROOT/'CMakeLists.txt'}
    for name,digest in runtime.items():
        h.require(h.sha(ROOT/name)==digest,'runtime source changed')
        target=PACKAGE/'tested-source'/name;target.parent.mkdir(parents=True,exist_ok=True)
        h.copy_checked(h.identity(ROOT/name),target)
    h.save(PACKAGE/'candidate/runtime-source.json',runtime)
    for name in ['configure.log','build.log','cpu-build.log']:
        h.copy_checked(h.identity(BASE/name),PACKAGE/'candidate'/name)
    metadata=copy.deepcopy(old)
    metadata.update(version='session-incremental-k3-20261029',binary_sha256=prior['candidate_sha256'],
        source_commit=prior['source_commit'],source_note='Main-based isolated private snapshot changes; tested manifest binds runtime. Research scoring, preallocation and pipeline excluded.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),build_cache_sha256=h.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance=decision['engineering_decision'],
        acceptance_snapshot=str(PACKAGE/'acceptance-contract.md'),acceptance_snapshot_sha256=h.sha(PACKAGE/'acceptance-contract.md'),
        acceptance_decision=str(PACKAGE/'decision.json'),acceptance_decision_sha256=h.sha(PACKAGE/'decision.json'),
        acceptance_audit=str(PACKAGE/'evidence/archive-receipt.json'),acceptance_audit_sha256=h.sha(PACKAGE/'evidence/archive-receipt.json'),
        previous_binary=str(PACKAGE/'previous/q4t'),previous_release=str(PACKAGE/'previous/q4t.release.json'),
        subset_artifact=str(PACKAGE/'artifact'),subset_manifest_sha256=h.sha(PACKAGE/'artifact/manifest.json'),
        incremental=dict(enabled_by='Q4T_SESSION_INCREMENTAL=1',default=False,private_only=True,
            block_bytes=8<<20,max_ranges=1024,max_blocks=8192,max_entries=8,
            first_save='full',restore_required_in_current_lease=True,checksums='all valid bytes every save and restore',
            same_process_only=True,statistical_noninferiority=False))
    metadata['defaults']['session_incremental']=False
    h.save(PACKAGE/'candidate/q4t.release.json',metadata)
    h.verify_pair(PACKAGE/'candidate/q4t',PACKAGE/'candidate/q4t.release.json',prior['candidate_sha256'])
    h.verify_pair(PACKAGE/'previous/q4t',PACKAGE/'previous/q4t.release.json',prior['parent_sha256'],PACKAGE/'previous/CMakeCache.txt')
    plan=copy.deepcopy(prior)
    plan.update(stage='INCREMENTAL_HEALTH',output=str(OUT/'run'),lease_directory=str(OUT/'lease'),
        candidate=str(ROOT/'build/q4t'),artifact=str(PACKAGE/'artifact'),http_requests=6,http_loads=2,
        stage_timeout_seconds=min(900,int(prior['task_deadline_unix']-time.time())),
        required_gate=str(PACKAGE/'decision.json'),protection=[dict(id='installed-incremental')],
        steps=[dict(name='installed-'+head,head=head,shared=True,session=True,binary=str(ROOT/'build/q4t'),
            kind='block',http_requests=3,http_loads=1,timeout=420,
            command=[sys.executable,'-B',str(ROOT/'tools/session_incremental/health.py'),
                '--plan',str(OUT/'plan.json'),'--custom-step','installed-'+head]) for head in ['full','subset']])
    # Default replacement is expected only during the owned deployment stage.
    for path in PACKAGE.rglob('*'):
        if path.is_file():plan['files'][str(path)]=h.sha(path)
    for name in ['q4t','q4t.release.json']:
        plan['files'][str(ROOT/'build'/name)]=h.sha(PACKAGE/'candidate'/name)
    health.validate(plan)
    OUT.mkdir();h.save(OUT/'plan.json',plan);h.save(PACKAGE/'health-plan.json',plan)
    h.save(BASE/'package-receipt.json',dict(seal=h.seal(PACKAGE),runtime_files=len(runtime),requests=6,starts=2))


def restore():
    h.verify_seal(PACKAGE)
    if MARKER.exists():h.require(h.read(MARKER)['package']==str(PACKAGE),'foreign recovery marker')
    for name in ['q4t','q4t.release.json']:
        path=ROOT/'build'/name
        old,new=h.sha(PACKAGE/'previous'/name),h.sha(PACKAGE/'candidate'/name)
        current=h.sha(path);h.require(current in [old,new],'unknown installed identity')
        if current!=old:h.replace_checked(PACKAGE/'previous'/name,path,new)
    h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',h.sha(PACKAGE/'previous/q4t'))
    if MARKER.exists():MARKER.unlink();h.fsync_directory(MARKER.parent)


def deploy():
    common.legacy.require_guard()
    h.verify_seal(PACKAGE)
    plan=h.read(OUT/'plan.json');health.validate(plan)
    h.require(plan==h.read(PACKAGE/'health-plan.json') and not MARKER.exists(),'health plan/recovery state')
    for name in ['q4t','q4t.release.json']:
        h.require(h.sha(ROOT/'build'/name)==h.sha(PACKAGE/'previous'/name),'baseline identity')
    h.save(MARKER,dict(package=str(PACKAGE),candidate=plan['candidate_sha256'],previous=plan['parent_sha256']))
    h.fsync_directory(MARKER.parent)
    state,child=dict(status='INSTALLING',started=time.time()),None
    def interrupted(*_):raise KeyboardInterrupt('owned deployment interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    try:
        for name in ['q4t','q4t.release.json']:
            h.replace_checked(PACKAGE/'candidate'/name,ROOT/'build'/name,h.sha(PACKAGE/'previous'/name))
        h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',plan['candidate_sha256'])
        with (OUT/'health-driver.log').open('x') as log:
            child=subprocess.Popen([sys.executable,'-B',str(ROOT/'tools/session_incremental/health.py'),
                '--plan',str(OUT/'plan.json')],cwd=ROOT,env=common.legacy.clean_env(),stdout=log,
                stderr=subprocess.STDOUT,start_new_session=True)
            code=child.wait(timeout=max(.01,min(900,plan['task_deadline_unix']-time.time())))
        h.require(code==0,'installed health failed')
        post,run=h.read(OUT/'postflight.json'),h.read(OUT/'run/state.json')
        h.require(post['status']==run['status']=='MEASURED_REQUIRES_REVIEW' and
                  run['requests_completed']==6 and run['loads_started']==2,'health completion')
        h.require(not any(post[k] for k in ['monitor_failures','cleanup_errors','final_monitor_errors'])
                  and bool(post['final_monitor_barrier']),'health monitor final barrier')
        h.verify_seal(PACKAGE)
        state['status']='HEALTH_CHECKED_PENDING_GUARD'
    except BaseException as error:
        if child is not None and child.poll() is None:common.legacy.stop_group(child)
        state.update(status='FAILED',error=repr(error))
        try:restore();state['rollback']='RESTORED_PREVIOUS'
        except BaseException as failure:state.update(status='RECOVERY_REQUIRED',rollback_error=repr(failure))
        raise
    finally:
        state['finished']=time.time();h.save(OUT/'deployment.json',state)


def finalize():
    h.require(h.read(OUT/'lease/exit.json')['status']=='COMPLETED','outer guard')
    h.require(h.read(OUT/'deployment.json')['status']=='HEALTH_CHECKED_PENDING_GUARD','health state')
    plan=h.read(OUT/'plan.json')
    common.legacy.verify_files(plan)
    for step in plan['steps']:
        common.audit(OUT/'run'/step['name'],3)
        h.require(h.read(OUT/'run'/step['name']/'health-result.json')['passed'],'installed HTTP outputs')
    h.require(h.read(OUT/'deployment.json')['finished']<=plan['task_deadline_unix'],'absolute deadline')
    h.verify_seal(PACKAGE)
    h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',plan['candidate_sha256'])
    marker=h.read(MARKER);h.require(marker['package']==str(PACKAGE),'recovery marker')
    MARKER.unlink();h.fsync_directory(MARKER.parent)
    h.save(OUT/'finalized.json',dict(status='DELIVERED',binary_sha256=plan['candidate_sha256'],
        sidecar_sha256=h.sha(ROOT/'build/q4t.release.json'),health_requests=6,health_starts=2,
        persistent_service=False,finished=time.time()))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','deploy','finalize','rollback'])
    args=parser.parse_args()
    if args.action=='rollback':common.legacy.require_guard();restore()
    else:globals()[args.action]()
