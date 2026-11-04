"""Correct the pre-GPU retention fixture in a fresh, non-overwriting attempt."""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_host import stage as s
from tools.serve_service import package as pkg

ORIGINAL=s.BASE
BASE=ORIGINAL/'continuation'
EXTRA=('tools/serve_host/continuation.py','tools/serve_host/test_continuation.py')
STORE_POLICY=dict(max_bytes=128*1024**2,max_generations=1,max_age_days=14)
RECEIPTS=('cpu-01','cpu-fix','cpu-fix-02','cpu-runtime','cpu-runtime-fix','cpu-driver','cpu-driver-02')


def original_gate(value):
    pkg.require(value['status']=='FAILED' and value['error']=="RuntimeError('invalid retention policy')" and
        value['gpu_started'] is None and value['gpu_finished'] is None and
        value['counts']==dict(generations=0,negatives=0,controls=0),'continuation requires a pre-GPU original failure')


def configure():
    s.BASE=BASE;s.PLAN=BASE/'plan.json';s.OUT=BASE/'execution';s.TASKS=BASE/'tasks';s.configure()


def cpu():
    from tools.serve_host import test_continuation
    os.umask(0o077);output=ORIGINAL/'cpu-continuation';output.mkdir(mode=0o700)
    sources={name:pkg.sha(ROOT/name) for name in EXTRA}
    for name in sources:
        dest=output/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/name).read_bytes())
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(test_continuation))
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,errors=len(result.errors),
        failures=len(result.failures),sources=sources,models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end='');pkg.require(result.wasSuccessful(),'targeted continuation CPU')
    return dict(passed=True,tests=result.testsRun)


def prepare():
    pkg.require(not BASE.exists(),'preserve continuation attempt')
    s.cpu_gate();old=pkg.read(ORIGINAL/'plan.json');original_gate(pkg.read(ORIGINAL/'execution/result.json'))
    for path in ('owned-generations','tasks','observer/observer.json','observer/binding.json','execution/managed-run'):
        pkg.require(not (ORIGINAL/path).exists(),'original attempt started work')
    for name,checksum in old['source_files'].items():pkg.require(pkg.sha(ROOT/name)==checksum,'original implementation changed')
    tested=pkg.read(ORIGINAL/'cpu-continuation/result.json')
    pkg.require(tested['passed'] and tested['tests_run']==3,'continuation CPU gate')
    for name,checksum in tested['sources'].items():
        pkg.require(pkg.sha(ROOT/name)==checksum and subprocess.check_output(['git','show','HEAD:'+name],cwd=ROOT)==
            (ROOT/name).read_bytes(),'commit tested continuation')
    BASE.mkdir(mode=0o700)
    for name in (*RECEIPTS,'cpu-continuation'):shutil.copytree(ORIGINAL/name,BASE/name)
    preserved=BASE/'original-before-gpu';preserved.mkdir(mode=0o700)
    for name in ('plan.json','live-command.log'):
        shutil.copyfile(ORIGINAL/name,preserved/name);(preserved/name).chmod(0o600)
    shutil.copytree(ORIGINAL/'execution',preserved/'execution')
    # Only the run path changes. Backend, gateway, notification rules, keys,
    # model budgets and all production source identities remain unchanged.
    observer=s.o.prepare(old['package']['package'],old['package']['manifest_sha256'],BASE/'execution/managed-run',
        'thor-host-short',Path(old['gateways'][0]['package'])/'nginx',s.PORTS['host'],BASE/'observer')
    s.n.init_state(old['notification']['package'],old['notification']['manifest_sha256'],BASE/'monitor-state')
    # Keep original notification package and its fixed loopback endpoints;
    # archive its non-secret configuration alongside the new attempt.
    copy=BASE/'notification';copy.mkdir(mode=0o700)
    for name in ('manifest.json','prometheus.json','alertmanager.json','notify.rules.json',
                 'validation-0-private.log','validation-1-private.log'):
        shutil.copyfile(Path(old['notification']['package'])/name,copy/name);(copy/name).chmod(0o600)
    value=dict(old,observer=observer,store_policy=STORE_POLICY,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        source_files={**old['source_files'],**tested['sources']},
        receipts={**old['receipts'],'cpu-continuation':pkg.sha(BASE/'cpu-continuation/result.json')},
        original_pre_gpu={str(p.relative_to(ORIGINAL)):pkg.sha(p) for p in (ORIGINAL/'execution').rglob('*') if p.is_file()},
        original_plan_sha256=pkg.sha(ORIGINAL/'plan.json'))
    pkg.save(BASE/'plan.json',value);configure();s.check()
    return dict(prepared=True,models_started=0,listeners_started=0,store_policy=STORE_POLICY)


