"""Finish unexecuted scenarios after the original order-only driver failure."""
import argparse
import ast
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
from tools.serve_host import stage as s,continuation as c
from tools.serve_service import package as pkg

ORIGINAL=c.ORIGINAL
PREVIOUS=c.BASE
BASE=ORIGINAL/'projection-corrected'
CHANGED=('tools/serve_host/stage.py','tools/serve_host/report.py')
EXTRA=('tools/serve_host/projection_fix.py','tools/serve_host/test_projection_fix.py')
LIMITS=dict(s.LIMITS,seconds=180)
GPU_WINDOW=150


def configure():
    s.BASE=BASE;s.PLAN=BASE/'plan.json';s.OUT=BASE/'execution';s.TASKS=BASE/'tasks'
    s.LIMITS=LIMITS;s.GPU_WINDOW=GPU_WINDOW;s.configure()


def method_contract(raw):
    tree=ast.parse(raw)
    cls=next(v for v in tree.body if isinstance(v,ast.ClassDef) and v.name=='Run')
    return {v.name:ast.dump(v,include_attributes=False) for v in cls.body if isinstance(v,ast.FunctionDef) and v.name!='projection'}


def cpu():
    from tools.serve_host import test_projection_fix
    output=ORIGINAL/'cpu-projection-fix';output.mkdir(mode=0o700)
    sources={name:pkg.sha(ROOT/name) for name in (*CHANGED,*EXTRA)}
    for name in sources:
        target=output/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ROOT/name).read_bytes())
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(test_projection_fix))
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,errors=len(result.errors),
        failures=len(result.failures),sources=sources,models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end='');pkg.require(result.wasSuccessful(),'projection driver correction')
    return dict(passed=True,tests=result.testsRun)


def prepare():
    pkg.require(not BASE.exists(),'preserve corrective attempt')
    old=pkg.read(PREVIOUS/'plan.json');failed=pkg.read(PREVIOUS/'execution/result.json')
    pkg.require(failed['status']=='FAILED' and failed['error']=="RuntimeError('deadline: consistent projection')" and
        failed['cleanup_confirmed'] and failed['counts']['generations']==0 and not (PREVIOUS/'tasks').exists() and
        pkg.read(PREVIOUS/'execution/custody/exit.json')['status']=='COMPLETED','original projection failure and cleanup')
    for name,checksum in old['source_files'].items():
        if name not in CHANGED:pkg.require(pkg.sha(ROOT/name)==checksum,'unaffected source changed')
    tested=pkg.read(ORIGINAL/'cpu-projection-fix/result.json')
    pkg.require(tested['passed'] and tested['tests_run']==3,'targeted driver gate')
    for name,checksum in tested['sources'].items():
        pkg.require(pkg.sha(ROOT/name)==checksum and subprocess.check_output(['git','show','HEAD:'+name],cwd=ROOT)==
            (ROOT/name).read_bytes(),'commit tested driver correction')
    BASE.mkdir(mode=0o700)
    for name in (*c.RECEIPTS,'cpu-continuation'):shutil.copytree(PREVIOUS/name,BASE/name)
    shutil.copytree(ORIGINAL/'cpu-projection-fix',BASE/'cpu-projection-fix')
    for path in (ORIGINAL/'cpu-fixtures').glob('*/observer/syntax-private.log'):
        target=BASE/path.relative_to(ORIGINAL);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target);target.chmod(0o600)
    shutil.copytree(PREVIOUS/'original-before-gpu',BASE/'original-before-gpu')
    preserved=BASE/'original-projection-failure';preserved.mkdir(mode=0o700)
    for name in ('plan.json','live-command.log'):
        shutil.copyfile(PREVIOUS/name,preserved/name);(preserved/name).chmod(0o600)
    shutil.copytree(PREVIOUS/'execution',preserved/'execution')
    obs=preserved/'observer';obs.mkdir(mode=0o700)
    for name in ('policy.json','status.json','metrics.prom','binding.json','observer.json','exporter.json','observer-terminal.json'):
        shutil.copyfile(PREVIOUS/'observer'/name,obs/name);(obs/name).chmod(0o600)
    observer=s.o.prepare(old['package']['package'],old['package']['manifest_sha256'],BASE/'execution/managed-run',
        'thor-host-short',Path(old['gateways'][0]['package'])/'nginx',s.PORTS['host'],BASE/'observer')
    s.n.init_state(old['notification']['package'],old['notification']['manifest_sha256'],BASE/'monitor-state')
    value=dict(old,observer=observer,limits=LIMITS,gpu_window_seconds=GPU_WINDOW,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        source_files={**old['source_files'],**tested['sources']},
        receipts={**old['receipts'],'cpu-projection-fix':pkg.sha(BASE/'cpu-projection-fix/result.json')},
        original_projection={str(p.relative_to(PREVIOUS)):pkg.sha(p) for p in (PREVIOUS/'execution').rglob('*') if p.is_file()},
        original_projection_plan_sha256=pkg.sha(PREVIOUS/'plan.json'),
        cumulative=dict(model_starts=2,complete_responses=4,real_tools=2,gpu_seconds=210,seconds=240))
    pkg.save(BASE/'plan.json',value);configure();s.check()
    return dict(prepared=True,models_started=0,production_changed=False,gpu_window_seconds=GPU_WINDOW)


