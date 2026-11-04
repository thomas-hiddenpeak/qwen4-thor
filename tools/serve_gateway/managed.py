"""Explicit, snapshot-bound gateway supervision; never owns the model service."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import itertools
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_gateway import gateway as g
from tools.serve_gateway import tls
from tools.serve_service import package as pkg, service, supervisor as sup
from tools.serve_ops import identity

LEGACY_SOURCES=(*pkg.SOURCES,'tools/serve_gateway/gateway.py',
         'tools/serve_gateway/managed.py','tools/serve_gateway/managed_child.py',
         'docs/serve-openapi-v1.json')
SOURCES=(*LEGACY_SOURCES, 'tools/serve_gateway/client_policy.py')
V3_SOURCES=(*SOURCES, 'tools/serve_service/resources.py')
V4_SOURCES=(*V3_SOURCES, 'tools/serve_gateway/tls.py')
SETTINGS=dict(poll_seconds=1,startup_seconds=10,drain_seconds=35,
              term_seconds=5,kill_seconds=5,log_bytes=4*1024**2,
              event_bytes=256*1024,restarts=0)


def private(path):
    path=Path(path)
    info=path.lstat()
    g.require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid() and
              info.st_mode & 0o077==0,'private owned directory required')
    return path


def read(path):
    return g.parse(g.read_file(path,maximum=2*1024**2,private=True))


def prepare(policy,nginx,certificate,key,output,client_policy=None,*,trusted_ca=None):
    target=pkg.private_directory(output)
    binary=target/'nginx'
    original=pkg.sha(nginx)
    g.save(binary,g.read_file(nginx,maximum=32*1024**2));binary.chmod(0o500)
    g.require(pkg.sha(binary)==original,'nginx changed while copying')
    gateway=g.prepare(policy,binary,certificate,key,target/'gateway',client_policy,trusted_ca=trusted_ca)
    for name in V4_SOURCES:
        dest=target/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True)
        g.save(dest,g.read_file(ROOT/name,maximum=2*1024**2));dest.chmod(0o400)
    python=Path(sys.executable).resolve()
    manifest=dict(version=4,repository=str(ROOT),gateway=gateway,settings=SETTINGS,
        python=dict(path=str(python),sha256=pkg.sha(python)),
        sources={n:pkg.sha(target/'source'/n) for n in V4_SOURCES},
        automatic_start=False,managed_gateway_accepted=False)
    pkg.save(target/'manifest.json',manifest)
    digest=pkg.sha(target/'manifest.json')
    verify(target,digest)
    return dict(package=str(target),manifest_sha256=digest,automatic_start=False)


def verify(package,digest):
    package=private(Path(package).resolve(strict=True))
    g.require(pkg.sha(package/'manifest.json')==digest,'managed manifest changed')
    manifest=read(package/'manifest.json')
    sources={1:LEGACY_SOURCES,2:SOURCES,3:V3_SOURCES,4:V4_SOURCES}.get(manifest['version'])
    g.require(type(manifest['version']) is int and sources is not None and
              manifest['settings']==SETTINGS and
              set(manifest['sources'])==set(sources),'unknown managed gateway contract')
    g.require(Path(manifest['gateway']['package'])==package/'gateway','gateway path changed')
    for name,digest in manifest['sources'].items():
        raw=g.read_file(package/'source'/name,maximum=2*1024**2,private=True)
        g.require(hashlib.sha256(raw).hexdigest()==digest,'snapshot source changed: '+name)
    g.require(pkg.sha(manifest['python']['path'])==manifest['python']['sha256'],'Python changed')
    # Always use the generator whose bytes the package pins. Worktree entry is
    # allowed only while it still has those same bytes; run then execs snapshot.
    g.require(pkg.sha(g.__file__)==manifest['sources']['tools/serve_gateway/gateway.py'],
              'use the pinned package entry after tooling changes')
    gateway=g.check(package/'gateway',manifest['gateway']['manifest_sha256'])
    g.require(gateway['nginx']['path']==str(package/'nginx'),'foreign nginx path')
    return manifest,gateway


@contextmanager
def claim(package):
    path=Path(package)/'gateway/runtime/managed.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        info=os.fstat(fd)
        g.require(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and
                  info.st_mode & 0o077==0,'invalid package claim')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield fd
    finally:os.close(fd)


def alive(owner):
    try:return sup.process_identity(owner['pid'])==owner
    except (FileNotFoundError,ProcessLookupError):return False


def bound_backend(directory,gateway):
    directory=Path(directory).resolve(strict=True)
    verified=g.check_backend(directory,gateway)
    control=read(directory/'controller.json')
    state=service.status(directory)
    g.require(state['controller_live'] and state['worker']['backend']==verified['backend'],
              'backend changed during binding')
    process=identity.process(verified['backend']['pid'],gateway['policy']['upstream_port'])
    g.require(process['listener_inode']==verified['listener_inode'],'backend listener changed')
    result = dict(run=str(directory),controller_sha256=pkg.sha(directory/'controller.json'),
        controller=control['owner'],worker=state['worker']['owner'],
        backend=verified['backend'],process=process)
    if 'operations_port' in verified:
        result.update(operations_port=verified['operations_port'],
                      operations_process=verified['operations_process'])
        g.require(identity.operations(process, result['operations_port']) ==
                  result['operations_process'], 'operations listener changed')
    return result


def check_backend(binding,port):
    directory=Path(binding['run'])
    g.require(pkg.sha(directory/'controller.json')==binding['controller_sha256'],
              'backend controller record changed')
    state=service.status(directory)
    g.require(state.get('controller_live') and alive(binding['controller']) and
        state['state'] in ('READY','DEGRADED','OBSERVATION_UNAVAILABLE') and
        state['worker']['owner']==binding['worker'] and
        state['worker']['backend']==binding['backend'] and alive(binding['worker']),
        'bound backend is no longer running')
    g.require(identity.process(binding['backend']['pid'],port)==binding['process'],
              'backend listener or executable changed')
    if binding.get('operations_port', 0):
        g.require(identity.operations(binding['process'], binding['operations_port']) ==
                  binding['operations_process'], 'operations listener or executable changed')


def listener(owner,host,port,binary):
    """Verify the exact IPv4 listener owned by the master, without HTTP."""
    g.require(alive(owner),'gateway process changed')
    proc=Path('/proc')/str(owner['pid'])
    a,b=(proc/'ns/net').stat(),Path('/proc/self/ns/net').stat()
    g.require((a.st_dev,a.st_ino)==(b.st_dev,b.st_ino),'gateway network namespace differs')
    fds=list(itertools.islice((proc/'fd').iterdir(),1025))
    g.require(len(fds)<=1024,'gateway descriptor observation bound')
    owned=set()
    for path in fds:
        try:owned.add(os.readlink(path))
        except FileNotFoundError:pass
    address=ipaddress.IPv4Address(host).packed[::-1].hex().upper()
    rows=identity.bounded_file(Path('/proc/net/tcp')).decode().splitlines()[1:]
    matches=[]
    for row in rows:
        fields=row.split()
        g.require(len(fields)>=10,'invalid listener observation')
        if fields[1].upper()==f'{address}:{port:04X}' and fields[3]=='0A':
            matches.append(fields[9])
    g.require(len(matches)==1 and 'socket:['+matches[0]+']' in owned,'gateway listener not owned')
    digest=hashlib.sha256(identity.bounded_file(proc/'exe',32*1024**2,follow=True)).hexdigest()
    g.require(digest==binary and alive(owner),'gateway executable changed')
    return dict(owner=owner,listener_inode=int(matches[0]),
                net_namespace=[a.st_dev,a.st_ino],executable_sha256=digest)


def intent(output,reason):
    path=Path(output)/'stop.request.json'
    try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:return
    with os.fdopen(fd,'w') as stream:
        json.dump(dict(reason=reason,unix_seconds=time.time()),stream)
        stream.flush();os.fsync(stream.fileno())
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def stop(output):
    output=private(Path(output).resolve(strict=True))
    control=read(output/'controller.json')
    if (output/'terminal.json').exists():return status(output)
    intent(output,'explicit_stop')
    try:sent=sup.signal_owner(control['owner'],signal.SIGTERM)
    except FileNotFoundError:sent=False
    return dict(state='STOP_REQUESTED' if sent else 'OWNER_ABSENT',
                signal_sent=sent,cleanup_confirmed=False)


def status(output):
    output=private(Path(output).resolve(strict=True))
    if (output/'terminal.json').exists():return read(output/'terminal.json')
    control=read(output/'controller.json')
    live=alive(control['owner'])
    state=read(output/'status.json') if (output/'status.json').exists() else None
    return dict(state=state['state'] if live and state else 'STARTING' if live else 'OWNER_ABSENT',
        controller_live=live,observation=state,cleanup_confirmed=False)


def child_command(package,manifest,output):
    return [manifest['python']['path'],'-I','-B',
        str(Path(package)/'source/tools/serve_gateway/managed_child.py'),'--run',str(output)]


def supervise(package,digest,output,manifest,gateway,binding,claim_fd,*,
              observe=listener,dependency=check_backend,tls_clock=None):
    """Internal CPU seams are not exposed as policy or CLI overrides."""
    cfg=manifest['settings'];stopping=threading.Event()
    previous={s:signal.signal(s,lambda *_:stopping.set()) for s in (signal.SIGTERM,signal.SIGINT)}
    owner=sup.process_identity(os.getpid())
    child=logger=None;nginx_owner=None;clean=True;failure=None;tls_drain=False
    events=sup.RotatingLog(output/'events.log',cfg['event_bytes'])
    state=dict(version=1,state='STARTING',owner=owner,nginx=None,starts=0,restarts=0,
               replayed_requests=0,package_manifest_sha256=digest,cleanup_confirmed=False)
    def stopped():return stopping.is_set() or (output/'stop.request.json').exists()
    def publish(value,**changes):
        changed=value!=state['state']
        state.update(state=value,updated_unix=time.time(),**changes)
        pkg.save(output/'status.json',state)
        if changed:events.write((json.dumps(dict(unix_seconds=time.time(),state=value))+'\n').encode())
    try:
        with sup.processes.child_subreaper():
            try:
                publish('STARTING')
                dependency(binding,gateway['policy']['upstream_port'])
                if not stopped():
                    env=dict(PATH='/usr/bin:/bin',LANG='C.UTF-8',HOME=str(Path.home()),
                             Q4T_GATEWAY_CLAIM_FD=str(claim_fd))
                    child=subprocess.Popen(child_command(package,manifest,output),
                        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                        start_new_session=True,close_fds=True,pass_fds=(claim_fd,),env=env,umask=0o077)
                    clean=False;publish('STARTING',starts=1)
                    nginx_owner=sup.process_identity(child.pid)
                    logger=sup.RotatingLog(output/'gateway.log',cfg['log_bytes']);logger.start(child.stdout)
                    publish('STARTING',nginx=nginx_owner,starts=1)
                    start=time.monotonic();first=None
                    while not stopped():
                        g.require(child.poll() is None,'gateway master exited unexpectedly')
                        g.require(logger.error is None,'gateway log capture failed')
                        dependency(binding,gateway['policy']['upstream_port'])
                        # Snapshot input tampering closes this generation, never reloads.
                        for name,wanted in gateway['files'].items():
                            raw=g.read_file(Path(package)/'gateway'/name,private=True)
                            g.require(hashlib.sha256(raw).hexdigest()==wanted,'gateway input changed')
                        if 'tls' in gateway:
                            validity=tls.observe(gateway['tls'],None if tls_clock is None else tls_clock())
                            if validity['state']!=state.get('tls',{}).get('state'):
                                events.write((json.dumps(dict(tls=validity))+'\n').encode())
                            state['tls']=validity
                            if validity['state'] not in ('VALID','EXPIRING'):
                                failure=validity['reason'];tls_drain=True
                                state['error_code']=failure
                                intent(output,failure)
                                break
                        try:
                            observed=observe(nginx_owner,gateway['policy']['listen_host'],
                                gateway['policy']['listen_port'],gateway['nginx']['sha256'])
                        except (OSError,ValueError):
                            if first is not None or time.monotonic()-start>=cfg['startup_seconds']:raise
                        else:
                            g.require(first is None or first==observed,'gateway listener changed')
                            first=observed;publish('READY',listener=observed)
                        stopping.wait(cfg['poll_seconds'])
                    intent(output,'controller_signal_or_stop')
                    publish('STOPPING')
            except BaseException as error:
                failure=repr(error)
            finally:
                try:
                    if child is not None:
                        # QUIT drains existing streams; failures stop promptly.
                        if (failure is None or tls_drain) and child.poll() is None:
                            sup.signal_owner(nginx_owner,signal.SIGQUIT)
                            until=time.monotonic()+cfg['drain_seconds']
                            while child.poll() is None and time.monotonic()<until:time.sleep(.02)
                            g.require(child.poll()==0,'gateway graceful drain did not complete')
                        readers={} if logger is None or logger.thread is None else {'gateway':logger.thread}
                        sup.processes.stop_children({'gateway':child},readers,
                            grace=cfg['term_seconds'],kill_grace=cfg['kill_seconds'])
                        clean=True
                        if logger is None:child.stdout.close()
                        else:g.require(logger.error is None,'gateway log capture failed')
                except BaseException as error:
                    cleanup_error=repr(error)
                    # A failed graceful wait must still drain/reap owned workers.
                    try:
                        if child is not None:
                            readers={} if logger is None or logger.thread is None else {'gateway':logger.thread}
                            sup.processes.stop_children({'gateway':child},readers,
                                grace=cfg['term_seconds'],kill_grace=cfg['kill_seconds'])
                            clean=True
                            if logger is None:child.stdout.close()
                    except BaseException as secondary:cleanup_error+='; '+repr(secondary)
                    state['cleanup_error']=cleanup_error
                    failure=failure or cleanup_error
        value='FAILED' if failure else 'STOPPED'
        publish(value,error=failure,cleanup_confirmed=clean,nginx_returncode=None if child is None else child.returncode)
        pkg.save(output/'terminal.json',dict(state,automatic_reactivation=False,
            backend_stopped_by_gateway=False,finished_unix=time.time()))
        return 1 if failure else 0
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)


def run(package,digest,backend_run,output):
    manifest,gateway=verify(package,digest)
    output=pkg.private_directory(output,Path(manifest['repository']))
    owner=sup.process_identity(os.getpid())
    control=dict(version=1,owner=owner,package=str(Path(package).resolve()),
        manifest_sha256=digest,backend_run=str(Path(backend_run).resolve()),created_unix=time.time())
    pkg.save(output/'controller.json',control)
    try:
        with claim(package) as fd:
            binding=bound_backend(backend_run,gateway)
            control.update(binding=binding,claim_identity=[os.fstat(fd).st_dev,os.fstat(fd).st_ino])
            pkg.save(output/'controller.json',control)
            return supervise(package,digest,output,manifest,gateway,binding,fd)
    except Exception as error:
        if not (output/'terminal.json').exists():
            # Only setup errors reach here normally; never invent cleanup for
            # an interrupted supervisor whose actual child state is unknown.
            spawned=(output/'status.json').exists() and read(output/'status.json').get('starts',0)>0
            pkg.save(output/'terminal.json',dict(state='FAILED',error=repr(error),
                cleanup_confirmed=not spawned,automatic_reactivation=False,
                backend_stopped_by_gateway=False,finished_unix=time.time()))
        raise


def unit(package,digest,backend_run,output):
    manifest,_=verify(package,digest)
    def quote(value):
        value=str(value)
        g.require(not any(c in value for c in '\n\r\x00'),'invalid unit argument')
        return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'
    argv=[manifest['python']['path'],'-I','-B',Path(package).resolve()/'source/tools/serve_gateway/managed.py',
        'run','--package',Path(package).resolve(),'--manifest-sha256',digest,
        '--backend-run',Path(backend_run).resolve(),'--output',Path(output).absolute()]
    return ('[Unit]\nDescription=Qwen4 Thor explicit managed TLS gateway\n'
        '[Service]\nType=exec\nExecStart='+' '.join(map(quote,argv))+'\n'
        'Restart=no\nKillMode=mixed\nTimeoutStopSec=60\nUMask=0077\n'
        'NoNewPrivileges=yes\nLimitCORE=0\n'
        'UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME OPENSSL_CONF OPENSSL_MODULES\n'
        'StandardOutput=journal\nStandardError=journal\n'
        '# No Install section: activation must be explicit.\n')


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('prepare')
    for name in ('policy','nginx','certificate','key','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--client-policy',type=Path)
    p.add_argument('--trusted-ca',type=Path)
    for name in ('run','unit'):
        p=sub.add_parser(name)
        for option in ('package','backend-run','output'):p.add_argument('--'+option,type=Path,required=True)
        p.add_argument('--manifest-sha256',required=True)
    for name in ('status','stop'):
        p=sub.add_parser(name);p.add_argument('--run',type=Path,required=True)
    args=parser.parse_args()
    if args.action=='prepare':value=prepare(args.policy,args.nginx,args.certificate,args.key,args.output,
                                           args.client_policy,trusted_ca=args.trusted_ca)
    elif args.action=='unit':print(unit(args.package,args.manifest_sha256,args.backend_run,args.output),end='');return 0
    elif args.action in ('status','stop'):value=(status if args.action=='status' else stop)(args.run)
    else:
        manifest,_=verify(args.package,args.manifest_sha256)
        entry=args.package.resolve()/'source/tools/serve_gateway/managed.py'
        if Path(__file__).resolve()!=entry:
            command=[manifest['python']['path'],'-I','-B',str(entry),'run',
                '--package',str(args.package.resolve()),'--manifest-sha256',args.manifest_sha256,
                '--backend-run',str(args.backend_run.resolve()),'--output',str(args.output.absolute())]
            os.execve(command[0],command,dict(PATH='/usr/bin:/bin',LANG='C.UTF-8',HOME=str(Path.home())))
        return run(args.package,args.manifest_sha256,args.backend_run,args.output)
    print(json.dumps(value));return 0


if __name__=='__main__':
    raise SystemExit(main())
