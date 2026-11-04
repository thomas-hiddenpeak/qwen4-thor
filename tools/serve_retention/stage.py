"""One managed model, two retained TLS lifetimes and explicit owned pruning."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_retention import runner as r,store,cpu
from tools.serve_clients import managed_stage as prior
from tools.serve_gateway import acceptance as gw,managed_stage as lifecycle
from tools.serve_service import acceptance as a
from tools.serve_soak import evidence

BASE=cpu.BASE;PLAN=BASE/'plan.json';OUT=BASE/'execution'
STORE=BASE/'owned-generations'
LIMITS=dict(starts=1,generations=6,negatives=1,observations=64,seconds=480)
GPU_WINDOW=420
SOURCES=sorted(set((*cpu.SOURCES,*gw.SOURCES,'tools/serve_retention/stage.py')))


def configure():
    for module in (a,gw,lifecycle):
        module.BASE,module.PLAN,module.OUT=BASE,PLAN,OUT
        module.LIMITS,module.GPU_WINDOW=LIMITS,GPU_WINDOW
    a.CONTROL_LIMIT=64
    prior.OUT=OUT  # Only the reused drain observer reads this module global.


def prepare():
    r.require(not PLAN.exists() and not OUT.exists() and not STORE.exists(),'preserve prior attempt')
    r.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit reviewed sources')
    tested=r.pkg.read(BASE/'cpu-ready/result.json');r.require(tested['passed'],'CPU gate')
    for name,digest in tested['sources'].items():r.require(r.pkg.sha(ROOT/name)==digest,'CPU source changed')
    prior.configure('day');old=prior.check()
    r.require(not prior.OUT.exists(),'operator day execution exists; preserve its package/runtime evidence')
    configure()
    r.pkg.save(PLAN,dict(package=old['package'],gateway=old['gateway'],gateways=old['gateways'],
        unit='unused-retention-driver.service',token='srv12e-short',limits=LIMITS,gpu_window_seconds=GPU_WINDOW,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        source_files={name:r.pkg.sha(ROOT/name) for name in SOURCES},
        cpu_sha256=r.pkg.sha(BASE/'cpu-ready/result.json'),default=old['default'],
        private=old['private'],days={**old['old_day_plans'],str(prior.PLAN):r.pkg.sha(prior.PLAN)},
        store_policy=dict(max_bytes=128*1024**2,max_generations=2,max_age_days=14),
        no_automatic_prune=True,no_automatic_retry=True,performance_acceptance=False))
    check()
    return dict(prepared=True,models_started=0,http_started=0)


def check():
    plan=r.pkg.read(PLAN)
    r.require(plan['limits']==LIMITS and plan['gpu_window_seconds']==GPU_WINDOW,'budget changed')
    for name,digest in plan['source_files'].items():r.require(r.pkg.sha(ROOT/name)==digest,'source changed: '+name)
    r.require(r.pkg.sha(BASE/'cpu-ready/result.json')==plan['cpu_sha256'],'CPU evidence changed')
    for name,digest in {**plan['default'],**plan['private'],**plan['days']}.items():
        r.require(r.pkg.sha(ROOT/name)==digest,'bound artifact changed')
    for name in plan['days']:r.require(not (Path(name).parent/'execution').exists(),'user day executed')
    r.binding('backend',plan['package']['package'],plan['package']['manifest_sha256'])
    for selected in plan['gateways']:r.gateway.verify(selected['package'],selected['manifest_sha256'])
    return plan


class Run(gw.Run):
    graceful=lifecycle.Run.graceful
    wait_front=lifecycle.Run.wait_front
    drain=prior.Run.drain

    def __init__(self,plan):
        a.Run.__init__(self,plan)
        self.mutex=threading.RLock();self.request_ids=set()
        private=prior.BASE/'model-private/policy-private.json'
        self.keys=r.gateway.g.parse(r.gateway.g.read_file(private,private=True))
        self.context=ssl.create_default_context(cafile=Path(plan['gateway']['package'])/'certificate.pem')
        self.fronts={};self.logs=[];self.backend_process=None;self.backend_folder=None
        self.seen=set();self.backend_stopped=False;self.saved=[]

    @contextmanager
    def concurrent_requests(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:yield pool
            except BaseException:
                # Frontend readers must finish inside the original backend
                # lease, including when a client-side assertion fails.
                self.cleaning=True;signal.setitimer(signal.ITIMER_REAL,0)
                for index,front in self.fronts.items():
                    if front['process'].poll() is None:
                        self.front_cli(index,f'front-{index}-failure-stop','stop')
                        front['process'].wait(110)
                self.stop_backend()
                raise

    def launch_retained(self,kind,label,selected):
        log=(OUT/(label+'-wrapper.log')).open('xb');self.logs.append(log)
        argv=[sys.executable,'-B',ROOT/'tools/serve_retention/runner.py','run','--root',STORE,
            '--kind',kind,'--package',selected['package'],'--manifest-sha256',selected['manifest_sha256']]
        if kind=='gateway':argv+=['--backend-run',self.run_dir]
        process=subprocess.Popen(list(map(str,argv)),stdin=subprocess.DEVNULL,stdout=log,
                                 stderr=subprocess.STDOUT,start_new_session=True)
        owner=r.sup.process_identity(process.pid)
        until=time.monotonic()+10
        while time.monotonic()<until:
            self.budget();r.require(process.poll() is None,'retained wrapper exited before reservation')
            for path in (STORE/'generations').iterdir():
                if path.name in self.seen:continue
                try:value=store.read(path/'reservation.json')
                except FileNotFoundError:continue
                if value['owner']==owner:
                    self.seen.add(path.name)
                    return process,path,owner
            time.sleep(.02)
        r.sup.signal_owner(owner,signal.SIGTERM);process.wait(110)
        raise RuntimeError('retained generation reservation deadline')

    def launch(self):
        store.init(STORE,**self.plan['store_policy'])
        self.gpu_started=time.monotonic();signal.setitimer(signal.ITIMER_REAL,GPU_WINDOW)
        process,folder,owner=self.launch_retained('backend','backend',self.plan['package'])
        self.backend_process,self.backend_folder=process,folder
        self.run_dir=folder/'run'
        state=self.wait_state('READY',1)
        self.controller=r.pkg.read(self.run_dir/'controller.json')
        self.server=SimpleNamespace(pid=state['backend']['pid'])
        self.backend_owner=owner
        self.owners=[self.controller['owner'],state['owner'],state['backend']]
        lock=a.guard.common_lock();info=lock.stat()
        r.require([info.st_dev,info.st_ino]==[66305,3294067] and not a.lock_available(lock),
                  'original GPU lease not retained')
        lease=r.pkg.read(lock)
        r.require(lease['pid']==self.controller['owner']['pid'] and lease['output']==str(self.run_dir/'lease'),
                  'wrong production guard owner')
        r.pkg.save(OUT/'backend-ownership.json',dict(wrapper=owner,controller=self.controller,
            owners=self.owners,lock_identity=[info.st_dev,info.st_ino]))

    def start_front(self,index):
        r.require(index in (1,2) and index not in self.fronts,'gateway start budget')
        process,folder,owner=self.launch_retained('gateway',f'front-{index}',self.plan['gateways'][index-1])
        self.fronts[index]=dict(process=process,folder=folder,run=folder/'run',wrapper=owner,
            package=Path(self.plan['gateways'][index-1]['package']))
        state=self.wait_front(index,{'READY'})
        control=r.pkg.read(folder/'run/controller.json')
        self.fronts[index].update(owner=control['owner'],nginx=state['observation']['nginx'])
        r.pkg.save(OUT/f'front-{index}-ready.json',dict(control=control,state=state,wrapper=owner))

    def front_cli(self,index,label,action):
        r.require(action=='stop','only explicit stop allowed')
        return self.command(label,[sys.executable,'-B',ROOT/'tools/serve_retention/runner.py','stop',
            '--root',STORE,'--generation',self.fronts[index]['folder'].name])

    def wait_gone(self,index,timeout=20):
        front=self.fronts[index]
        r.require(front['process'].wait(timeout)==0,'retained gateway wrapper failed')
        r.require(not r.owner_live(front['owner']) and not r.sup.processes.group_members(front['nginx']['pid']),
                  'gateway descendants remained')
        with store.claim(front['folder']):pass
        with r.gateway.claim(front['package']):pass
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',self.keys['listen_port']))
        with store.locked(STORE) as (root,meta):
            rows=store.inventory(root,meta)
        r.require(any(v['name']==front['folder'].name and v['state']=='SEALED' for v in rows),'gateway not sealed')
        r.pkg.save(OUT/f'front-{index}-gone.json',dict(passed=True,inventory=rows,claims_available=True,port_available=True))

    def preserve(self,folder):
        with store.locked(STORE) as (root,meta):
            rows=store.inventory(root,meta)
            r.require(any(v['name']==folder.name and v['state']=='SEALED' for v in rows),'preserve only sealed logs')
            target=OUT/'saved-generations'/folder.name
            target.parent.mkdir(exist_ok=True);shutil.copytree(folder,target)
            identities={str(p.relative_to(folder)):r.pkg.sha(p) for p in folder.rglob('*') if p.is_file()}
            r.require(all(r.pkg.sha(target/n)==digest for n,digest in identities.items()),'copy changed bytes')
            self.saved.append(dict(name=folder.name,files=identities))
            r.pkg.save(OUT/'saved-generations.json',self.saved)

    def blocked_start(self):
        self.budget('negatives')
        selected=self.plan['gateways'][0]
        result=self.command('capacity-refusal',[sys.executable,'-B',ROOT/'tools/serve_retention/runner.py',
            'run','--root',STORE,'--kind','gateway','--package',selected['package'],
            '--manifest-sha256',selected['manifest_sha256'],'--backend-run',self.run_dir],expected=1)
        r.require('retention capacity exhausted' in result.stderr,'wrong refusal')
        with store.locked(STORE) as (root,meta):rows=store.inventory(root,meta)
        r.require(len(rows)==2 and all(v['state']=='ACTIVE' for v in rows),'refusal changed active generations')
        r.pkg.save(OUT/'capacity-refusal-inventory.json',rows)

    def business(self):
        self.start_front(1);self.blocked_start()
        self.plain('ordinary')
        strict=self.complete('strict',a.strict_json())
        r.require(json.loads(strict.message['content'])=={'ok':True},'strict output')
        self.tools('project');self.drain('initial-idle',4)
        self.graceful();self.drain('after-drain',5)
        old=self.fronts[1]['folder'];self.preserve(old)
        tail=store.tail(STORE,old.name,'run/gateway.log',OUT/'gateway-tail-private.bin',65536)
        r.require(tail['bytes']>0,'actual access log missing');r.pkg.save(OUT/'tail-receipt.json',tail)
        plan=store.plan(STORE);r.pkg.save(OUT/'prune-plan.json',plan)
        r.require([v['name'] for v in plan['remove']]==[old.name],'prune included active backend')
        applied=store.apply(STORE,plan);r.pkg.save(OUT/'prune-applied.json',applied)
        r.require(not old.exists() and self.backend_process.poll() is None,'explicit prune damaged backend')
        self.start_front(2)
        value=self.complete('reconnected',dict(a.ordinary(True),max_tokens=64),1)
        r.require(value.message['content'].strip()=='710003','reconnected beta quality')
        metrics=self.drain('final-idle',6)
        r.require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==3,
                  'grammar cleanup')
        r.pkg.save(OUT/'final-metrics.json',metrics)
        self.front_cli(2,'front-2-stop','stop');self.wait_front(2,{'STOPPED'});self.wait_gone(2)
        self.preserve(self.fronts[2]['folder']);self.stop_backend();self.preserve(self.backend_folder)

    def stop_backend(self):
        if self.backend_stopped or self.backend_process is None:return
        self.command('backend-stop',[sys.executable,'-B',ROOT/'tools/serve_retention/runner.py','stop',
            '--root',STORE,'--generation',self.backend_folder.name])
        r.require(self.backend_process.wait(110)==0,'retained backend wrapper failed')
        terminal=r.pkg.read(self.run_dir/'terminal.json')
        r.require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and
                  terminal['guard']['status']=='COMPLETED','backend did not stop cleanly')
        r.require((self.backend_folder/'seal.json').is_file(),'backend not sealed')
        self.backend_stopped=True

    def execute(self):
        OUT.mkdir(mode=0o700);r.pkg.save(OUT/'begin.json',dict(monotonic=self.started,unix_seconds=time.time()))
        result=dict(status='FAILED',performance_acceptance=False,long_run=False)
        def interrupted(sig,_):raise RuntimeError('validation interrupted: '+str(sig))
        previous={sig:signal.signal(sig,interrupted) for sig in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT)}
        try:
            a.require_idle(OUT/'gpu-before.json');self.launch();self.business()
            time.sleep(10)
            r.require(self.backend_process.poll()==0 and all(f['process'].poll()==0 for f in self.fronts.values()),
                      'wrapper reactivated')
            result.update(status='PASS_RETAINED_MANAGED_SHORT',model_starts=1,gateway_starts=2,
                successful_requests=6,real_tool_executions=1,sealed_generations=3,explicitly_pruned_generations=1)
        except BaseException as error:
            result.update(status='ENV_ABORT' if str(error).startswith('ENV_ABORT:') else 'FAILED',error=repr(error))
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0);self.cleaning=True
            try:
                for index,front in self.fronts.items():
                    if front['process'].poll() is None:
                        self.front_cli(index,f'front-{index}-cleanup-stop','stop');front['process'].wait(110)
                self.stop_backend()
                a.require_idle(OUT/'gpu-final.json')
                r.require(a.lock_available(a.guard.common_lock()),'GPU lease not released')
                r.require(time.monotonic()-self.started<=LIMITS['seconds'],'phase deadline')
                r.require(self.gpu_started is not None and time.monotonic()-self.gpu_started<=GPU_WINDOW,'GPU window deadline')
                r.pkg.save(OUT/'guard-archive.json',evidence.guard_archive(self.run_dir/'lease/events.jsonl',
                    r.pkg.read(self.run_dir/'lease/exit.json')))
                check();result['cleanup_confirmed']=True
            except BaseException as error:
                result.update(status='ENV_ABORT' if result['status']=='ENV_ABORT' or
                    str(error).startswith('ENV_ABORT:') else 'FAILED',cleanup_error=repr(error))
                raise
            finally:
                exit_path=self.run_dir/'lease/exit.json'
                if exit_path.exists() and r.pkg.read(exit_path)['status'] in ('ENV_ABORT','LEASE_BUSY','RECOVERY_REQUIRED'):
                    result['status']='ENV_ABORT'
                for sig,handler in previous.items():signal.signal(sig,handler)
                for log in self.logs:log.close()
                result.update(counts=self.counts,seconds=time.monotonic()-self.started)
                r.pkg.save(OUT/'result.json',result)
        return result


if __name__=='__main__':
    os.umask(0o077);configure()
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','check','run'))
    args=parser.parse_args()
    if args.action=='prepare':value=prepare()
    elif args.action=='check':check();value=dict(checked=True,started=False)
    else:
        plan=check();r.require(not OUT.exists(),'preserve previous run')
        with r.sup.processes.child_subreaper():value=Run(plan).execute()
    print(json.dumps(value))
