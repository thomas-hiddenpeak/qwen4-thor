"""Freeze the local-operations stage; reuse the unchanged accepted runtime."""
import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_pressure import stage as previous

h,a=previous.h,previous.a
BASE=ROOT/'.q4t-work/serve-ops-20261103'
PACKAGE,OUTPUT=BASE/'package',BASE/'execution'
LIMITS=dict(generations=4,negatives=2,starts=2,seconds=1800)
PLANNED=dict(generations=3,negatives=0,starts=1)


def dependencies():
    expected=h.read(BASE/'deps/identity.json')
    h.require(h.sha(BASE/'deps/promtool')==expected['tool_sha256'], 'promtool changed')
    server=h.read(BASE/'deps/server-identity.json')
    h.check_identity(server['binary'])
    h.require(server['source_tar_sha256']==expected['tar_sha256'], 'dependency source changed')
    return dict(promtool=h.identity(BASE/'deps/promtool'),prometheus=server['binary'],
                provenance=expected,server=server)


def prepare():
    h.require(not PACKAGE.exists(),'preserve frozen package')
    h.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
              'commit reviewed implementation before freezing')
    previous.installed()
    h.require(h.read(BASE/'cpu/group-03/result.json')['passed'] and
              h.read(BASE/'cpu/followup/result.json')['passed'],'CPU gate')
    inherited=h.read(previous.PACKAGE/'plan.json')
    a.verify_hashes(inherited['model_dir'],inherited['model_inputs'])
    sdk=previous.previous.previous.prior.sdk_identity()
    deps=dependencies()
    PACKAGE.mkdir()
    shutil.copytree(BASE/'cpu',PACKAGE/'cpu',ignore=shutil.ignore_patterns('tmp'))
    for name in ('identity.json','server-identity.json','sha256sums.txt','LICENSE','NOTICE'):
        shutil.copy2(BASE/'deps'/name,PACKAGE/name)
    paths=subprocess.check_output(['git','ls-files','tools','deploy/serve-ops'],cwd=ROOT,text=True).splitlines()
    paths+=['docs/SERVE_OPERATIONS_2026-11-03.md','docs/SERVE_OPERATIONS.md','docs/MODEL.md']
    paths+=[str(p.relative_to(ROOT)) for p in (ROOT/'docs').glob('serve-*.json')]
    h.save(PACKAGE/'sources.json',{p:h.sha(ROOT/p) for p in sorted(set(paths)) if (ROOT/p).is_file()})
    h.save(PACKAGE/'sdk.json',sdk)
    h.save(PACKAGE/'dependencies.json',deps)
    plan=dict(limits=LIMITS,planned=PLANNED,source_commit=subprocess.check_output(
        ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        binary=h.identity(ROOT/'build/q4t'),sidecar=h.identity(ROOT/'build/q4t.release.json'),
        protocol_sha256=inherited['protocol_sha256'],model_dir=inherited['model_dir'],
        model_inputs=inherited['model_inputs'],sdk_directory=sdk['sdk_directory'],
        port=18107,prometheus_port=19092,prometheus=deps['prometheus'],
        reuse=h.identity(previous.OUTPUT/'finalized.json'),runtime_changed=False,
        performance_acceptance=False,deployment_changed=False)
    h.save(PACKAGE/'plan.json',plan)
    h.seal(PACKAGE)
    print('PREPARED; unchanged default binary and sidecar',flush=True)


def check():
    h.verify_seal(PACKAGE)
    plan=h.read(PACKAGE/'plan.json')
    h.require(plan['limits']==LIMITS and plan['planned']==PLANNED,'frozen budget changed')
    previous.installed()
    a.verify_hashes(ROOT,h.read(PACKAGE/'sources.json'))
    a.verify_hashes(plan['model_dir'],plan['model_inputs'])
    h.require(dependencies()==h.read(PACKAGE/'dependencies.json'),'dependencies changed')
    h.require(previous.previous.previous.prior.sdk_identity()==h.read(PACKAGE/'sdk.json'),'SDK changed')
    return plan


def execute():
    h.require_guard(OUTPUT)
    plan=check()
    events=[json.loads(line) for line in (OUTPUT/'lease/events.jsonl').read_text().splitlines()]
    plan['started']=datetime.datetime.fromisoformat(next(r['utc'] for r in events
        if r['event']=='lease_acquired')).timestamp()
    h.save(OUTPUT/'begin.json',dict(started=plan['started'],limits=LIMITS))
    from tools.serve_ops.verify import Run
    Run(plan,OUTPUT/'business').run()


def finalize():
    check()
    guard=h.read(OUTPUT/'lease/exit.json')
    result=h.read(OUTPUT/'business/result.json')
    elapsed=time.time()-h.read(OUTPUT/'begin.json')['started']
    h.require(guard['status']=='COMPLETED' and guard['child_returncode']==0 and
        result['status']=='PASS_BOUNDED_LOCAL_OPERATIONS' and elapsed<=LIMITS['seconds'],
        'bounded operations/guard gate')
    h.save(OUTPUT/'finalized.json',dict(passed=True,elapsed_seconds=elapsed,
        binary_unchanged=previous.SHA,sidecar_unchanged=previous.SIDECAR,
        performance_acceptance=False,monitoring_receiver_deployed=False,
        long_duration_soak_proved=False,physical_ram_hard_limit_proved=False))
    h.seal(OUTPUT)


if __name__=='__main__':
    if sys.argv[1:]==['prepare']:prepare()
    elif sys.argv[1:]==['execute']:execute()
    elif sys.argv[1:]==['finalize']:finalize()
    else:raise SystemExit('prepare | execute | finalize')
