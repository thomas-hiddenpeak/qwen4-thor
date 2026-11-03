"""Opt-in managed generations with private storage reservations; no autostart."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_retention import store
from tools.serve_service import package as pkg, supervisor as sup, inspection
from tools.serve_gateway import managed as gateway

require=store.require


def owner_live(owner):
    try:return sup.process_identity(owner['pid'])==owner
    except (FileNotFoundError,ProcessLookupError):return False


def binding(kind,package,digest,backend_run=None):
    package=Path(package).resolve(strict=True)
    if kind=='backend':
        manifest=pkg.verify(package,digest)
        require(manifest['version'] in (1,2,3,4) and backend_run is None,'backend contract')
        entry=package/'source/tools/serve_service/service.py'
    elif kind=='gateway':
        manifest,_=gateway.verify(package,digest)
        require(backend_run is not None,'gateway requires explicit backend generation')
        backend_run=Path(backend_run).resolve(strict=True)
        entry=package/'source/tools/serve_gateway/managed.py'
    else:raise ValueError('unknown service kind')
    return dict(kind=kind,package=str(package),manifest_sha256=digest,
        entry=str(entry),entry_sha256=pkg.sha(entry),python=manifest['python'],
        backend_run=None if backend_run is None else str(backend_run))


def command(bound,output):
    argv=[bound['python']['path'],'-I','-B',bound['entry'],'run',
        '--package',bound['package'],'--manifest-sha256',bound['manifest_sha256'],
        '--output',str(output)]
    if bound['kind']=='gateway':argv+=['--backend-run',bound['backend_run']]
    return argv


def formal_stop(folder):
    record=store.read(folder/'reservation.json');bound=record['binding']
    run=folder/'run'
    if not (run/'controller.json').exists():return None
    require(pkg.sha(bound['entry'])==bound['entry_sha256'] and
            pkg.sha(bound['python']['path'])==bound['python']['sha256'],'pinned stop entry changed')
    result=subprocess.run([bound['python']['path'],'-I','-B',bound['entry'],'stop','--run',str(run)],
        capture_output=True,text=True,timeout=15)
    require(len(result.stdout)+len(result.stderr)<65536,'stop response bound')
    require(result.returncode==0,'formal stop rejected')
    return dict(returncode=result.returncode,stdout=result.stdout,stderr=result.stderr)


def cleanup_proof(folder):
    record=store.read(folder/'reservation.json');kind=record['kind'];run=folder/'run'
    control=pkg.read(run/'controller.json');terminal=pkg.read(run/'terminal.json')
    require(control['package']==record['binding']['package'] and
            control['manifest_sha256']==record['binding']['manifest_sha256'] and
            terminal['cleanup_confirmed'] is True and terminal['state'] in ('STOPPED','FAILED'),
            'terminal does not prove managed cleanup')
    owners=[control['owner']]
    if kind=='backend':
        state=pkg.read(run/'status.json');owners.append(state['owner'])
        require(state['attempts']<=4 and state['cleanup_confirmed'],'backend cleanup incomplete')
        for index in range(1,state['attempts']+1):owners.append(pkg.read(run/f'attempt-{index}.json')['owner'])
        require(control.get('observation_context')==inspection.current_context(),
                'backend observation namespace changed')
        require(terminal['guard']==pkg.read(run/'lease/exit.json') and
                'cleanup_error' not in terminal['guard'],'guard cleanup incomplete')
    else:
        if terminal.get('nginx'):owners.append(terminal['nginx'])
        require(not terminal['backend_stopped_by_gateway'],'gateway altered backend')
    for owner in owners:
        require(not owner_live(owner),'managed owner is still live')
        # A recycled group can conservatively prevent sealing; never signal it.
        require(not sup.processes.group_members(owner['pid']),'managed process group remains')
    return dict(cleanup_confirmed=True,terminal_state=terminal['state'],owners=owners,
        terminal_sha256=pkg.sha(run/'terminal.json'),observation_context=inspection.current_context(),
        historical_failure_preserved=terminal['state']=='FAILED')


def run(root,bound):
    owner=sup.process_identity(os.getpid())
    folder,claim_fd=store.reserve(root,bound['kind'],bound,owner)
    stopped=threading.Event()
    previous={}
    child=logger=None;result=dict(state='FAILED',generation=str(folder),cleanup_confirmed=False,sealed=False)
    failure=None
    try:
        helper=Path(__file__).with_name('exec_managed.py')
        (folder/'entry.py').write_bytes(helper.read_bytes());(folder/'entry.py').chmod(0o600)
        store.save(folder/'launcher.json',dict(owner=owner,command=command(bound,folder/'run'),
            entry_sha256=pkg.sha(folder/'entry.py'),runner_sha256=pkg.sha(__file__),created_unix=time.time()))
        previous={sig:signal.signal(sig,lambda *_:stopped.set()) for sig in (signal.SIGTERM,signal.SIGINT)}
        with sup.processes.child_subreaper():
            try:
                require(pkg.sha(bound['entry'])==bound['entry_sha256'],'entry changed before launch')
                child=subprocess.Popen([bound['python']['path'],'-I','-B',str(folder/'entry.py'),str(folder)],
                    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                    start_new_session=True,close_fds=True,pass_fds=(claim_fd,),
                    env=dict(PATH='/usr/bin:/bin',HOME=str(Path.home()),LANG='C.UTF-8'),umask=0o077)
                managed_owner=sup.process_identity(child.pid)
                store.save(folder/'managed-owner.json',managed_owner)
                logger=sup.RotatingLog(folder/'wrapper.log',256*1024);logger.start(child.stdout)
                while child.poll() is None:
                    if stopped.is_set() or (folder/'stop.request.json').exists():
                        reply=formal_stop(folder)
                        if reply is not None:
                            store.save(folder/'stop-response.json',reply);break
                        time.sleep(.05)
                    require(logger.error is None,'generation stdout capture failed')
                    store.tree(folder,store.RESERVATIONS[bound['kind']]-store.META_LIMIT,hashes=False)
                    stopped.wait(.25)
                if child.poll() is None:child.wait(timeout=100)
                logger.thread.join(10)
                require(not logger.thread.is_alive() and logger.error is None,'stdout capture did not close')
            except BaseException as error:
                failure=repr(error)
            finally:
                if child is not None and child.poll() is None:
                    try:
                        reply=formal_stop(folder)
                        if reply is not None:store.save(folder/'cleanup-stop.json',reply)
                    except BaseException:
                        # An unavailable stop receipt is not permission to signal
                        # historical/unrelated PIDs. This identity is our child.
                        sup.signal_owner(managed_owner,signal.SIGTERM)
                    sup.processes.stop_children({'manager':child},{'manager':logger.thread},grace=100,kill_grace=5)
            require(child is not None,'managed entry never started')
            proof=cleanup_proof(folder)
            result.update(state='STOPPED' if child.returncode==0 and failure is None else 'FAILED',
                managed_returncode=child.returncode,error=failure,cleanup_confirmed=True)
            # Terminal outcome precedes the separate atomic seal. Do not
            # persist a stale "sealed: false" after sealing has succeeded.
            store.save(folder/'wrapper-terminal.json',{k:v for k,v in result.items() if k!='sealed'})
            store.seal(folder,proof,claim_fd);result['sealed']=True
    except BaseException as error:
        result.update(error=repr(error))
        try:store.save(folder/'wrapper-failure.json',result)
        except OSError:pass
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)
        os.close(claim_fd)
    return result


def stop(root,name):
    with store.locked(root) as (root,meta):
        require(store.NAME.fullmatch(name),'invalid generation')
        folder=root/'generations'/name;value=store.reservation(folder,meta)
        if (folder/'seal.json').exists():return dict(state='SEALED',signal_sent=False)
        if not (folder/'stop.request.json').exists():
            store.save(folder/'stop.request.json',dict(reason='explicit_stop',unix_seconds=time.time()))
        sent=sup.signal_owner(value['owner'],signal.SIGTERM)
        return dict(state='STOP_REQUESTED' if sent else 'OWNER_ABSENT',signal_sent=sent,cleanup_confirmed=False)


def reconcile(root,name):
    with store.locked(root) as (root,meta):
        require(store.NAME.fullmatch(name),'invalid generation')
        folder=root/'generations'/name;value=store.reservation(folder,meta)
        require(not owner_live(value['owner']),'wrapper is still alive')
    with store.claim(folder) as fd:
        proof=cleanup_proof(folder)
        store.save(folder/'reconciled.json',dict(read_only_process_check=True,proof=proof,
                                               unix_seconds=time.time(),historical_receipts_unchanged=True))
        store.seal(folder,proof,fd)
    return dict(state='SEALED',generation=name,signals_sent=0)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('init','run','list','plan','prune','resume-prune','tail','stop','reconcile'))
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--kind',choices=tuple(store.RESERVATIONS))
    parser.add_argument('--package',type=Path);parser.add_argument('--manifest-sha256')
    parser.add_argument('--backend-run',type=Path);parser.add_argument('--generation')
    parser.add_argument('--plan',type=Path);parser.add_argument('--file');parser.add_argument('--output',type=Path)
    parser.add_argument('--max-bytes',type=int,default=256*1024**2)
    parser.add_argument('--max-generations',type=int,default=16);parser.add_argument('--max-age-days',type=int,default=14)
    parser.add_argument('--tail-bytes',type=int,default=65536)
    args=parser.parse_args()
    if args.action=='init':value=dict(created=str(store.init(args.root,max_bytes=args.max_bytes,
        max_generations=args.max_generations,max_age_days=args.max_age_days)),services_started=0)
    elif args.action=='run':
        require(args.kind and args.package and args.manifest_sha256,'run requires kind and package identity')
        value=run(args.root,binding(args.kind,args.package,args.manifest_sha256,args.backend_run))
    elif args.action=='list':
        with store.locked(args.root) as (root,meta):value=dict(policy=meta['policy'],generations=store.inventory(root,meta))
    elif args.action=='plan':
        require(args.output is not None,'save an explicit plan outside the store')
        output=args.output.absolute()
        require(output==output.resolve() and output.is_relative_to(ROOT/'.q4t-work') and
                not output.is_relative_to(args.root.resolve()) and not output.exists(),'new external plan required')
        store.save(output,store.plan(args.root));value=dict(plan=str(output),destructive=False)
    elif args.action=='prune':
        require(args.plan is not None,'explicit saved plan required')
        value=store.apply(args.root,store.read(args.plan))
    elif args.action=='resume-prune':value=store.resume_gc(args.root)
    elif args.action=='tail':
        require(args.generation and args.file and args.output,'tail requires generation/file/output')
        value=store.tail(args.root,args.generation,args.file,args.output,args.tail_bytes)
    else:
        require(args.generation,'generation required')
        value=(stop if args.action=='stop' else reconcile)(args.root,args.generation)
    print(json.dumps(value))
    return 1 if args.action=='run' and value['state']!='STOPPED' else 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,RuntimeError) as error:
        print('retention refused: '+str(error),file=sys.stderr);raise SystemExit(1)
