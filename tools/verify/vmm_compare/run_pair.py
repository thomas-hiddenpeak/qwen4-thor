"""Hold one external lease across two predeclared independently guarded arms."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'tools/evalscope'))
from gpu_exclusive import common_lock
from isolated_scope import scope_properties,stop_owned_scope
BASE=ROOT/'.q4t-work/vmm-compare-20261020'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def save(path,data):path.write_text(json.dumps(data,indent=2)+'\n')

def main(limit_gib=64):
    # Refuse comparison without the full capture and actual-link contracts.
    capture=BASE/'capture-v1'
    for rel,value in json.loads((capture/'sealed-files.json').read_text()).items():
        assert sha(capture/rel)==value,rel
    assert json.loads((capture/'seal.json').read_text())['guard_status']=='COMPLETED'
    assert json.loads((capture/'run/normal/http/exit.json').read_text())['http_output_checks_passed']
    for name in ('runtime','vmm'):
        assert 'LINK_CONTRACT PASS' in (capture/f'run/contract-{name}.log').read_text()
    out=BASE/('bounded-pair-v1' if limit_gib==48 else 'pair-v1');out.mkdir(exist_ok=False)
    state=dict(started=time.time(),arms=[],status='RUNNING',order=['runtime','vmm'],cache_limit_gib=limit_gib)
    save(out/'state.json',state)
    lock_path=common_lock();lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state['lease_acquired']=time.time();save(out/'state.json',state)
        try:
            for arm in ('runtime','vmm'):
                stage=BASE/(f'bounded-{arm}-v1' if limit_gib==48 else f'hot-{arm}-v1');stage.mkdir(exist_ok=False)
                command=[sys.executable,'-B',str(ROOT/'tools/evalscope/gpu_exclusive.py'),
                    '--output',str(stage/'lease'),'--lease-fd',str(lock.fileno()),'--',
                    sys.executable,'-B',str(Path(__file__).with_name('run_arm.py')),'--arm',arm,'--limit-gib',str(limit_gib)]
                started=time.time()
                with (stage/'driver.log').open('x') as log:
                    result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,
                                          cwd=ROOT,pass_fds=(lock.fileno(),))
                lease=json.loads((stage/'lease/exit.json').read_text())
                if 'cleanup_error' in lease:raise RuntimeError('guard cleanup failed')
                # Drain only a scope whose random ownership token is ours.
                spec=stage/'run/normal/http/isolation/spec.json'
                if spec.exists():
                    owned=json.loads(spec.read_text())
                    launch=json.loads(spec.with_name('launch.json').read_text())
                    unit=launch['unit']
                    props=scope_properties(unit)
                    if props.get('LoadState')!='not-found':
                        cleanup=stop_owned_scope(unit,owned['token'])
                        save(stage/'supervisor-cleanup.json',cleanup)
                    if scope_properties(unit).get('LoadState')!='not-found':
                        raise RuntimeError('owned scope remains')
                seal=dict(guard_exit=result.returncode,guard_status=lease['status'],
                          started=started,finished=time.time(),external_lease=True,
                          scope='sealed after own guard and scope cleanup')
                save(stage/'seal.json',seal)
                save(stage/'sealed-files.json',{str(p.relative_to(stage)):sha(p)
                    for p in stage.rglob('*') if p.is_file()})
                state['arms'].append(dict(arm=arm,**seal));save(out/'state.json',state)
                # A new arm is not a retry of the failed arm. Preconditions and
                # NVML preflight are repeated by its guard; no failure bypass.
                if lease['status'] not in ('COMPLETED','ENV_ABORT'):
                    raise RuntimeError('non-environment failure; inspect before continuing')
                if lease['status']=='ENV_ABORT' and 'NVML process query' not in lease.get('reason',''):
                    raise RuntimeError('non-NVML environment abort; comparison stops')
            state['status']='COMPLETED_BOUNDED_COMPARISON'
        except BaseException as error:
            state.update(status='STOPPED',error=repr(error));raise
        finally:
            state['lease_release_at']=time.time();save(out/'state.json',state)
            lock.seek(0);lock.truncate();lock.write(json.dumps(dict(state='released',output=str(out)))+'\n');lock.flush()
    return 0

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--limit-gib',type=int,choices=(48,64),default=64);a=p.parse_args()
    raise SystemExit(main(a.limit_gib))
