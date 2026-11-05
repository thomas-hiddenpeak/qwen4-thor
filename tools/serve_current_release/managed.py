"""Qualify the accepted native default with the unchanged v6 managed lifecycle."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_operations_managed import stage as base, test_stage
from tools.serve_service import package as pkg

BASE=ROOT/'.q4t-work/serve-current-managed-20261105'
PLAN,OUT=BASE/'plan.json',BASE/'execution'
OLD=ROOT/'.q4t-work/serve-supervisor-deadlines-20261104'
LIMITS=dict(starts=1,generation_attempts=4,controls=80,seconds=300)
GPU_SECONDS=240
ORIGINAL_CHECK=base.check


def configure():
    base.BASE,base.PLAN,base.OUT=BASE,PLAN,OUT
    base.LIMITS,base.GPU_SECONDS=LIMITS,GPU_SECONDS
    base.HERE='tools/serve_current_release/'
    base.check=check


def sources():
    values=base.sources()
    values['docs/SERVE_CURRENT_CLOSEOUT_2026-11-05.md']=pkg.sha(ROOT/'docs/SERVE_CURRENT_CLOSEOUT_2026-11-05.md')
    return values


def cpu():
    configure();out=BASE/'cpu-01';out.mkdir(parents=True)
    files=sources();log=io.StringIO()
    suite=unittest.defaultTestLoader.loadTestsFromModule(test_stage)
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (out/'tests.log').write_text(log.getvalue())
    pkg.save(out/'result.json',dict(passed=result.wasSuccessful() and not result.skipped,
        tests_run=result.testsRun,sources=files,models=0,http=0))
    pkg.require(result.wasSuccessful() and not result.skipped and files==sources(),'CPU driver gate')
    return dict(passed=True,tests=result.testsRun,models=0,http=0)


def prepare():
    configure()
    pkg.require(not PLAN.exists() and not OUT.exists(),'preserve managed attempt')
    pkg.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit reviewed stage')
    tested=pkg.read(BASE/'cpu-01/result.json')
    pkg.require(tested['passed'] and tested['sources']==sources(),'CPU source binding')
    old=pkg.read(OLD/'plan.json')
    receipt=pkg.read(OLD/'accepted-package.json')['package']
    previous=pkg.verify(receipt['package'],receipt['manifest_sha256'])
    backend=pkg.prepare(ROOT/'build/q4t',ROOT/'build/q4t.release.json',old['model_dir'],
        BASE/'backend-package',port=base.PORT,operations_port=base.OPS_PORT,
        client_policy=old['policy'],restart_limit=0)
    current=pkg.verify(backend['package'],backend['manifest_sha256'])
    changed=sorted(n for n,d in current['files'].items() if previous['files'].get(n)!=d)
    pkg.require(not any(n.startswith('source/') for n in changed) and
        current['settings']==previous['settings'] and current['client_policy']==previous['client_policy'],
        'managed implementation/profile changed')
    pkg.require(set(changed)=={'q4t','q4t.release.json','CMakeCache.txt','acceptance.json',
        'declarations/capabilities.json','declarations/protocol-sources.json'},'unexpected package delta')
    private=BASE/'private';private.mkdir(mode=0o700)
    cert,key=base.client.certificate(private)
    front=pkg.read(old['frontend']);pkg.save(private/'gateway.json',front)
    nginx=pkg.read(base.client.VENDOR)['binary']
    gateways,rejected=[],[]
    for label,value in (('good-1',front),('good-2',front),
        ('missing-operations',{k:v for k,v in front.items() if k!='upstream_operations_port'}),
        ('wrong-operations',dict(front,upstream_operations_port=18134))):
        path=private/(label+'.json');pkg.save(path,value)
        row=base.gateway.prepare(path,nginx,cert,key,BASE/('gateway-'+label),
            old['policy'],trusted_ca=cert)
        (gateways if label.startswith('good') else rejected).append(row)
    outer,_=base.gateway.verify(gateways[0]['package'],gateways[0]['manifest_sha256'])
    preserved=dict(old['preserved'])
    for path in (ROOT/'build/q4t',ROOT/'build/q4t.release.json',OLD/'accepted-package.json',
                 Path(old['policy']),*base.DAYS):
        preserved[str(path)]=pkg.sha(path)
    release=pkg.read(ROOT/'build/q4t.release.json')
    plan=dict(package=backend,previous_package=receipt,package_delta=changed,
        gateways=gateways,rejected_gateways=rejected,gateway=outer['gateway'],
        frontend=str(private/'gateway.json'),policy=old['policy'],
        protocol=release['chat_protocol']['protocol_sha256'],limits=LIMITS,gpu_seconds=GPU_SECONDS,
        sources=sources(),preserved=preserved,runtime=pkg.read(release['tested_source_manifest']),
        model_dir=old['model_dir'],model_inputs=old['model_inputs'],
        cpu_sha256=pkg.sha(BASE/'cpu-01/result.json'),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        planned=dict(models=1,gateway_starts=2,rejected_gateways=2,responses=4,real_tools=2,public_slow_headers=136),
        supervisor_http_upper_bound=4*(GPU_SECONDS//5+3),
        long_run=False,actual_lan=False,persistent_installation=False,performance_acceptance=False)
    pkg.save(PLAN,plan);check()
    return dict(prepared=True,package=backend,package_delta=changed,models=0)


def check():
    plan=ORIGINAL_CHECK()
    previous=pkg.verify(plan['previous_package']['package'],plan['previous_package']['manifest_sha256'])
    current=pkg.verify(plan['package']['package'],plan['package']['manifest_sha256'])
    pkg.require(all(d==previous['files'].get(n) for n,d in current['files'].items()
        if n.startswith('source/')),'supervision source drift')
    return plan


def run():
    configure();plan=check();OUT.mkdir(mode=0o700)
    with base.sup.processes.child_subreaper():
        result=base.Run(plan).execute()
    target=BASE/'postflight';target.mkdir()
    observed=base.inspection.inspect(OUT/'managed-run')
    lock=base.service.guard.common_lock();st=lock.stat()
    passed=(observed['cleanup_observed'] and not observed['listeners'] and
        not observed['gpu']['pids'] and observed['lock']['state']=='AVAILABLE' and
        (st.st_dev,st.st_ino)==(66305,3294067))
    pkg.save(target/'result.json',dict(passed=passed,observation=observed,
        lock=dict(dev=st.st_dev,inode=st.st_ino)))
    pkg.require(passed,'managed postflight')
    return result


configure()
if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('cpu','prepare','run','check'))
    print(json.dumps(globals()[p.parse_args().action]()))
