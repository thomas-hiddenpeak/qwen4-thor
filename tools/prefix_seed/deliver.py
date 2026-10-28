"""Accepted-only package, atomic file replacement, bounded health and rollback."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import health
common=health.common
ROOT=common.ROOT
BASE=ROOT/'.q4t-work/prefix-seed-20261028'
PACKAGE=BASE/'delivery-package';OUT=BASE/'deployment'
MARKER=ROOT/'build/prefix-seed-recovery.json'
spec=importlib.util.spec_from_file_location('delivery_helpers',ROOT/'tools/verify/startup_delivery/deploy.py')
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
OLD='09fe25ba5db65ae0bd343a110f2b7042de06f1bc7184bba51d10704638ad26b3'
NEW='6f3722b26e5b9d1b08d405e873d1f90d72f4b5c1aec2dd14589d1e27390b7965'


def prepare():
    decision=h.read(BASE/'final-decision.json')
    h.require(decision['passed'] and decision['candidate_sha256']==NEW,'accepted candidate required')
    prior=h.read(BASE/'phase-d/plan.json');common.legacy.verify_files(prior)
    old=h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',OLD)
    audit=h.read(BASE/'final-audit.json')
    h.require(audit['passed'] and audit['cumulative_http']+4<=300 and
              audit['cumulative_starts']+2<=20 and audit['cumulative_gpu_fixtures']<=2,
              'remaining health budget')
    PACKAGE.mkdir()
    for n in ['previous','candidate','tested-source','evidence']:(PACKAGE/n).mkdir()
    copies=[(ROOT/'build/q4t','previous/q4t'),(ROOT/'build/q4t.release.json','previous/q4t.release.json'),
        (Path(old['build_cache']),'previous/CMakeCache.txt'),(Path(prior['candidate']),'candidate/q4t'),
        (BASE/'build/CMakeCache.txt','candidate/CMakeCache.txt'),(BASE/'final-decision.json','decision.json'),
        (BASE/'final-audit.json','evidence/final-audit.json'),
        (ROOT/'docs/PREFIX_SEED_RESULT_2026-10-28.md','acceptance.md'),
        (Path(__file__),'deliver.py'),(ROOT/'tools/prefix_seed/health.py','health.py')]
    for source,target in copies:h.copy_checked(h.identity(source),PACKAGE/target)
    shutil.copytree(prior['artifact'],PACKAGE/'artifact')
    for name in ['head.bf16','token_ids.bin','manifest.json']:
        h.require(h.sha(PACKAGE/'artifact'/name)==prior['files'][str(Path(prior['artifact'])/name)],'subset identity')
    first=h.read(BASE/'phase-a/plan.json')
    runtime={str(Path(p).relative_to(ROOT)):digest for p,digest in first['files'].items()
        if Path(p).is_relative_to(ROOT/'src') or Path(p).is_relative_to(ROOT/'include') or Path(p)==ROOT/'CMakeLists.txt'}
    for name,digest in runtime.items():
        h.require(h.sha(ROOT/name)==digest,'tested source changed: '+name)
        target=PACKAGE/'tested-source'/name;target.parent.mkdir(parents=True,exist_ok=True)
        h.copy_checked(h.identity(ROOT/name),target)
    h.save(PACKAGE/'candidate/runtime-source.json',runtime)
    for name in ['configure.log','build.log','build-final.log']:
        h.copy_checked(h.identity(BASE/name),PACKAGE/'candidate'/name)
    metadata=dict(version='explicit-prefix-seed-k3-20261028',binary_sha256=NEW,
        source_commit='dc52dff1690e99e7e18b29d7c771c0330fbf9d32',source_note='Runtime source manifest binds the first HTTP identity; later reference fixture/tools changes do not change inference binary.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),tested_source_manifest_sha256=h.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),build_cache_sha256=h.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance=decision['engineering_decision'],acceptance_snapshot=str(PACKAGE/'acceptance.md'),acceptance_snapshot_sha256=h.sha(PACKAGE/'acceptance.md'),
        acceptance_decision=str(PACKAGE/'decision.json'),acceptance_decision_sha256=h.sha(PACKAGE/'decision.json'),
        acceptance_audit=str(PACKAGE/'evidence/final-audit.json'),acceptance_audit_sha256=h.sha(PACKAGE/'evidence/final-audit.json'),
        previous_binary=str(PACKAGE/'previous/q4t'),previous_release=str(PACKAGE/'previous/q4t.release.json'),
        subset_artifact=str(PACKAGE/'artifact'),subset_manifest_sha256=h.sha(PACKAGE/'artifact/manifest.json'),
        defaults=dict(head='full',mtp=True,mtp_k=3,suffix=False,max_seq=1,session_cache=False,prefix_cache=False),
        agent_configuration=dict(max_seq=4,max_prefill=4096,max_len=208896,
            seq_capacities=[208896,65536,65536,65536],admitted_requests=32),
        session_cache=dict(enabled_by='--session-cache-dir DIR plus session_id',
            max_entries=8,disk_budget_bytes=24<<30,ram_budget_bytes=32<<20,
            ttl_seconds=1800,min_prefix=8192,same_process_only=True),
        prefix_cache=dict(enabled_by='--prefix-cache-dir DIR plus prefix_cache_scope',checkpoints=[8192,32768,65536],
            write_field='prefix_cache_write_tokens',default_readonly=True,max_writes_per_request=1,
            max_checkpoints=4,checkpoint_range=[1024,131072],max_entries=8,disk_budget_bytes=8<<30,ram_budget_bytes=32<<20,
            ttl_seconds=1800,same_process_only=True),scope=decision['accepted_scope'],
        atomicity='Individual atomic replaces, durable recovery marker, identity-checked rollback; not a two-file power-loss transaction')
    h.save(PACKAGE/'candidate/q4t.release.json',metadata)
    h.verify_pair(PACKAGE/'candidate/q4t',PACKAGE/'candidate/q4t.release.json',NEW)
    h.verify_pair(PACKAGE/'previous/q4t',PACKAGE/'previous/q4t.release.json',OLD,PACKAGE/'previous/CMakeCache.txt')
    plan=copy.deepcopy(prior)
    plan.update(stage='PREFIX_SEED_HEALTH',output=str(OUT/'run'),candidate=str(ROOT/'build/q4t'),artifact=str(PACKAGE/'artifact'),
        http_requests=4,http_loads=2,stage_timeout_seconds=900,required_gates=[str(PACKAGE/'decision.json')],
        protection=[dict(id='installed-prefix-seed')],steps=[dict(name='installed-'+head,head=head,shared=True,session=True,
            binary=str(ROOT/'build/q4t'),kind='block',http_requests=2,http_loads=1,timeout=600,
            command=[sys.executable,'-B',str(ROOT/'tools/prefix_seed/health.py'),'--plan',str(OUT/'plan.json'),'--custom-step','installed-'+head]) for head in ['full','subset']])
    for path in [*PACKAGE.rglob('*'),Path(__file__),ROOT/'tools/prefix_seed/health.py',
                 *(ROOT/'tools/verify/startup_delivery').glob('*.py')]:
        if path.is_file():plan['files'][str(path)]=h.sha(path)
    for name in ['q4t','q4t.release.json']:plan['files'][str(ROOT/'build'/name)]=h.sha(PACKAGE/'candidate'/name)
    health.validate(plan)
    OUT.mkdir();h.save(OUT/'plan.json',plan);h.save(PACKAGE/'health-plan.json',plan)
    h.save(BASE/'package-receipt.json',dict(seal=h.seal(PACKAGE),runtime_files=len(runtime),requests=4,starts=2))


def restore():
    for name in ['q4t','q4t.release.json']:
        destination=ROOT/'build'/name
        old,new=h.sha(PACKAGE/'previous'/name),h.sha(PACKAGE/'candidate'/name)
        current=h.sha(destination);h.require(current in [old,new],'unknown installed identity')
        if current!=old:h.replace_checked(PACKAGE/'previous'/name,destination,new)
    h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',OLD)
    if MARKER.exists():MARKER.unlink();h.fsync_directory(MARKER.parent)


def deploy():
    h.require_guard(OUT);h.verify_seal(PACKAGE)
    plan=h.read(OUT/'plan.json');health.validate(plan)
    h.require(plan==h.read(PACKAGE/'health-plan.json') and not MARKER.exists(),'health plan/recovery marker')
    for name in ['q4t','q4t.release.json']:
        h.require(h.sha(ROOT/'build'/name)==h.sha(PACKAGE/'previous'/name),'baseline identity')
    h.save(MARKER,dict(package=str(PACKAGE),candidate=NEW,previous=OLD));h.fsync_directory(MARKER.parent)
    child=None;state=dict(status='INSTALLING',started=time.time())
    def interrupted(*_):raise KeyboardInterrupt('owned prefix deployment interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    try:
        for name in ['q4t','q4t.release.json']:
            h.replace_checked(PACKAGE/'candidate'/name,ROOT/'build'/name,h.sha(PACKAGE/'previous'/name))
        h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',NEW)
        with (OUT/'health-driver.log').open('x') as log:
            child=subprocess.Popen([sys.executable,'-B',str(ROOT/'tools/prefix_seed/health.py'),'--plan',str(OUT/'plan.json')],
                cwd=ROOT,env=common.legacy.clean_env(),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            code=child.wait(timeout=max(.01,min(900,plan['task_deadline_unix']-time.time())))
        h.require(code==0,'installed health')
        post,run=h.read(OUT/'postflight.json'),h.read(OUT/'run/state.json')
        h.require(post['status']==run['status']=='MEASURED_REQUIRES_REVIEW' and run['requests_completed']==4 and run['loads_started']==2,'health completion')
        h.require(not any(post[k] for k in ['monitor_failures','cleanup_errors','final_monitor_errors']),'health environment')
        h.require(bool(post['final_monitor_barrier']),'health final monitor barrier')
        h.verify_seal(PACKAGE);state['status']='HEALTH_CHECKED_PENDING_GUARD'
    except BaseException as e:
        if child is not None and child.poll() is None:common.legacy.stop_group(child)
        state.update(status='FAILED',error=repr(e))
        try:restore();state['rollback']='RESTORED_PREVIOUS'
        except BaseException as error:state.update(status='RECOVERY_REQUIRED',rollback_error=repr(error))
        raise
    finally:state['finished']=time.time();h.save(OUT/'deployment.json',state)


def finalize():
    h.require(h.read(OUT/'lease/exit.json')['status']=='COMPLETED','outer guard')
    h.require(h.read(OUT/'deployment.json')['status']=='HEALTH_CHECKED_PENDING_GUARD','health state')
    h.require(h.read(BASE/'final-audit.json')['passed'],'combined evidence audit')
    h.verify_seal(PACKAGE);h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',NEW)
    marker=h.read(MARKER);h.require(marker['candidate']==NEW and marker['package']==str(PACKAGE),'recovery identity')
    MARKER.unlink();h.fsync_directory(MARKER.parent)
    h.save(OUT/'finalized.json',dict(status='DELIVERED',binary_sha256=NEW,sidecar_sha256=h.sha(ROOT/'build/q4t.release.json'),
        health_requests=4,health_starts=2,persistent_service=False,finished=time.time()))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','deploy','finalize']);args=p.parse_args()
    globals()[args.action]()
