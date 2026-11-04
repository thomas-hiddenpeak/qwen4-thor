"""One host observer/Prometheus generation and four real TLS tool responses."""
import argparse
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_host import cpu,observer as o
from tools.serve_notify import notify as n,test_notify
from tools.serve_service import package as pkg,supervisor as sup,resource_stage as prior
from tools.serve_service.lease_custody import Custody,EnvironmentAbort
from tools.serve_retention import stage as retained
from tools.project_tasks import engine,store
from tools.project_tasks.live_worker import QUESTION

BASE=cpu.BASE
PLAN,OUT,TASKS=BASE/'plan.json',BASE/'execution',BASE/'tasks'
LIMITS=dict(starts=1,generations=4,negatives=0,observations=64,seconds=240)
GPU_WINDOW=210
PORTS=dict(host=19448,prometheus=19097,alertmanager=19098,receiver=19449)
SOURCES=sorted(set((*prior.SOURCES,*cpu.SOURCES,*engine.RUNTIME_FILES,
    'tools/serve_host/cpu_fix.py','tools/serve_host/stage.py','tools/serve_host/test_stage.py',
    'tools/serve_host/runtime_cpu.py','tools/serve_host/test_runtime.py','tools/serve_host/runtime_fix.py',
    'tools/serve_host/driver_cpu.py','tools/serve_host/report.py',
    'tools/serve_service/controller_exec.py','tools/serve_service/lease_custody.py',
    'tools/serve_notify/owned_exec.py')))


def configure():
    retained.BASE,retained.PLAN,retained.OUT=BASE,PLAN,OUT
    retained.STORE=BASE/'owned-generations'
    retained.LIMITS,retained.GPU_WINDOW=LIMITS,GPU_WINDOW
    retained.configure()


def projection_matches(raw,value,deployment,digest):
    # JSON storage sorts mapping keys; Prometheus event series have no order
    # contract. Preserve multiplicity, values and labels while comparing.
    expected=o.c.metrics(value,deployment,digest).encode()
    return sorted(raw.splitlines())==sorted(expected.splitlines())


def cpu_gate():
    first=pkg.read(BASE/'cpu-01/result.json');fixed=pkg.read(BASE/'cpu-fix-02/result.json')
    pkg.require(not first['passed'] and first['tests_run']==33 and first['errors']==1 and first['failures']==0 and
        first['rule_scenarios']==22 and all(v['returncode']==0 for v in first['commands']),'original CPU evidence')
    pkg.require(fixed['passed'] and fixed['tests_run']==1 and
        fixed['reused_cpu_sha256']==pkg.sha(BASE/'cpu-01/result.json'),'targeted correction')
    runtime=pkg.read(BASE/'cpu-runtime-fix/result.json')
    pkg.require(runtime['passed'] and runtime['tests_run']==2 and
        runtime['reused_cpu_sha256']==pkg.sha(BASE/'cpu-runtime/result.json'),'runtime cleanup correction')
    for name,checksum in runtime['sources'].items():pkg.require(pkg.sha(ROOT/name)==checksum,'tested source changed: '+name)
    driver=pkg.read(BASE/'cpu-driver-02/result.json');pkg.require(driver['passed'],'driver CPU gate')
    for name,checksum in driver['sources'].items():pkg.require(pkg.sha(ROOT/name)==checksum,'tested driver changed: '+name)


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists() and not TASKS.exists(),'preserve original attempt')
    for name in SOURCES:
        pkg.require(subprocess.check_output(['git','show','HEAD:'+name],cwd=ROOT)==(ROOT/name).read_bytes(),
                    'commit reviewed source: '+name)
    cpu_gate();old=pkg.read(prior.PLAN)
    pkg.require(pkg.read(prior.BASE/'execution/result.json')['status']=='PASS_HOST_ENVELOPE_SHORT',
                'reuse only accepted backend')
    backend=pkg.verify(old['package']['package'],old['package']['manifest_sha256'])
    retained.r.gateway.verify(old['gateways'][0]['package'],old['gateways'][0]['manifest_sha256'])
    observer=o.prepare(old['package']['package'],old['package']['manifest_sha256'],OUT/'managed-run',
        'thor-host-short',Path(old['gateways'][0]['package'])/'nginx',PORTS['host'],BASE/'observer')
    private=BASE/'notify-private';private.mkdir(mode=0o700)
    policy=test_notify.private_inputs(private,PORTS['receiver'])
    policy.update(version=2,deployment='thor-host-short',runner_port=backend['settings']['port'],
        prometheus_port=PORTS['prometheus'],alertmanager_port=PORTS['alertmanager'],host_observer_port=PORTS['host'])
    pkg.save(private/'policy-private.json',policy)
    notification=n.prepare(private/'policy-private.json',test_notify.dependencies(),BASE/'notification')
    n.init_state(notification['package'],notification['manifest_sha256'],BASE/'monitor-state')
    pkg.save(PLAN,dict(package=old['package'],gateway=old['gateway'],gateways=old['gateways'],
        observer=observer,notification=notification,ports=PORTS,host_resources=old['host_resources'],
        profiles=old['profiles'],bindings=old['bindings'],default=old['default'],private=old['private'],days=old['days'],
        unit='unused-host-observation.service',token='srv12f-short',limits=LIMITS,gpu_window_seconds=GPU_WINDOW,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        source_files={name:pkg.sha(ROOT/name) for name in SOURCES},
        receipts={folder:pkg.sha(BASE/folder/'result.json') for folder in (
            'cpu-01','cpu-fix','cpu-fix-02','cpu-runtime','cpu-runtime-fix','cpu-driver','cpu-driver-02')},
        reused_plan=dict(path=str(prior.PLAN),sha256=pkg.sha(prior.PLAN)),
        reused_archive_sha256=pkg.sha(prior.ROOT/'docs/evidence/serve-host-envelope-20261104/records.tar.gz'),
        store_policy=dict(max_bytes=64*1024**2,max_generations=1,max_age_days=14),
        model_starts=1,gateway_starts=1,observer_starts=1,monitor_starts=1,complete_responses=4,real_tools=2,
        performance_acceptance=False,external_notifications=False,long_run=False))
    check();return dict(prepared=True,models_started=0,listeners_started=0)


