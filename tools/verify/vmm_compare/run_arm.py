"""One actual-model arm; the caller supplies a full-lifetime GPU lease."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'tools/evalscope'),str(ROOT/'tools/verify/inference_metadata')]
import diagnose_warm_start as observer
from run_budget_experiment import run_owned_runner
from cache_start import prepare,check_vector
from file_cache import model_files,observe_files
BASE=ROOT/'.q4t-work/vmm-compare-20261020'
OLD=ROOT/'.q4t-work/offload-foundation-20261020/scope-integration-v5/run'
MODEL=Path.home()/'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'


def main(arm, attempt=1, repair=False, limit_gib=64):
    mode='runtime' if arm=='capture' else arm
    stage='capture-v1' if arm=='capture' else (f'bounded-{arm}-v1' if limit_gib==48 else f'hot-{arm}-v{attempt}')
    out=BASE/stage/'run';out.mkdir(parents=True,exist_ok=False)
    def save(name,data):(out/name).write_text(json.dumps(data,indent=2)+'\n')
    identity=json.loads((BASE/'build.json').read_text())
    binary=BASE/mode/'q4t'
    if hashlib.sha256(binary.read_bytes()).hexdigest()!=identity['arms'][mode]['binary']:
        raise RuntimeError('binary identity changed')
    shutil.copy2(BASE/'build.json',out/'build.json')
    sources=list(Path(__file__).parent.glob('*'))+list((ROOT/'tools/evalscope').glob('*.py'))
    sources += [Path(observer.__file__),ROOT/'tools/verify/vmm/block.h']
    manifest={}
    for path in sources:
        if not path.is_file():continue
        dest=out/'source'/path.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,dest)
        manifest[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    save('sources.json',manifest)
    shutil.copytree(OLD/'fixtures',out/'fixtures')
    for name in ('hot.json','reference.json'):shutil.copy2(OLD/name,out/name)
    command=[a.replace(str(OLD),str(out)) for a in json.loads((OLD/'normal-command.json').read_text()) if a!='--clear-model-cache']
    command[command.index('--binary')+1]=str(binary)
    command[command.index('--output')+1]=str(out/'normal')
    save('plan.json',dict(arm=arm,binary=identity['arms'][mode]['binary'],command=command,
        warm=arm!='capture',attempt=attempt,cache_repair=repair,cache_limit_gib=limit_gib,loads=1,http_requests=1,performance_acceptance=False))
    state=dict(status='RUNNING',started=time.time());monitor=None
    try:
        if arm=='capture':
            for test_mode in ('runtime','vmm'):
                with (out/f'contract-{test_mode}.log').open('x') as log:
                    subprocess.run([str(BASE/test_mode/'contract')],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=30)
        gate=prepare(MODEL,out,arm!='capture',repair=repair,limit_gib=limit_gib);save('cache-gate.json',gate)
        monitor=subprocess.Popen(['sudo','-n',sys.executable,'-B',str(Path(__file__).resolve()),'--arm',arm,'--attempt',str(attempt),'--limit-gib',str(limit_gib),'--observe'])
        # A second non-faulting check immediately before launching the runner.
        final=observe_files(model_files(MODEL));save('cache-launch.json',final)
        check_vector(final,json.loads((out/'cache-expected.json').read_text()))
        with (out/'normal.log').open('x') as log:
            rc=run_owned_runner(command,cwd=ROOT,
                env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'},
                log=log,timeout=1260,evidence=out/'normal-process.json')
        state.update(status='COMPLETED' if rc==0 else 'COMMAND_FAILED',returncode=rc)
        return rc
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED',error=repr(error));raise
    finally:
        (out/'stop-observer').touch()
        if monitor is not None:monitor.wait(timeout=5)
        state['finished']=time.time();save('state.json',state)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--arm',choices=['capture','runtime','vmm'],required=True);p.add_argument('--observe',action='store_true');p.add_argument('--attempt',type=int,choices=(1,2),default=1);p.add_argument('--cache-repair',action='store_true');p.add_argument('--limit-gib',type=int,choices=(48,64),default=64);args=p.parse_args()
    if args.observe:
        mode='runtime' if args.arm=='capture' else args.arm
        observer.BINARY=BASE/mode/'q4t'
        stage='capture-v1' if args.arm=='capture' else (f'bounded-{args.arm}-v1' if args.limit_gib==48 else f'hot-{args.arm}-v{args.attempt}')
        sys.exit(observer.observe(BASE/stage/'run'))
    sys.exit(main(args.arm,args.attempt,args.cache_repair,args.limit_gib))
