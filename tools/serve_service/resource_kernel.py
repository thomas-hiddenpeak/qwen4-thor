"""Six bounded owned kernel cases; no model, CUDA or HTTP."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_service import resource_cpu as cpu,resources as r,package as pkg,supervisor as sup,memory

BASE=cpu.BASE
CASES=('normal','high','oom','tasks','drift','death')


def run(*,cases=CASES,output=None,tested_path=None):
    os.umask(0o077)
    tested_path=tested_path or BASE/'cpu-01/result.json'
    tested=pkg.read(tested_path)
    pkg.require(tested['passed'],'CPU gate')
    for name,digest in tested['sources'].items():pkg.require(pkg.sha(ROOT/name)==digest,'CPU source changed')
    output=output or BASE/'kernel-01';output.mkdir(mode=0o700)
    pkg.save(output/'environment.json',dict(memory=memory.read(),uid=os.getuid(),
        manager=r.dependencies(),cgroup=r.membership(os.getpid())))
    pkg.require(memory.read()['available_bytes']>=2*1024**3,'insufficient safe CPU probe headroom')
    records=[]
    with sup.processes.child_subreaper():
        for case in cases:
            folder=output/case;folder.mkdir(mode=0o700)
            started=time.monotonic()
            log=(folder/'worker.log').open('wb')
            child=subprocess.Popen([sys.executable,'-B',ROOT/'tools/serve_service/resource_worker.py',folder,case],
                stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            owner=sup.process_identity(child.pid)
            pkg.save(folder/'launched.json',dict(owner=owner))
            try:
                if case=='death':
                    while not (folder/'payload-held.json').exists() and child.poll() is None and time.monotonic()-started<20:
                        time.sleep(.02)
                    pkg.require((folder/'payload-held.json').exists() and child.poll() is None,'death boundary unavailable')
                    pkg.require(sup.signal_owner(owner,signal.SIGKILL),'owned worker identity changed')
                    code=child.wait(5)
                    pkg.require(code==-signal.SIGKILL,'worker death missing')
                    payload=pkg.read(folder/'payload-ready.json')['owner']
                    until=time.monotonic()+5
                    while True:
                        try:os.waitpid(payload['pid'],os.WNOHANG)
                        except ChildProcessError:pass
                        try:live=sup.process_identity(payload['pid'])==payload
                        except FileNotFoundError:live=False
                        if not live:break
                        pkg.require(time.monotonic()<until,'orphan payload survived parent death');time.sleep(.02)
                    pkg.require(not (folder/'worker-exit.json').exists() and not (folder/'resources-final.json').exists(),
                        'missing controller evidence was fabricated')
                else:
                    code=child.wait(25)
                    state=pkg.read(folder/'status.json')
                    pkg.require(state['attempts']==1 and state['restarts']==0 and state['cleanup_confirmed'],
                        'CPU ownership/restart/cleanup')
                    expected=0 if case in ('normal','high') else 1
                    pkg.require(code==expected,'CPU outcome')
                    final=pkg.read(folder/'resources-final.json')
                    pkg.require(final['payload_removed'] and not final['final']['members'] and
                        final['final']['cgroup.events']['populated']==0,'payload did not clear')
                    if case=='oom':pkg.require(final['violation']=='host_resource_oom' and
                        final['final']['memory.events']['oom_kill']>=2,'group OOM not enforced')
                    if case=='tasks':pkg.require(final['violation']=='host_resource_tasks' and
                        pkg.read(folder/'pids-rejected.json')['errno']==11,'pids not enforced')
                    if case=='high':pkg.require(final['violation'] is None and final['final']['memory.events']['high']>0,
                        'high reclaim not observed')
                    if case=='drift':pkg.require(final['violation']=='host_resource_observation_unavailable',
                        'limit drift did not fail closed')
                departed=r.departed(folder)
                pkg.require(departed['scope_removed'],'scope remained')
                seconds=time.monotonic()-started;pkg.require(seconds<=30,'CPU probe deadline')
                record=dict(case=case,passed=True,returncode=code,seconds=seconds,scope=departed)
                pkg.save(folder/'result.json',record);records.append(record)
            except BaseException as error:
                pkg.save(folder/'failure.json',dict(error=repr(error),returncode=child.poll()))
                if child.poll() is None:
                    sup.signal_owner(owner,signal.SIGTERM)
                    try:child.wait(5)
                    except subprocess.TimeoutExpired:
                        sup.signal_owner(owner,signal.SIGKILL);child.wait(5)
                # Reap adopted CPU descendants after their parent-death
                # signals. Never stop an unrelated unit or reuse a bare PID.
                until=time.monotonic()+5
                while time.monotonic()<until:
                    try:
                        pid,_=os.waitpid(-1,os.WNOHANG)
                    except ChildProcessError:break
                    if pid==0:time.sleep(.02)
                try:pkg.save(folder/'failure-cleanup.json',r.departed(folder))
                except Exception as cleanup:
                    pkg.save(folder/'failure-cleanup.json',dict(error=repr(cleanup),scope_removed=False))
                raise
            finally:log.close()
    result=dict(passed=True,cases=records,models_started=0,listeners_started=0,gpu_started=0,
        source_files=tested['sources'],cpu_sha256=pkg.sha(tested_path))
    pkg.save(output/'result.json',result)
    print(json.dumps({k:result[k] for k in ('passed','cases','models_started','gpu_started')}))


if __name__=='__main__':run()
