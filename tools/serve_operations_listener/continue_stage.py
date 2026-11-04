"""Continue an unchanged short after the pre-work guard argv failure only."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_operations_listener import cpu,stage as s

ORIGINAL=s.BASE/'execution'
OUTPUT=s.BASE/'execution-02'
PLAN=s.BASE/'continuation-plan.json'
SOURCES=('tools/serve_operations_listener/continue_stage.py',
         'tools/serve_operations_listener/test_continue.py')


def guard_command():
    return [str(Path(sys.executable).resolve()),'-B',str(ROOT/'tools/evalscope/gpu_exclusive.py'),
        '--output',str(OUTPUT/'lease'),'--',str(Path(sys.executable).resolve()),'-B',
        str(ROOT/SOURCES[0]),'execute']


def original_gate():
    value=cpu.read(ORIGINAL/'lease/exit.json')
    cpu.require(value['status']=='COMMAND_FAILED' and value['child_returncode']==1 and
                not (ORIGINAL/'begin.json').exists() and not (ORIGINAL/'business').exists(),
                'continuation only permits pre-work failure with zero starts/requests')
    return value


def prepare():
    cpu.require(not PLAN.exists() and not OUTPUT.exists(),'no automatic continuation retry')
    cpu.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit continuation first')
    original_gate();s.check()
    tested=cpu.read(s.BASE/'driver-continuation/result.json');cpu.require(tested['passed'],'continuation CPU gate')
    for name,digest in tested['sources'].items():cpu.require(cpu.sha(ROOT/name)==digest,'continuation source changed')
    s.h.seal(ORIGINAL)
    cpu.save(PLAN,dict(reason='absolute guard argv required; original command failed before begin',
        original=s.h.identity(ORIGINAL/'sealed-files.json'),package=s.h.identity(s.PACKAGE/'sealed-files.json'),
        sources={n:cpu.sha(ROOT/n) for n in SOURCES},command=guard_command(),
        limits=s.LIMITS,planned=s.PLANNED,prior_models=0,prior_http=0,
        commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()))
    return dict(prepared=True,prior_models=0,prior_http=0,limits=s.LIMITS)


def bind():
    plan=cpu.read(PLAN);original_gate();s.h.verify_seal(ORIGINAL)
    s.h.check_identity(plan['original']);s.h.check_identity(plan['package'])
    for name,digest in plan['sources'].items():cpu.require(cpu.sha(ROOT/name)==digest,'continuation source drift')
    cpu.require(plan['limits']==s.LIMITS and plan['planned']==s.PLANNED and plan['command']==guard_command(),
                'continuation scope drift')
    s.OUTPUT=OUTPUT;s.check()


def execute():bind();s.execute()

def finalize():bind();s.finalize()


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','execute','finalize'))
    print(globals()[p.parse_args().action]() or {'passed':True})