def check():
    value=pkg.read(PLAN)
    pkg.require(value['limits']==LIMITS and value['gpu_window_seconds']==GPU_WINDOW and value['ports']==PORTS,'stage bounds')
    for name,checksum in value['source_files'].items():pkg.require(pkg.sha(ROOT/name)==checksum,'frozen source changed: '+name)
    for name,checksum in {**value['default'],**value['private'],**value['days']}.items():
        pkg.require(pkg.sha(ROOT/name)==checksum,'fixed identity changed')
    for name in value['days']:pkg.require(not (Path(name).parent/'execution').exists(),'user day executed')
    for folder,checksum in value['receipts'].items():pkg.require(pkg.sha(BASE/folder/'result.json')==checksum,'receipt changed')
    for client,row in value['profiles'].items():
        pkg.require(pkg.sha(row['path'])==row['sha256'] and
            prior.transport.profile(row['path'])[1]==value['bindings'][client],'client binding changed')
    pkg.require(pkg.sha(value['reused_plan']['path'])==value['reused_plan']['sha256'],'reused plan changed')
    pkg.verify(value['package']['package'],value['package']['manifest_sha256'])
    retained.r.gateway.verify(value['gateways'][0]['package'],value['gateways'][0]['manifest_sha256'])
    o.verify(value['observer']['state'],value['observer']['policy_sha256'])
    n.verify(value['notification']['package'],value['notification']['manifest_sha256'])
    cert=Path(value['gateway']['package'])/'certificate.pem'
    pkg.require(subprocess.run(['/usr/bin/openssl','x509','-checkend',str(GPU_WINDOW+3600),'-noout','-in',cert],
        capture_output=True,timeout=10).returncode==0,'certificate expiry')
    return value