def check():
    configure();value=s.check()
    pkg.require(value['store_policy']==STORE_POLICY and pkg.sha(ORIGINAL/'plan.json')==value['original_plan_sha256'],
                'original plan or corrected capacity changed')
    for name,checksum in value['original_pre_gpu'].items():pkg.require(pkg.sha(ORIGINAL/name)==checksum,'original failure changed')
    return value


def report(action):
    configure()
    # Import after configuring: the original reporter's paths consistently
    # select this fresh attempt without rewriting any original evidence.
    from tools.serve_host import report as r
    original_collect,original_evaluate=r.collect,r.evaluate
    def collect():
        # r.collect expects colocated dependency identities; reuse immutable
        # originals through an explicit collection here rather than symlinks.
        paths=[BASE/'plan.json',BASE/'live-command.log']
        for folder in (*RECEIPTS,'cpu-continuation','execution','owned-generations','tasks','postflight','original-before-gpu'):
            paths.extend(p for p in (BASE/folder).rglob('*') if p.is_file())
        paths.extend(BASE/'observer'/name for name in ('policy.json','nginx.conf','syntax-private.log','observer.json',
            'binding.json','status.json','metrics.prom','exporter.json','exporter.log','observer-terminal.json'))
        paths.extend(p for p in (BASE/'notification').iterdir() if p.is_file())
        data={str(p.relative_to(BASE)):p.read_bytes() for p in sorted(set(paths)) if p.is_file() and not p.is_symlink()}
        for path in (ORIGINAL/'cpu-fixtures').glob('*/observer/syntax-private.log'):
            data[str(path.relative_to(ORIGINAL))]=path.read_bytes()
        for path in (ORIGINAL/'runtime-fixtures').glob('*/observer-terminal.json'):
            data[str(path.relative_to(ORIGINAL))]=path.read_bytes()
        policy=pkg.read(s.retained.prior.BASE/'model-private/policy-private.json')
        plan=pkg.read(BASE/'plan.json');notification=Path(plan['notification']['package'])
        secrets=[v['api_key'] for v in policy['clients']]+[pkg.read(s.retained.prior.BASE/'model-private/clients.json')['gateway_token'],
            (notification/'token-private').read_text().strip()]
        for name,raw in data.items():
            pkg.require(all(v.encode() not in raw for v in secrets) and not any(b'-----BEGIN '+k+b'PRIVATE KEY-----' in raw
                for k in (b'',b'RSA ',b'EC ',b'ENCRYPTED ')),'secret in evidence: '+name)
        binaries=[Path(plan['package']['package'])/'q4t',Path(plan['gateways'][0]['package'])/'nginx',BASE/'observer/nginx',
                  *(notification/name for name in s.n.BINARIES)]
        local={str(p):dict(bytes=p.stat().st_size,sha256=pkg.sha(p)) for p in binaries}
        return data,local
    def evaluate(data):
        result=original_evaluate(data);read=lambda name:json.loads(data[name])
        original_gate(read('original-before-gpu/execution/result.json'))
        plan=read('plan.json');pkg.require(r.sha(data['original-before-gpu/plan.json'])==plan['original_plan_sha256'],
                                         'original frozen plan')
        for name,checksum in plan['original_pre_gpu'].items():
            pkg.require(r.sha(data['original-before-gpu/'+name])==checksum,'original pre-GPU bytes')
        value=read('cpu-continuation/result.json')
        pkg.require(value['passed'] and value['tests_run']==3 and value['errors']==value['failures']==0 and
            r.sha(data['cpu-continuation/result.json'])==plan['receipts']['cpu-continuation'],'continuation CPU evidence')
        for name,checksum in value['sources'].items():
            pkg.require(r.sha(data['cpu-continuation/source/'+name])==plan['source_files'][name]==checksum,'continuation sources')
        result.update(original_pre_gpu_failure_preserved=True,continuation_driver_tests=3,
            original_pre_gpu_seconds=read('original-before-gpu/execution/result.json')['seconds'])
        return result
    r.collect,r.evaluate=collect,evaluate
    try:return getattr(r,action)()
    finally:r.collect,r.evaluate=original_collect,original_evaluate


if __name__=='__main__':
    os.umask(0o077);parser=argparse.ArgumentParser();parser.add_argument('action',choices=('cpu','prepare','check','run','postflight','archive','verify'))
    action=parser.parse_args().action
    if action in ('postflight','archive','verify'):value=report(action)
    elif action=='check':check();value=dict(checked=True,started=False)
    elif action=='run':
        plan=check();pkg.require(not s.OUT.exists(),'preserve continuation run')
        with s.sup.processes.child_subreaper():value=s.Run(plan).execute()
    else:value=globals()[action]()
    print(json.dumps(value))
