"""Preserve native-fixture timeout and retest its group with the same model ELF."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_operations_listener import cpu,stage as s

ORIGINAL_PACKAGE=s.PACKAGE
FAILED=s.BASE/'execution-02'
PACKAGE,OUTPUT=s.BASE/'package-native-fix',s.BASE/'execution-03'
FIXTURE='tools/serve_reliability/http_lifecycle_test.cpp'
SOURCES=('tools/serve_operations_listener/native_fix.py',
         'tools/serve_operations_listener/test_native_fix.py')


def prior_gate():
    value=cpu.read(FAILED/'business/result.json')
    guard=cpu.read(FAILED/'lease/exit.json')
    cpu.require(value['status']=='FAILED' and value['counts']==dict(starts=0,generation_attempts=0,controls=0) and
                value['error']=="TimeoutError('owned child exceeded deadline')" and
                guard['status']=='COMMAND_FAILED' and guard['child_returncode']==1,
                'only the zero-model native fixture timeout is eligible')
    native=cpu.read(FAILED/'business/native/identity.json')
    cpu.require(not Path(f"/proc/{native['pid']}").exists(),'old native child still exists')
    cpu.require(not (FAILED/'release').exists(),'no release may have been installed')


def prepare():
    cpu.require(not PACKAGE.exists() and not OUTPUT.exists(),'preserve prior repair attempt')
    cpu.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit fixture repair first')
    prior_gate();s.h.verify_seal(ORIGINAL_PACKAGE)
    previous=cpu.read(ORIGINAL_PACKAGE/'sources.json')
    delta={n for n,d in previous.items() if cpu.sha(ROOT/n)!=d}
    cpu.require(delta=={FIXTURE},'unexpected source changes')
    built=cpu.read(s.BASE/'build-native-fix/result.json')
    cpu.require(built['passed'] and cpu.sha(cpu.ENGINE/'q4t')==built['production_sha256'] and
                cpu.sha(cpu.ENGINE/'q4t_http_lifecycle_tests')==built['native_sha256'],'native build gate')
    tested=cpu.read(s.BASE/'driver-native-fix/result.json');cpu.require(tested['passed'],'repair driver gate')
    for name,digest in tested['sources'].items():cpu.require(cpu.sha(ROOT/name)==digest,'repair driver source changed')
    s.h.seal(FAILED)
    PACKAGE.mkdir()
    changed={'sources.json','plan.json','sealed-files.json','candidate/q4t_http_lifecycle_tests'}
    for path in sorted(ORIGINAL_PACKAGE.rglob('*')):
        rel=path.relative_to(ORIGINAL_PACKAGE)
        if path.is_file() and str(rel) not in changed:
            target=PACKAGE/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
    s.h.copy_checked(s.h.identity(cpu.ENGINE/'q4t_http_lifecycle_tests'),PACKAGE/'candidate/q4t_http_lifecycle_tests')
    previous[FIXTURE]=cpu.sha(ROOT/FIXTURE)
    previous.update({n:cpu.sha(ROOT/n) for n in SOURCES})
    cpu.save(PACKAGE/'sources.json',previous)
    plan=cpu.read(ORIGINAL_PACKAGE/'plan.json')
    cpu.require(plan['candidate_sha256']==built['production_sha256'],'production ELF changed')
    plan.update(candidate=str(PACKAGE/'candidate/q4t'),binary=str(PACKAGE/'candidate/q4t'),
                original_source_commit=plan['source_commit'],source_commit=subprocess.check_output(
                    ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    for path in (FAILED/'sealed-files.json',s.BASE/'build-native-fix/result.json',s.BASE/'driver-native-fix/result.json'):
        plan['local'][str(path)]=cpu.sha(path)
    plan['native_repair']=dict(original_package=s.h.identity(ORIGINAL_PACKAGE/'sealed-files.json'),
        original_failure=s.h.identity(FAILED/'sealed-files.json'),changed=[FIXTURE],
        native_sha256=built['native_sha256'],prior_models=0,prior_generations=0,
        prior_native_result='incomplete timeout; no per-case PASS inference')
    cpu.save(PACKAGE/'plan.json',plan);s.h.seal(PACKAGE)
    return dict(prepared=True,production_unchanged=True,limits=plan['limits'])


def bind():
    prior_gate();s.h.verify_seal(FAILED)
    s.PACKAGE,s.OUTPUT=PACKAGE,OUTPUT;s.check()


def execute():bind();s.execute()

def finalize():bind();s.finalize()


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','execute','finalize'))
    print(globals()[p.parse_args().action]() or {'passed':True})
