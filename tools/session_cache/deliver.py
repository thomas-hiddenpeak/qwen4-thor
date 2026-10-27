"""Package an accepted session candidate and install with bounded health.

Preparation performs no inference or installation. Deployment requires the
shared GPU lease and uses existing identity-checked atomic replacement helpers.
The two destination files are individually atomic, not a power-loss transaction.
"""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from run_stage import ROOT, legacy
import run_health as health

BASE = ROOT / '.q4t-work/session-cache-20261027'
PACKAGE = BASE / 'delivery-package'
OUT = BASE / 'deployment'
MARKER = ROOT / 'build/session-cache-recovery.json'
OLD = 'f2a6de360e05676c5fee2f86fbd314c9a9ff3ea0378fa104e0185cef6b4ec164'
NEW = '09fe25ba5db65ae0bd343a110f2b7042de06f1bc7184bba51d10704638ad26b3'
spec = importlib.util.spec_from_file_location('session_delivery_helpers',
    ROOT / 'tools/verify/startup_delivery/deploy.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def prepare():
    decision = h.read(BASE / 'final-decision.json')
    h.require(decision['passed'] and decision['candidate_sha256'] == NEW,
              'accepted candidate decision required')
    prior = h.read(BASE / 'phase-d/plan.json')
    legacy.verify_files(prior)
    old = h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', OLD)
    h.require(h.sha(Path(prior['candidate'])) == NEW, 'candidate identity')
    PACKAGE.mkdir()
    for name in ['previous','candidate','tested-source','evidence']:
        (PACKAGE/name).mkdir()
    copies = [(ROOT/'build/q4t','previous/q4t'),
        (ROOT/'build/q4t.release.json','previous/q4t.release.json'),
        (Path(old['build_cache']),'previous/CMakeCache.txt'),
        (Path(prior['candidate']),'candidate/q4t'),
        (BASE/'build/CMakeCache.txt','candidate/CMakeCache.txt'),
        (BASE/'final-decision.json','decision.json'),
        (BASE/'final-audit.json','evidence/final-audit.json'),
        (ROOT/'docs/SESSION_CACHE_RESULT_2026-10-27.md','acceptance.md'),
        (Path(__file__),'deliver.py'),
        (ROOT/'tools/session_cache/run_health.py','health.py')]
    for source, target in copies:
        h.copy_checked(h.identity(source), PACKAGE/target)
    h.verify_pair(PACKAGE/'previous/q4t',PACKAGE/'previous/q4t.release.json',
                  OLD,PACKAGE/'previous/CMakeCache.txt')
    h.require(h.sha(PACKAGE/'candidate/q4t')==NEW,'copied candidate identity')
    shutil.copytree(prior['artifact'], PACKAGE/'artifact')
    for name in ['head.bf16','token_ids.bin','manifest.json']:
        h.require(h.sha(PACKAGE/'artifact'/name)==
                  prior['files'][str(Path(prior['artifact'])/name)],
                  'copied subset identity: '+name)
    shutil.copytree(ROOT/'docs/evidence/session-cache-20261027',
                    PACKAGE/'evidence/summary')
    first = h.read(BASE/'phase-a/plan.json')
    runtime = {str(Path(path).relative_to(ROOT)):digest
        for path,digest in first['files'].items()
        if Path(path).is_relative_to(ROOT/'src') or
           Path(path).is_relative_to(ROOT/'include') or Path(path)==ROOT/'CMakeLists.txt'}
    for name, digest in runtime.items():
        h.require(h.sha(ROOT/name)==digest, 'tested runtime source changed: '+name)
        target = PACKAGE/'tested-source'/name
        target.parent.mkdir(parents=True,exist_ok=True)
        h.copy_checked(h.identity(ROOT/name), target)
        h.require(h.sha(target)==digest,'copied tested source changed: '+name)
    h.save(PACKAGE/'candidate/runtime-source.json',runtime)
    for name in ['configure.log','build-1.log','build-2.log','build-3.log','build-4.log']:
        h.copy_checked(h.identity(BASE/'evidence'/name),PACKAGE/'candidate'/name)
    metadata = dict(version='session-cache-k3-20261027', binary_sha256=NEW,
        source_commit=first['source_commit'], implementation_commit='524ce37',
        source_note='Runtime sources frozen after build-4 and before first HTTP; '
        'all subsequent inference used identical 09fe25ba. Later tools/docs did '
        'not rebuild or alter these runtime inputs.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),
        build_cache_sha256=h.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance=decision['engineering_decision'],
        acceptance_snapshot=str(PACKAGE/'acceptance.md'),
        acceptance_snapshot_sha256=h.sha(PACKAGE/'acceptance.md'),
        acceptance_decision=str(PACKAGE/'decision.json'),
        acceptance_decision_sha256=h.sha(PACKAGE/'decision.json'),
        acceptance_audit=str(PACKAGE/'evidence/final-audit.json'),
        acceptance_audit_sha256=h.sha(PACKAGE/'evidence/final-audit.json'),
        previous_binary=str(PACKAGE/'previous/q4t'),
        previous_release=str(PACKAGE/'previous/q4t.release.json'),
        subset_artifact=str(PACKAGE/'artifact'),
        subset_manifest_sha256=h.sha(PACKAGE/'artifact/manifest.json'),
        defaults=dict(head='full',mtp=True,mtp_k=3,suffix=False,max_seq=1,
                      session_cache=False),
        agent_configuration=dict(max_seq=4,max_prefill=4096,max_len=208896,
            seq_capacities=[208896,65536,65536,65536],admitted_requests=32),
        session_cache=dict(enabled_by='--session-cache-dir DIR plus session_id',
            max_entries=8,disk_budget_bytes=24<<30,ram_budget_bytes=32<<20,
            ttl_seconds=1800,min_prefix=8192,same_process_only=True),
        scope=decision['accepted_scope'],
        atomicity='Individual atomic replaces; durable recovery marker and '
                  'known-identity rollback, not a cross-file power-loss transaction')
    h.save(PACKAGE/'candidate/q4t.release.json',metadata)
    h.verify_pair(PACKAGE/'candidate/q4t',PACKAGE/'candidate/q4t.release.json',NEW)
    plan = copy.deepcopy(prior)
    plan.update(stage='SESSION_HEALTH',output=str(OUT/'run'),
        candidate=str(ROOT/'build/q4t'),artifact=str(PACKAGE/'artifact'),
        http_requests=2,http_loads=1,stage_timeout_seconds=600,
        required_gates=[str(PACKAGE/'decision.json')],
        protection=[dict(id='installed-session')],steps=[])
    plan['steps'] = [dict(name='installed-full',head='full',cache=True,
        binary=str(ROOT/'build/q4t'),kind='block',http_requests=2,http_loads=1,
        timeout=600,command=[sys.executable,'-B',str(ROOT/'tools/session_cache/run_health.py'),
                            '--plan',str(OUT/'plan.json'),'--custom-step','installed-full'])]
    for path in [*PACKAGE.rglob('*'),Path(__file__),ROOT/'tools/session_cache/run_health.py',
                 * (ROOT/'tools/verify/startup_delivery').glob('*.py')]:
        if path.is_file():
            plan['files'][str(path)] = h.sha(path)
    for name in ['q4t','q4t.release.json']:
        plan['files'][str(ROOT/'build'/name)] = h.sha(PACKAGE/'candidate'/name)
    health.validate(plan)
    for key, value in [('http_requests',3),('http_loads',2)]:
        bad = copy.deepcopy(plan);bad[key]=value
        try:
            health.validate(bad)
        except ValueError:
            continue
        raise ValueError('health budget guard accepted '+key)
    OUT.mkdir()
    h.save(OUT/'plan.json',plan)
    h.save(PACKAGE/'health-plan.json',plan)
    receipt = h.seal(PACKAGE)
    h.save(BASE/'package-receipt.json',dict(seal=receipt,runtime_files=len(runtime),
        requests=2,starts=1,deadline=plan['task_deadline_unix'],no_inference=True))
    print(json.dumps(dict(package=str(PACKAGE),runtime_files=len(runtime))))


def restore():
    for name in ['q4t','q4t.release.json']:
        destination = ROOT/'build'/name
        old, new = h.sha(PACKAGE/'previous'/name),h.sha(PACKAGE/'candidate'/name)
        current = h.sha(destination)
        h.require(current in [old,new], 'unknown installed identity; preserve marker')
        if current != old:
            h.replace_checked(PACKAGE/'previous'/name,destination,new)
    h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',OLD)
    if MARKER.exists():
        MARKER.unlink();h.fsync_directory(MARKER.parent)


def deploy():
    h.require_guard(OUT)
    h.verify_seal(PACKAGE)
    plan = h.read(OUT/'plan.json')
    h.require(plan == h.read(PACKAGE/'health-plan.json'),'health plan changed')
    health.validate(plan)
    h.require(not MARKER.exists(),'unresolved recovery marker')
    for name in ['q4t','q4t.release.json']:
        h.require(h.sha(ROOT/'build'/name)==h.sha(PACKAGE/'previous'/name),
                  'installed baseline changed')
    h.save(MARKER,dict(package=str(PACKAGE),output=str(OUT),previous=OLD,candidate=NEW))
    h.fsync_directory(MARKER.parent)
    state = dict(status='INSTALLING',started=time.time())
    child = None
    def interrupted(*_):
        raise KeyboardInterrupt('owned session deployment interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    try:
        for name in ['q4t','q4t.release.json']:
            h.replace_checked(PACKAGE/'candidate'/name,ROOT/'build'/name,
                              h.sha(PACKAGE/'previous'/name))
        h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',NEW)
        with (OUT/'health-driver.log').open('x') as log:
            child = subprocess.Popen([sys.executable,'-B',
                str(ROOT/'tools/session_cache/run_health.py'),'--plan',str(OUT/'plan.json')],
                cwd=ROOT,env=legacy.clean_env(),stdout=log,stderr=subprocess.STDOUT,
                start_new_session=True)
            code = child.wait(timeout=max(.01,min(600,plan['task_deadline_unix']-time.time())))
        h.require(code==0,'installed health driver failed')
        post,run = h.read(OUT/'postflight.json'),h.read(OUT/'run/state.json')
        h.require(post['status']==run['status']=='MEASURED_REQUIRES_REVIEW' and
                  run['requests_completed']==2 and run['loads_started']==1,
                  'health completion contract')
        h.require(not any(post[k] for k in ['monitor_failures','cleanup_errors',
                                           'final_monitor_errors']), 'health environment')
        h.verify_seal(PACKAGE)
        h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',NEW)
        state.update(status='HEALTH_CHECKED_PENDING_GUARD',requests=2,starts=1)
    except BaseException as error:
        if child is not None and child.poll() is None:
            legacy.stop_group(child)
        state.update(status='FAILED',error=repr(error))
        try:
            restore();state['rollback']='RESTORED_PREVIOUS'
        except BaseException as recovery:
            state.update(status='RECOVERY_REQUIRED',rollback_error=repr(recovery))
        raise
    finally:
        state['finished']=time.time()
        h.save(OUT/'deployment.json',state)


def finalize():
    h.require(h.read(OUT/'lease/exit.json')['status']=='COMPLETED',
              'outer GPU guard did not complete')
    h.require(h.read(OUT/'deployment.json')['status']=='HEALTH_CHECKED_PENDING_GUARD',
              'installation health incomplete')
    h.verify_seal(PACKAGE)
    h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',NEW)
    marker = h.read(MARKER)
    h.require(marker['package']==str(PACKAGE) and marker['candidate']==NEW,
              'unknown recovery marker')
    MARKER.unlink();h.fsync_directory(MARKER.parent)
    h.save(OUT/'finalized.json',dict(status='DELIVERED',binary_sha256=NEW,
        sidecar_sha256=h.sha(ROOT/'build/q4t.release.json'),health_requests=2,
        health_starts=1,subset_health='Reuse D same binary/artifact recall',
        persistent_service=False,finished=time.time()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','deploy','finalize','rollback'])
    parser.add_argument('--rollback-output',type=Path)
    args = parser.parse_args()
    if args.action=='prepare':
        prepare()
    elif args.action=='deploy':
        deploy()
    elif args.action=='finalize':
        finalize()
    else:
        if args.rollback_output is None:
            raise ValueError('rollback requires fresh --rollback-output')
        output = args.rollback_output.resolve()
        h.require(output.is_relative_to(ROOT/'.q4t-work'),'rollback evidence path')
        h.require_guard(output)
        h.verify_seal(PACKAGE)
        restore()
        h.save(output/'rollback.json',dict(status='ROLLED_BACK',binary_sha256=OLD,
                                         finished=time.time()))