def report(action):
    configure()
    from tools.serve_host import report as r
    original_evaluate=r.evaluate
    def evaluate(data):
        result=original_evaluate(data);read=lambda name:json.loads(data[name]);plan=read('plan.json')
        c.original_gate(read('original-before-gpu/execution/result.json'))
        first=read('original-projection-failure/execution/result.json')
        pkg.require(first['status']=='FAILED' and first['error']=="RuntimeError('deadline: consistent projection')" and
            first['cleanup_confirmed'] and first['counts']['generations']==0 and
            r.sha(data['original-projection-failure/plan.json'])==plan['original_projection_plan_sha256'],
            'original projection failure')
        for name,checksum in plan['original_projection'].items():
            pkg.require(r.sha(data['original-projection-failure/'+name])==checksum,'original projection bytes')
        for folder,count in (('cpu-continuation',3),('cpu-projection-fix',3)):
            value=read(folder+'/result.json')
            pkg.require(value['passed'] and value['tests_run']==count and value['errors']==value['failures']==0 and
                r.sha(data[folder+'/result.json'])==plan['receipts'][folder],'corrective CPU result')
            for name,checksum in value['sources'].items():
                pkg.require(r.sha(data[folder+'/source/'+name])==plan['source_files'][name]==checksum,'corrected source identity')
        old_projection=read('original-projection-failure/observer/status.json')
        old_policy=data['original-projection-failure/observer/policy.json']
        old_wire=data['original-projection-failure/observer/metrics.prom']
        pkg.require(s.projection_matches(old_wire,old_projection,'thor-host-short',r.sha(old_policy)),'original metrics were valid')
        cumulative_gpu=result['gpu_seconds']+first['gpu_finished']-first['gpu_started']
        cumulative_seconds=result['seconds']+first['seconds']+read('original-before-gpu/execution/result.json')['seconds']
        cumulative_controls=result['controls']+first['counts']['controls']
        pkg.require(cumulative_gpu<=210 and cumulative_seconds<=240 and cumulative_controls<=64,'cumulative corrective bound')
        result.update(original_pre_gpu_failure_preserved=True,original_projection_failure_preserved=True,
            continuation_driver_tests=3,projection_driver_tests=3,cumulative_model_starts=2,
            cumulative_gpu_seconds=cumulative_gpu,cumulative_seconds=cumulative_seconds,cumulative_controls=cumulative_controls)
        return result
    r.evaluate=evaluate
    try:return getattr(r,action)()
    finally:r.evaluate=original_evaluate


if __name__=='__main__':
    os.umask(0o077);parser=argparse.ArgumentParser();parser.add_argument('action',choices=('cpu','prepare','check','run','postflight','archive','verify'))
    action=parser.parse_args().action
    if action in ('postflight','archive','verify'):value=report(action)
    elif action=='check':configure();s.check();value=dict(checked=True,started=False)
    elif action=='run':
        configure();plan=s.check();pkg.require(not s.OUT.exists(),'preserve corrective run')
        with s.sup.processes.child_subreaper():value=s.Run(plan).execute()
    else:value=globals()[action]()
    print(json.dumps(value))
