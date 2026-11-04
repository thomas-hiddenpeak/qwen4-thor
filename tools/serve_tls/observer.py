"""Read-only gateway/TLS observer with an isolated, bounded static exporter."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_tls import core as c,source
from tools.serve_host import observer as host
from tools.serve_service import package as pkg
from tools.serve_gateway import managed as m
from tools.serve_watchdog.storage import Directory,encoded,sha

FILES=tuple(dict.fromkeys((*host.FILES,'tools/serve_tls/core.py','tools/serve_tls/source.py',
    'tools/serve_tls/observer.py','tools/serve_tls/child.py')))


def prepare(gateway,digest,run,deployment,nginx,port,output):
    pinned=source.package(gateway,digest);repository=Path(pinned['repository']);run=Path(run).absolute()
    c.require(re.fullmatch(r'[A-Za-z0-9_-]{1,64}',deployment) and type(port) is int and 1024<=port<=65535 and
        port not in (pinned['listen_port'],pinned['backend_port']),'binding_invalid')
    c.require(run==run.resolve() and any(run.is_relative_to(repository/n) for n in ('build','.q4t-work')),'binding_invalid')
    output=pkg.private_directory(output,repository)
    c.require(not run.is_relative_to(output) and not output.is_relative_to(run),'binding_invalid')
    m.g.save(output/'nginx',m.g.read_file(nginx,maximum=32*1024**2));(output/'nginx').chmod(0o500)
    # Only the reviewed gateway binary can serve this static loopback surface.
    c.require(pkg.sha(output/'nginx')==pinned['nginx_sha256'],'binding_invalid')
    (output/'runtime').mkdir(mode=0o700)
    for name in ('body','proxy','fastcgi','uwsgi','scgi'):(output/'runtime'/name).mkdir(mode=0o700)
    for name in FILES:
        target=output/'source'/name;target.parent.mkdir(parents=True,exist_ok=True)
        m.g.save(target,(ROOT/name).read_bytes());target.chmod(0o400)
    m.g.save(output/'nginx.conf',host.config(output,port).encode())
    python=Path(sys.executable).resolve()
    value=dict(version=1,**pinned,gateway_run=str(run),deployment=deployment,port=port,
        context=source.inspection.current_context(),python=dict(path=str(python),sha256=pkg.sha(python)),
        files={n:pkg.sha(output/n) for n in ('nginx','nginx.conf',*('source/'+n for n in FILES))},
        automatic_start=False,model_signalled=False,gateway_signalled=False)
    pkg.save(output/'policy.json',value);checksum=pkg.sha(output/'policy.json')
    result=subprocess.run([output/'nginx','-e','stderr','-t','-p',str(output)+'/', '-c',output/'nginx.conf'],capture_output=True,timeout=10)
    m.g.save(output/'syntax-private.log',result.stdout+result.stderr);c.require(result.returncode==0,'binding_invalid')
    pid=output/'nginx.pid'
    if os.path.lexists(pid):
        c.require(m.g.read_file(pid,private=True,maximum=64)==b'','binding_invalid');pid.unlink()
    verify(output,checksum)
    return dict(state=str(output),policy_sha256=checksum,automatic_start=False)


def verify(path,digest):
    path=Path(path).absolute()
    with Directory(path) as directory:
        raw=directory.read('policy.json');c.require(sha(raw)==digest,'binding_invalid');policy=source.parse(raw)
        c.require(type(policy['version']) is int and policy['version']==1 and policy['context']==source.inspection.current_context() and
            policy['automatic_start'] is policy['model_signalled'] is policy['gateway_signalled'] is False and
            set(policy['files'])=={'nginx','nginx.conf',*('source/'+n for n in FILES)},'binding_invalid')
        c.material(policy['material'])
        c.require(Path(sys.executable).resolve()==Path(policy['python']['path']) and pkg.sha(policy['python']['path'])==policy['python']['sha256'],
            'binding_invalid')
        for name,digest in policy['files'].items():
            c.require(hashlib.sha256(m.g.read_file(path/name,private=True,maximum=32*1024**2)).hexdigest()==digest,'binding_invalid')
        c.require((path/'nginx.conf').read_text()==host.config(path,policy['port']),'binding_invalid')
        for name in ('body','proxy','fastcgi','uwsgi','scgi'):m.private(path/'runtime'/name)
        return policy


def evaluate(directory,policy,digest):
    c.require(sha(directory.read('policy.json'))==digest,'binding_invalid')
    bound=source.optional(directory,'binding.json');value,new=source.observe(policy,bound)
    if new is not None and new!=bound:directory.write('binding.json',encoded(new))
    directory.write('status.json',encoded(value))
    directory.write('metrics.prom',c.metrics(value,policy['deployment'],digest).encode())
    return value


def observe_once(path,digest):
    policy=verify(path,digest)
    with Directory(path) as directory,directory.claim('observer.lock',0):return evaluate(directory,policy,digest)


def watch(path,digest):return host.watch(path,digest,verify_policy=verify,project=evaluate,kind='tls')


def unit(path,digest):
    policy=verify(path,digest);path=Path(path).resolve()
    argv=[policy['python']['path'],'-I','-B',str(path/'source/tools/serve_tls/observer.py'),'watch',
        '--state',str(path),'--policy-sha256',digest]
    c.require(all(re.fullmatch(r'[A-Za-z0-9_./=-]+',v) for v in argv),'binding_invalid')
    return ('[Unit]\nDescription=Q4T read-only TLS and gateway observer\n[Service]\nType=simple\nExecStart='+
        ' '.join(argv)+'\nRestart=no\nKillMode=control-group\nTimeoutStopSec=15\nUMask=0077\nNoNewPrivileges=yes\n')


def main():
    os.umask(0o077);parser=argparse.ArgumentParser(description=__doc__);subs=parser.add_subparsers(dest='action',required=True)
    p=subs.add_parser('prepare')
    for name in ('gateway-package','gateway-run','nginx','output'):p.add_argument('--'+name,type=Path,required=True)
    for name in ('gateway-manifest-sha256','deployment'):p.add_argument('--'+name,required=True)
    p.add_argument('--port',type=int,required=True)
    for name in ('observe','watch','metrics','status','unit'):
        p=subs.add_parser(name);p.add_argument('--state',type=Path,required=True);p.add_argument('--policy-sha256',required=True)
    args=parser.parse_args()
    if args.action=='prepare':
        value=prepare(args.gateway_package,args.gateway_manifest_sha256,args.gateway_run,args.deployment,args.nginx,args.port,args.output)
    else:
        policy=verify(args.state,args.policy_sha256);entry=args.state.resolve()/'source/tools/serve_tls/observer.py'
        if Path(__file__).resolve()!=entry:
            argv=[policy['python']['path'],'-I','-B',str(entry),args.action,'--state',str(args.state.resolve()),'--policy-sha256',args.policy_sha256]
            os.execve(argv[0],argv,dict(PATH='/usr/bin:/bin',LANG='C.UTF-8'))
        if args.action=='watch':return watch(args.state,args.policy_sha256)
        if args.action=='unit':print(unit(args.state,args.policy_sha256),end='');return 0
        if args.action=='observe':value=observe_once(args.state,args.policy_sha256)
        else:
            with Directory(args.state) as directory:value=directory.json('status.json')
            if args.action=='metrics':print(c.metrics(value,policy['deployment'],args.policy_sha256),end='');return 0
            now=source.clock();value=dict(value,projection_age_seconds=now['boottime']-value['evaluated_boottime']
                if now['boot_id']==value['boot_id'] else None)
    print(json.dumps(value));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError,TypeError,RuntimeError):
        print(json.dumps(dict(error='tls_observer_rejected',model_signalled=False,gateway_signalled=False)),file=sys.stderr)
        raise SystemExit(1)