class Run(retained.Run):
    def __init__(self,plan):
        super().__init__(plan)
        self.children={};self.custody=None;self.custody_done=False;self.api_index=0
        self.observer=Path(plan['observer']['state']);self.observer_paused=False

    def budget(self,kind=None):
        if not self.cleaning and self.custody is not None:self.custody.check()
        super().budget(kind)

    def wait(self,predicate,timeout,label,interval=.25):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.budget();value=predicate()
            if value:return value
            time.sleep(interval)
        raise RuntimeError('deadline: '+label)

    def launch(self):
        retained.store.init(retained.STORE,**self.plan['store_policy'])
        for port in (*PORTS.values(),self.port,self.keys['listen_port']):
            with socket.socket() as sock:sock.bind(('127.0.0.1',port))
        log=(OUT/'controller.log').open('xb');self.logs.append(log)
        argv=[self.manifest['python']['path'],'-I','-B',ROOT/'tools/serve_service/controller_exec.py',
            '--plan',PLAN,'--output',self.run_dir,'--parent',str(os.getpid())]
        self.gpu_started=time.monotonic();signal.setitimer(signal.ITIMER_REAL,GPU_WINDOW)
        self.backend_process=subprocess.Popen(list(map(str,argv)),stdin=subprocess.DEVNULL,
            stdout=log,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
        self.backend_owner=sup.process_identity(self.backend_process.pid)
        state=self.wait_state('READY',1)
        self.controller=pkg.read(self.run_dir/'controller.json');self.server=SimpleNamespace(pid=state['backend']['pid'])
        self.owners=[self.controller['owner'],state['owner'],state['backend']]
        pkg.require(self.controller['owner']==self.backend_owner,'controller must be direct owned child')
        lock=retained.a.guard.common_lock();info=lock.stat();lease=pkg.read(lock)
        pkg.require([info.st_dev,info.st_ino]==[66305,3294067] and not retained.a.lock_available(lock) and
            lease['state']=='held' and lease['pid']==self.backend_process.pid and lease['output']==str(self.run_dir/'lease'),
            'original production guard binding')
        folder=OUT/'custody';folder.mkdir(mode=0o700)
        def failed():
            if not self.cleaning:os.kill(os.getpid(),signal.SIGUSR1)
        self.custody=Custody(self.backend_owner,lock,folder,on_failure=failed);self.custody.start()
        pkg.save(OUT/'backend-ownership.json',dict(controller=self.controller,owners=self.owners,
            lock_identity=[info.st_dev,info.st_ino],custody_fd=self.custody.fd))

    def spawn(self,label,argv):
        self.budget();pkg.require(label not in self.children,'duplicate component')
        command=[sys.executable,'-I','-B',ROOT/'tools/serve_notify/owned_exec.py',
            '--parent',json.dumps(sup.process_identity(os.getpid())),'--',*argv]
        child=subprocess.Popen(list(map(str,command)),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,start_new_session=True,close_fds=True,umask=0o077,
            env=dict(PATH='/usr/bin:/bin',LANG='C.UTF-8',HOME=str(Path.home())))
        owner=sup.process_identity(child.pid);log=sup.RotatingLog(OUT/(label+'.log'),2*1024**2)
        self.children[label]=dict(process=child,owner=owner,log=log)
        log.start(child.stdout);pkg.save(OUT/(label+'-owner.json'),dict(owner=owner,argv=list(map(str,command))))

    def start_observation(self):
        self.spawn('observer',[sys.executable,'-I','-B',self.observer/'source/tools/serve_host/observer.py','watch',
            '--state',self.observer,'--policy-sha256',self.plan['observer']['policy_sha256']])
        def current():
            pkg.require(self.children['observer']['process'].poll() is None,'observer exited')
            if not (self.observer/'exporter.json').exists():return None
            value=pkg.read(self.observer/'status.json')
            return value if value['state']=='RUNNING' and value['kind']=='current' else None
        self.wait(current,15,'current observer')
        selected=self.plan['notification'];package=Path(selected['package'])
        self.spawn('prometheus',[sys.executable,'-I','-B',package/'source/tools/serve_notify/notify.py','run',
            '--package',package,'--manifest-sha256',selected['manifest_sha256'],'--role','prometheus',
            '--state',BASE/'monitor-state','--output',OUT/'prometheus-run'])
        def listening():
            pkg.require(self.children['prometheus']['process'].poll() is None,'Prometheus exited')
            path=OUT/'prometheus-run/status.json'
            return pkg.read(path) if path.exists() else None
        self.wait(listening,30,'Prometheus listener')
        def ready():
            pkg.require(self.children['prometheus']['process'].poll() is None,'Prometheus exited before readiness')
            try:return self.api('prometheus-ready',PORTS['prometheus'],'/-/ready',raw=True)
            except OSError:return None
            except RuntimeError as error:
                if str(error)=='HTTP status: prometheus-ready':return None
                raise
        self.wait(ready,10,'Prometheus ready',1)
        self.wait_alert('Q4THostObserverStale',False,15,'initial-alerts')
        self.projection('running','RUNNING','current')
        self.query_state('RUNNING',20)
        value=self.api('query-memory',PORTS['prometheus'],'/api/v1/query?'+urlencode(dict(
            query='q4t_host_memory_current_bytes{job="q4t-host"}')))
        rows=value.get('data',{}).get('result',[])
        pkg.require(len(rows)==1 and rows[0]['metric']['generation']==self.plan['observer']['policy_sha256'] and
                    float(rows[0]['value'][1])>0,'Prometheus current memory collection')
        pkg.save(OUT/'query-memory.json',value)

    def api(self,label,port,path,*,raw=False):
        self.budget('controls');self.api_index+=1
        pkg.require(self.custody is not None and not self.custody_done,'HTTP requires original lease custody')
        self.custody.check()
        with retained.a.guard.borrowed_lock(self.custody.fd,retained.a.guard.common_lock()):pass
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
        receipt=dict(label=label,port=port,path=path,monotonic=time.monotonic(),unix=time.time())
        try:
            conn.request('GET',path);response=conn.getresponse();data=response.read(1024**2+1)
            pkg.require(len(data)<=1024**2,'HTTP evidence bound')
            receipt.update(status=response.status,body=data.decode())
            pkg.require(response.status==200,'HTTP status: '+label)
            return data.decode() if raw else json.loads(data)
        except BaseException as error:receipt['error']=repr(error);raise
        finally:conn.close();pkg.save(OUT/f'api-{self.api_index:03d}.json',receipt)

    def wait_alert(self,name,wanted,timeout,label):
        def found():
            value=self.api(label,PORTS['prometheus'],'/api/v1/alerts')
            pkg.require(value['status']=='success','Prometheus alerts response')
            rows=[r for r in value['data']['alerts'] if r['labels']['alertname']==name and r['state']=='firing']
            if bool(rows)!=wanted:return None
            for row in rows:pkg.require(row['labels']['generation']==self.plan['observer']['policy_sha256'],'alert generation')
            pkg.save(OUT/(label+'.json'),dict(name=name,firing=wanted,response=value,monotonic=time.monotonic()))
            return True
        return self.wait(found,timeout,label,5)

    def query_state(self,state,timeout):
        query='q4t_host_state{job="q4t-host",state="'+state+'"}'
        def found():
            value=self.api('query-'+state,PORTS['prometheus'],'/api/v1/query?'+urlencode(dict(query=query)))
            rows=value.get('data',{}).get('result',[])
            if not rows:return None
            pkg.require(len(rows)==1 and rows[0]['metric']['generation']==self.plan['observer']['policy_sha256'],
                        'Prometheus source generation')
            return value if rows[0]['value'][1]=='1' else None
        value=self.wait(found,timeout,'Prometheus '+state,5);pkg.save(OUT/('query-'+state+'.json'),value)

    def projection(self,label,state,kind):
        def stable():
            raw=(self.observer/'metrics.prom').read_bytes();value=pkg.read(self.observer/'status.json')
            if raw!=(self.observer/'metrics.prom').read_bytes():return None
            return (raw,value) if projection_matches(raw,value,'thor-host-short',self.plan['observer']['policy_sha256']) and \
                value['state']==state and value['kind']==kind else None
        raw,value=self.wait(stable,10,'consistent projection')
        (OUT/(label+'-metrics.prom')).write_bytes(raw);pkg.save(OUT/(label+'-projection.json'),value)
        if kind=='current':
            actual=prior.resource.sample(pkg.read(self.run_dir/'resources.json'))
            pkg.require(value['resources']['memory.current']>0 and value['resources']['pids.current']>0 and
                actual['effective_limits']==value['resources']['effective_limits'] and prior.resource.violation(actual) is None,
                'actual live kernel resources')
            pkg.save(OUT/(label+'-kernel.json'),actual)
        wire=self.api(label+'-metrics',PORTS['host'],'/metrics',raw=True)
        pkg.require('generation="'+self.plan['observer']['policy_sha256']+'"' in wire and
                    'state="'+state+'"} 1\n' in wire,'actual exporter response')
        return value

    def tools(self):
        store.init(TASKS,2);self.start_front(1)
        for client,tokens in (('alpha',512),('beta',64)):
            response=self.command(client+'-completed',[sys.executable,'-B',ROOT/'tools/project_tasks/runner.py',
                'submit','--root',TASKS,'--profile',self.plan['profiles'][client]['path'],'--key','same',
                '--question',QUESTION,'--max-requests','2','--max-tools','1','--max-tokens',str(tokens)],timeout=75)
            value=json.loads(response.stdout)
            pkg.require(value['created'] and value['state']=='COMPLETED' and value['outcome']=='ANSWERED' and
                value['requests']==2 and value['tool_attempts']==value['saved_tool_results']==1 and
                'Qwen4ExpForConditionalGeneration' in value['answer'] and '[docs/MODEL.md:L1-L24]' in value['answer'],
                'actual tool answer quality')
            with store.open_task(TASKS,store.task_name(client,'same'),exclusive=False) as ledger:
                pkg.require(dict(engine.public(ledger,engine.replay(ledger)),created=True)==value,'ledger differs')
                pkg.save(OUT/(client+'-ledger.json'),dict(meta=ledger.meta,events=ledger.events(),
                    database_sha256=pkg.sha(ledger.path/'ledger.db')))
            self.counts['generations']+=2
        metrics=self.drain('final',4)
        pkg.require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,
                    'grammar lifecycle')
        pkg.save(OUT/'final-metrics.json',metrics)
        self.front_cli(1,'front-1-stop','stop');self.wait_front(1,{'STOPPED'});self.wait_gone(1)
        self.preserve(self.fronts[1]['folder'])

    def pause_resume(self):
        owner=self.children['observer']['owner']
        pkg.require(sup.signal_owner(owner,signal.SIGSTOP),'pause owned observer');self.observer_paused=True
        self.wait(lambda:o.source.inspection.process_row(owner['pid'])['state']=='T',3,'observer stopped')
        raw=(self.observer/'metrics.prom').read_bytes();value=pkg.read(self.observer/'status.json')
        pkg.save(OUT/'paused.json',dict(owner=owner,metrics_sha256=o.sha(raw),projection=value,monotonic=time.monotonic()))
        self.wait_alert('Q4THostObserverStale',True,48,'stale-alert')
        pkg.require((self.observer/'metrics.prom').read_bytes()==raw,'paused source was refreshed')
        wire=self.api('paused-still-served',PORTS['host'],'/metrics',raw=True)
        pkg.require(wire.encode()==raw,'exporter did not preserve original sample')
        pkg.require(sup.signal_owner(owner,signal.SIGCONT),'resume owned observer');self.observer_paused=False
        self.wait(lambda:pkg.read(self.observer/'status.json')['evaluated_unix']>value['evaluated_unix'],5,'observer resumed')
        self.wait_alert('Q4THostObserverStale',False,25,'recovered-alert')
        self.projection('recovered','RUNNING','current')

    def stop_backend(self):
        if self.backend_stopped or self.backend_process is None:return
        if self.backend_process.poll() is None:
            if (self.run_dir/'controller.json').exists():self.service_cli('backend-stop','stop')
            else:sup.signal_owner(self.backend_owner,signal.SIGTERM)
        pkg.require(self.backend_process.wait(45)==0,'backend controller exit')
        terminal=pkg.read(self.run_dir/'terminal.json')
        pkg.require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and
            terminal['guard']['status']=='COMPLETED' and terminal['host_resource_cleanup']['scope_removed'],
            'backend did not stop cleanly')
        self.backend_stopped=True

    def business(self):
        self.start_observation();self.tools();self.pause_resume();self.stop_backend()
        pkg.require(not retained.a.lock_available(retained.a.guard.common_lock()),'custody released before final HTTP')
        before={str(p.relative_to(self.run_dir)):pkg.sha(p) for p in self.run_dir.rglob('*') if p.is_file()}
        self.projection('final','STOPPED','final');self.query_state('STOPPED',20)
        self.wait_alert('Q4THostGenerationTerminal',True,15,'terminal-alert')
        after={str(p.relative_to(self.run_dir)):pkg.sha(p) for p in self.run_dir.rglob('*') if p.is_file()}
        pkg.require(before==after,'read-only observer changed ended backend evidence')
        pkg.save(OUT/'source-read-only.json',dict(passed=True,before=before,after=after))

    def stop_component(self,label):
        if label not in self.children:return
        row=self.children[label];child=row['process']
        if label=='observer' and self.observer_paused:
            sup.signal_owner(row['owner'],signal.SIGCONT);self.observer_paused=False
        if child.poll() is None:sup.signal_owner(row['owner'],signal.SIGTERM)
        sup.processes.stop_children({label:child},{label:row['log'].thread},grace=25,kill_grace=5)
        pkg.require(child.returncode==0 and row['log'].error is None,'component cleanup: '+label)
        path=self.observer/'observer-terminal.json' if label=='observer' else OUT/'prometheus-run/terminal.json'
        terminal=pkg.read(path)
        pkg.require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'],'component terminal: '+label)
        pkg.save(OUT/(label+'-exit.json'),dict(returncode=child.returncode,terminal=terminal,group_gone=True))

    def cleanup(self):
        failures=[]
        actions=[lambda:self.stop_component('prometheus'),lambda:self.stop_component('observer')]
        for index,front in self.fronts.items():
            def stop_front(index=index,front=front):
                if front['process'].poll() is None:
                    self.front_cli(index,f'front-{index}-cleanup-stop','stop');front['process'].wait(45)
            actions.append(stop_front)
        actions.append(self.stop_backend)
        for action in actions:
            try:action()
            except BaseException as error:failures.append(repr(error))
        try:retained.a.require_idle(OUT/'gpu-final.json')
        except BaseException as error:failures.append(repr(error))
        if self.custody is not None and not self.custody_done:
            try:self.custody.finish(cleanup_confirmed=not failures)
            except BaseException as error:failures.append(repr(error))
            self.custody_done=True;self.gpu_finished=time.monotonic()
        pkg.require(not failures,'cleanup failed: '+repr(failures))

    def execute(self):
        OUT.mkdir(mode=0o700);pkg.save(OUT/'begin.json',dict(monotonic=self.started,unix_seconds=time.time()))
        result=dict(status='FAILED',performance_acceptance=False,long_run=False,external_notifications=False)
        def interrupted(signum,_):
            if signum==signal.SIGUSR1:raise EnvironmentAbort('ENV_ABORT: custody monitor failed')
            raise RuntimeError('validation interrupted: '+str(signum))
        handlers={sig:signal.signal(sig,interrupted) for sig in (signal.SIGALRM,signal.SIGINT,signal.SIGTERM,signal.SIGUSR1)}
        try:
            retained.a.require_idle(OUT/'gpu-before.json');self.launch();self.business()
            result.update(status='PASS_HOST_OBSERVATION_SHORT',model_starts=1,gateway_starts=1,
                observer_starts=1,monitor_starts=1,complete_responses=4,real_tools=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if isinstance(error,EnvironmentAbort) or str(error).startswith('ENV_ABORT:') else 'FAILED',
                          error=repr(error));raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0);self.cleaning=True
            try:
                self.cleanup()
                pkg.require(retained.a.lock_available(retained.a.guard.common_lock()),'GPU lease retained')
                pkg.require(time.monotonic()-self.started<=LIMITS['seconds'] and self.gpu_started is not None and
                    self.gpu_finished is not None and self.gpu_finished-self.gpu_started<=GPU_WINDOW and
                    self.counts['controls']<=64,'stage deadline or control budget')
                check();result['cleanup_confirmed']=True
            except BaseException as error:
                result.update(status='ENV_ABORT' if result['status']=='ENV_ABORT' or isinstance(error,EnvironmentAbort) or
                    str(error).startswith('ENV_ABORT:') else 'FAILED',cleanup_error=repr(error));raise
            finally:
                path=self.run_dir/'lease/exit.json'
                if path.exists() and pkg.read(path)['status'] in ('ENV_ABORT','LEASE_BUSY','RECOVERY_REQUIRED'):result['status']='ENV_ABORT'
                for sig,handler in handlers.items():signal.signal(sig,handler)
                for log in self.logs:log.close()
                result.update(counts=self.counts,seconds=time.monotonic()-self.started,
                    gpu_started=self.gpu_started,gpu_finished=self.gpu_finished);pkg.save(OUT/'result.json',result)
        return result


if __name__=='__main__':
    os.umask(0o077);configure();parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','check','run'))
    action=parser.parse_args().action
    if action=='prepare':value=prepare()
    elif action=='check':check();value=dict(checked=True,started=False)
    else:
        plan=check();pkg.require(not OUT.exists(),'preserve original run')
        with sup.processes.child_subreaper():value=Run(plan).execute()
    print(json.dumps(value))
