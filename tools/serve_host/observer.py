"""Pinned read-only host observer and bounded loopback static metrics export."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_host import core as c,source
from tools.serve_service import package as pkg,supervisor as sup
from tools.serve_gateway import managed as m
from tools.serve_watchdog.storage import Directory,encoded,sha

LEGACY_FILES=tuple(dict.fromkeys((*pkg.V5_SOURCES,'tools/serve_gateway/gateway.py','tools/serve_gateway/managed.py',
    'tools/serve_watchdog/core.py','tools/serve_watchdog/storage.py',
    'tools/serve_host/core.py','tools/serve_host/source.py','tools/serve_host/observer.py','tools/serve_host/child.py')))
FILES=tuple(dict.fromkeys((*LEGACY_FILES,*m.V4_SOURCES)))


def config(path,port):
    metrics=m.g.quoted(Path(path)/'metrics.prom');pid=m.g.quoted(Path(path)/'nginx.pid')
    temporary='\n'.join('  '+directive+'_temp_path '+m.g.quoted(Path(path)/'runtime'/name)+';'
        for directive,name in (('client_body','body'),('proxy','proxy'),('fastcgi','fastcgi'),('uwsgi','uwsgi'),('scgi','scgi')))
    return f'''worker_processes 1;
pid {pid};
error_log stderr warn;
worker_shutdown_timeout 3s;
events {{ worker_connections 32; }}
http {{
{temporary}
  access_log off;
  server_tokens off;
  keepalive_timeout 0;
  client_header_timeout 2s;
  client_body_timeout 2s;
  send_timeout 2s;
  client_max_body_size 1;
  large_client_header_buffers 2 1k;
  open_file_cache off;
  etag off;
  if_modified_since off;
  max_ranges 0;
  server {{
    listen 127.0.0.1:{port} backlog=16;
    server_name localhost;
    location = /metrics {{
      if ($request_method !~ ^(GET|HEAD)$) {{ return 405; }}
      if ($args != "") {{ return 400; }}
      default_type text/plain;
      add_header Cache-Control "no-store" always;
      alias {metrics};
    }}
    location / {{ return 404; }}
  }}
}}
'''


def prepare(backend,digest,run,deployment,nginx,port,output):
    c.require(type(port) is int and 1024<=port<=65535 and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',deployment),
              'binding_invalid')
    backend=Path(backend).resolve(strict=True)
    manifest=pkg.verify(backend,digest)
    c.require(manifest['version']==5 and port!=manifest['settings']['port'],'binding_invalid')
    run=Path(run).absolute();repository=Path(manifest['repository'])
    c.require(run==run.resolve() and any(run.is_relative_to(repository/name) for name in ('build','.q4t-work')),
              'binding_invalid')
    output=pkg.private_directory(output,repository)
    c.require(not run.is_relative_to(output) and not output.is_relative_to(run),'binding_invalid')
    m.g.save(output/'nginx',m.g.read_file(nginx,maximum=32*1024**2));(output/'nginx').chmod(0o500)
    (output/'runtime').mkdir(mode=0o700)
    for name in ('body','proxy','fastcgi','uwsgi','scgi'):(output/'runtime'/name).mkdir(mode=0o700)
    for name in FILES:
        target=output/'source'/name;target.parent.mkdir(parents=True,exist_ok=True)
        m.g.save(target,(ROOT/name).read_bytes());target.chmod(0o400)
    m.g.save(output/'nginx.conf',config(output,port).encode())
    python=Path(sys.executable).resolve()
    value=dict(version=2,repository=str(repository),backend_package=str(backend),backend_manifest_sha256=digest,
        backend_run=str(run),host_resources=manifest['host_resources'],deployment=deployment,port=port,
        context=source.inspection.current_context(),python=dict(path=str(python),sha256=pkg.sha(python)),
        files={name:pkg.sha(output/name) for name in ('nginx','nginx.conf',*('source/'+n for n in FILES))},
        automatic_start=False,model_signalled=False)
    pkg.save(output/'policy.json',value);checksum=pkg.sha(output/'policy.json')
    result=subprocess.run([output/'nginx','-e','stderr','-t','-p',str(output)+'/', '-c',output/'nginx.conf'],
        capture_output=True,timeout=10)
    m.g.save(output/'syntax-private.log',result.stdout+result.stderr)
    c.require(result.returncode==0,'binding_invalid')
    # nginx -t creates an empty PID file without starting a master. Remove
    # only this owned empty syntax artifact; never remove a runtime PID.
    pid_path=output/'nginx.pid'
    if os.path.lexists(pid_path):
        c.require(m.g.read_file(pid_path,private=True,maximum=64)==b'','binding_invalid')
        pid_path.unlink()
    verify(output,checksum)
    return dict(state=str(output),policy_sha256=checksum,automatic_start=False)


def verify(path,digest):
    path=Path(path).absolute()
    with Directory(path) as directory:
        raw=directory.read('policy.json');c.require(sha(raw)==digest,'binding_invalid');policy=source.parse(raw)
        sources={1:LEGACY_FILES,2:FILES}.get(policy['version'])
        c.require(type(policy['version']) is int and sources is not None and
            policy['context']==source.inspection.current_context() and policy['automatic_start'] is False and
            policy['model_signalled'] is False and set(policy['files'])=={'nginx','nginx.conf',*('source/'+n for n in sources)},
            'binding_invalid')
        c.require(Path(sys.executable).resolve()==Path(policy['python']['path']) and
            pkg.sha(policy['python']['path'])==policy['python']['sha256'],'binding_invalid')
        c.require(policy['host_resources']==source.r.validate(policy['host_resources']),'binding_invalid')
        for name,checksum in policy['files'].items():
            raw=m.g.read_file(path/name,private=True,maximum=32*1024**2)
            c.require(hashlib.sha256(raw).hexdigest()==checksum,'binding_invalid')
        c.require((path/'nginx.conf').read_text()==config(path,policy['port']),'binding_invalid')
        for name in ('body','proxy','fastcgi','uwsgi','scgi'):m.private(path/'runtime'/name)
        return policy


def evaluate(directory,policy,digest):
    bound=source.optional(directory,'binding.json')
    value,new=source.observe(policy,bound)
    if new is not None and new!=bound:directory.write('binding.json',encoded(new))
    directory.write('status.json',encoded(value))
    directory.write('metrics.prom',c.metrics(value,policy['deployment'],digest).encode())
    return value


def observe_once(path,digest):
    policy=verify(path,digest)
    with Directory(path) as directory,directory.claim('observer.lock',0):
        return evaluate(directory,policy,digest)


def watch(path,digest,*,verify_policy=None,project=None,kind='host'):
    # Shared bounded exporter lifecycle. Hooks are fixed by module entrypoints,
    # never taken from a policy, environment variable or user-provided code.
    c.require(kind in ('host','tls'),'binding_invalid')
    verify_policy=verify if verify_policy is None else verify_policy
    project=evaluate if project is None else project
    path=Path(path).resolve(strict=True);policy=verify_policy(path,digest)
    stopping=threading.Event();previous={sig:signal.signal(sig,lambda *_:stopping.set()) for sig in (signal.SIGINT,signal.SIGTERM)}
    child=logger=None;failure=None;clean=True
    try:
        with Directory(path) as directory,directory.claim('observer.lock',0) as fd,sup.processes.child_subreaper():
            control=dict(owner=sup.process_identity(os.getpid()),state=str(path),policy_sha256=digest,
                         claim_identity=[os.fstat(fd).st_dev,os.fstat(fd).st_ino])
            directory.write('observer.json',encoded(control))
            try:
                project(directory,policy,digest)
                env=dict(PATH='/usr/bin:/bin',LANG='C.UTF-8',**{f'Q4T_{kind.upper()}_CLAIM_FD':str(fd)})
                child=subprocess.Popen([policy['python']['path'],'-I','-B',path/f'source/tools/serve_{kind}/child.py',
                    '--state',path],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                    start_new_session=True,close_fds=True,pass_fds=(fd,),umask=0o077)
                clean=False
                owner=sup.process_identity(child.pid)
                logger=sup.RotatingLog(path/'exporter.log',1024**2);logger.start(child.stdout)
                until=time.monotonic()+10;first=None
                while not stopping.is_set():
                    c.require(child.poll() is None and logger.error is None,'source_unavailable')
                    try:current=m.listener(owner,'127.0.0.1',policy['port'],policy['files']['nginx'])
                    except (OSError,ValueError,RuntimeError):
                        c.require(first is None and time.monotonic()<until,'source_unavailable')
                    else:
                        c.require(first is None or current==first,'binding_invalid');first=current
                        directory.write('exporter.json',encoded(dict(owner=owner,listener=current)))
                    project(directory,policy,digest)
                    stopping.wait(1)
            except BaseException as error:
                failure=str(error) if isinstance(error,c.Invalid) else 'observer_failed'
            finally:
                try:
                    if child is not None:
                        readers={} if logger is None or logger.thread is None else {'exporter':logger.thread}
                        sup.processes.stop_children({'exporter':child},readers,grace=5,kill_grace=3)
                        clean=True
                        if logger is None:child.stdout.close()
                        c.require(logger is not None and logger.error is None,'source_unavailable')
                except BaseException:
                    failure=failure or 'cleanup_unconfirmed'
                    raise
                finally:
                    # Publish while the same claim is still held. A new
                    # observer may not race an old owner's terminal write.
                    directory.write('observer-terminal.json',encoded(dict(state='FAILED' if failure else 'STOPPED',
                        reason=failure,cleanup_confirmed=clean,model_signalled=False,gateway_signalled=False,automatic_reactivation=False,
                        child_returncode=None if child is None else child.returncode)))
    except BaseException as error:
        failure=failure or (str(error) if isinstance(error,c.Invalid) else 'observer_failed')
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)
    return 1 if failure else 0


def unit(path,digest):
    policy=verify(path,digest);path=Path(path).resolve()
    argv=[policy['python']['path'],'-I','-B',str(path/'source/tools/serve_host/observer.py'),'watch',
          '--state',str(path),'--policy-sha256',digest]
    c.require(all(re.fullmatch(r'[A-Za-z0-9_./=-]+',a) for a in argv),'binding_invalid')
    return ('[Unit]\nDescription=Q4T read-only host resource observer\n[Service]\nType=simple\nExecStart='+
        ' '.join(argv)+'\nRestart=no\nKillMode=control-group\nTimeoutStopSec=15\nUMask=0077\nNoNewPrivileges=yes\n')


def main():
    os.umask(0o077);parser=argparse.ArgumentParser();subs=parser.add_subparsers(dest='action',required=True)
    prepare_parser=subs.add_parser('prepare')
    for name in ('backend-package','backend-run','nginx','output'):prepare_parser.add_argument('--'+name,type=Path,required=True)
    for name in ('backend-manifest-sha256','deployment'):prepare_parser.add_argument('--'+name,required=True)
    prepare_parser.add_argument('--port',type=int,required=True)
    for name in ('observe','watch','metrics','unit','status'):
        item=subs.add_parser(name);item.add_argument('--state',type=Path,required=True);item.add_argument('--policy-sha256',required=True)
    args=parser.parse_args()
    if args.action!='prepare':
        policy=verify(args.state,args.policy_sha256)
        entry=args.state.resolve()/'source/tools/serve_host/observer.py'
        if Path(__file__).resolve()!=entry:
            argv=[policy['python']['path'],'-I','-B',str(entry),args.action,'--state',str(args.state.resolve()),
                  '--policy-sha256',args.policy_sha256]
            os.execve(argv[0],argv,dict(PATH='/usr/bin:/bin',LANG='C.UTF-8'))
    if args.action=='prepare':
        value=prepare(args.backend_package,args.backend_manifest_sha256,args.backend_run,args.deployment,args.nginx,args.port,args.output)
    elif args.action in ('watch','unit'):
        policy=verify(args.state,args.policy_sha256)
        if args.action=='unit':print(unit(args.state,args.policy_sha256),end='');return 0
        return watch(args.state,args.policy_sha256)
    elif args.action=='observe':value=observe_once(args.state,args.policy_sha256)
    else:
        policy=verify(args.state,args.policy_sha256)
        with Directory(args.state) as directory:value=directory.json('status.json')
        now=source.clock();value=dict(value,projection_age_seconds=now['boottime']-value['evaluated_boottime']
            if now['boot_id']==value['boot_id'] else None)
        if args.action=='metrics':
            # Keep the original timestamp so old files cannot look freshly evaluated.
            print(c.metrics(value,policy['deployment'],args.policy_sha256),end='');return 0
    print(json.dumps(value));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError,TypeError,RuntimeError):
        print(json.dumps(dict(error='host_observer_rejected',model_signalled=False)),file=sys.stderr)
        raise SystemExit(1)
