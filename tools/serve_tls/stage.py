"""Bounded real TLS alerts and local HTTPS delivery under one model lease."""
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
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_tls import cpu,observer as o
from tools.serve_notify import notify as n,test_notify,receiver
from tools.serve_gateway import tls_stage as prior,tls_fixture as fixture,managed as gateway
from tools.serve_service import package as pkg,supervisor as sup,acceptance as a
from tools.serve_retention import stage as retained
from tools.serve_soak import evidence
from tools.project_tasks import store,engine,transport

BASE=cpu.BASE;PLAN=BASE/'plan.json';OUT=BASE/'execution';TASKS=BASE/'tasks'
LIMITS=dict(starts=1,generations=4,negatives=0,observations=64,seconds=240)
GPU_WINDOW=210
PORTS=dict(tls=19450,prometheus=19099,alertmanager=19100,receiver=19451)
SOURCES=sorted(set((*cpu.SOURCES,*prior.SOURCES,*engine.RUNTIME_FILES,'tools/serve_tls/cpu_fix.py',
    'tools/serve_tls/stage.py','tools/serve_tls/driver_cpu.py','tools/serve_tls/report.py',
    'tools/serve_notify/receiver.py','tools/serve_notify/owned_exec.py','tools/serve_service/controller_exec.py')))


def configure():
    prior.BASE,prior.PLAN,prior.OUT,prior.TASKS=BASE,PLAN,OUT,TASKS
    prior.LIMITS,prior.GPU_WINDOW=LIMITS,GPU_WINDOW
    prior.configure()


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists() and not TASKS.exists(),'preserve prior attempt')
    for name in SOURCES:
        pkg.require(subprocess.check_output(['git','show','HEAD:'+name],cwd=ROOT)==(ROOT/name).read_bytes(),'commit source: '+name)
    first=pkg.read(BASE/'cpu-01/result.json');fixed=pkg.read(BASE/'cpu-rule-fix/result.json');driver=pkg.read(BASE/'cpu-driver/result.json')
    pkg.require(not first['passed'] and first['tests_run']==57 and first['errors']==first['failures']==0 and
        fixed['passed'] and fixed['rule_scenarios']==29 and driver['passed'],'CPU gates')
    for value in (fixed,driver):
        for name,digest in value['sources'].items():pkg.require(pkg.sha(ROOT/name)==digest,'CPU source changed: '+name)
    old_path=ROOT/'.q4t-work/serve-tls-lifecycle-20261104/plan.json';old=pkg.read(old_path)
    backend=pkg.verify(old['package']['package'],old['package']['manifest_sha256'])
    pkg.require(backend['version']==5 and backend['settings']['restart_limit']==0,'fixed backend')
    private=BASE/'private';private.mkdir(mode=0o700);ca=fixture.ca(private/'ca');now=int(time.time())
    intermediate=fixture.issue(private/'intermediate','intermediate',ca,now-60,now+40*86400,is_ca=True)
    leaf=fixture.issue(private/'leaf','leaf',intermediate,now-60,now+2*86400)
    cert=fixture.chain(private/'leaf',leaf[0],intermediate[0])
    originals=ROOT/'.q4t-work/serve-clients-managed-20261104/model-private'
    front=gateway.prepare(originals/'policy-private.json',Path(old['gateways'][0]['package'])/'nginx',cert,leaf[1],
        BASE/'gateway',originals/'clients.json',trusted_ca=ca[0])
    manifest,inner=gateway.verify(front['package'],front['manifest_sha256'])
    profiles={};bindings={};policy=pkg.read(originals/'policy-private.json');protocol=pkg.read(ROOT/'build/q4t.release.json')['chat_protocol']
    for client in policy['clients']:
        path=private/(client['id']+'.json')
        pkg.save(path,dict(client_id=client['id'],api_key=client['api_key'],connect_host='127.0.0.1',
            base_url=f'https://{policy["server_name"]}:{policy["listen_port"]}/v1',ca_file=str(ca[0]),model='qwen3.8-flash-next',
            protocol_sha256=protocol['protocol_sha256'],template_sha256=protocol['template']['reference_sha256']))
        _,bindings[client['id']]=transport.profile(path);profiles[client['id']]=dict(path=str(path),sha256=pkg.sha(path))
    observer=o.prepare(front['package'],front['manifest_sha256'],OUT/'gateway-run','thor-acceptance',
        Path(front['package'])/'nginx',PORTS['tls'],BASE/'observer')
    notify_private=private/'notification';notify_private.mkdir(mode=0o700)
    notification_policy=test_notify.private_inputs(notify_private,PORTS['receiver'])
    notification_policy.update(version=3,runner_port=backend['settings']['port'],prometheus_port=PORTS['prometheus'],
        alertmanager_port=PORTS['alertmanager'],tls_observer_port=PORTS['tls'])
    pkg.save(notify_private/'policy-private.json',notification_policy)
    notification=n.prepare(notify_private/'policy-private.json',test_notify.dependencies(),BASE/'notification')
    n.init_state(notification['package'],notification['manifest_sha256'],BASE/'monitor-state')
    pkg.save(PLAN,dict(package=old['package'],gateway=manifest['gateway'],gateways=[front],observer=observer,notification=notification,
        ca=dict(path=str(ca[0]),sha256=pkg.sha(ca[0])),profiles=profiles,bindings=bindings,default=old['default'],private=old['private'],
        days=old['days'],original_packages=old['original_packages']+old['gateways'],reused_plan=dict(path=str(old_path),sha256=pkg.sha(old_path)),
        unit='unused-tls-observation.service',token='srv10f-short',limits=LIMITS,gpu_window_seconds=GPU_WINDOW,ports=PORTS,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),source_files={name:pkg.sha(ROOT/name) for name in SOURCES},
        receipts={name:pkg.sha(BASE/name/'result.json') for name in ('cpu-01','cpu-rule-fix','cpu-driver')},
        notification_private={str(p):pkg.sha(p) for p in notify_private.iterdir() if p.is_file()},
        model_starts=1,gateway_starts=1,complete_responses=4,real_tools=2,performance_acceptance=False,long_run=False,
        external_notifications=False,reused_archive_sha256=pkg.sha(ROOT/'docs/evidence/serve-tls-lifecycle-20261104/records.tar.gz')))
    check();return dict(prepared=True,models_started=0,listeners_started=0)


def check():
    value=pkg.read(PLAN)
    pkg.require(value['limits']==LIMITS and value['gpu_window_seconds']==GPU_WINDOW and value['ports']==PORTS,'stage bounds')
    for name,digest in value['source_files'].items():pkg.require(pkg.sha(ROOT/name)==digest,'frozen source changed: '+name)
    for name,digest in {**value['default'],**value['private'],**value['days'],**value['notification_private']}.items():
        pkg.require(pkg.sha(ROOT/name)==digest,'fixed artifact changed')
    for name in value['days']:pkg.require(not (Path(name).parent/'execution').exists(),'user day executed')
    for folder,digest in value['receipts'].items():pkg.require(pkg.sha(BASE/folder/'result.json')==digest,'CPU receipt changed')
    pkg.require(pkg.sha(value['reused_plan']['path'])==value['reused_plan']['sha256'],'old plan changed')
    for v in value['original_packages']:pkg.require(pkg.sha(Path(v['package'])/'manifest.json')==v['manifest_sha256'],'old package changed')
    pkg.verify(value['package']['package'],value['package']['manifest_sha256'])
    for v in value['gateways']:
        _,inner=gateway.verify(v['package'],v['manifest_sha256'])
        remaining=gateway.tls.observe(inner['tls'])['remaining_seconds']
        pkg.require(GPU_WINDOW+3600<remaining<=7*86400,'real warning certificate lifetime')
    o.verify(value['observer']['state'],value['observer']['policy_sha256']);n.verify(value['notification']['package'],value['notification']['manifest_sha256'])
    pkg.require(pkg.sha(value['ca']['path'])==value['ca']['sha256'],'CA changed')
    for client,row in value['profiles'].items():
        pkg.require(pkg.sha(row['path'])==row['sha256'] and transport.profile(row['path'])[1]==value['bindings'][client],'profile changed')
    return value


class Run(prior.Run):
    def __init__(self,plan):
        super().__init__(plan);self.children={};self.receiver=None;self.observer_paused=False;self.api_index=0
        self.observer=Path(plan['observer']['state']);self.source_after_stop=None

    def wait(self,predicate,timeout,label,interval=.2):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.budget();value=predicate()
            if value:return value
            time.sleep(interval)
        raise RuntimeError('deadline: '+label)

    def spawn(self,label,argv):
        self.budget();pkg.require(label not in self.children,'duplicate component')
        command=[sys.executable,'-I','-B',ROOT/'tools/serve_notify/owned_exec.py','--parent',json.dumps(sup.process_identity(os.getpid())),'--',*argv]
        child=subprocess.Popen(list(map(str,command)),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            start_new_session=True,env=dict(PATH='/usr/bin:/bin',LANG='C.UTF-8',HOME=str(Path.home())),umask=0o077)
        self.pending[label]=child
        record=dict(process=child,owner=None,log=None);self.children[label]=record
        record['owner']=sup.process_identity(child.pid)
        record['log']=sup.RotatingLog(OUT/(label+'.log'),2*1024**2);record['log'].start(child.stdout)
        pkg.save(OUT/(label+'-owner.json'),dict(owner=record['owner'],argv=list(map(str,command))))
        return record

    def launch(self):
        for port in (*PORTS.values(),self.port,self.keys['listen_port']):
            with socket.socket() as sock:sock.bind(('127.0.0.1',port))
        self.gpu_started=time.monotonic();signal.setitimer(signal.ITIMER_REAL,GPU_WINDOW)
        child=self.spawn('backend',[self.manifest['python']['path'],'-I','-B',ROOT/'tools/serve_service/controller_exec.py',
            '--plan',PLAN,'--output',self.run_dir,'--parent',str(os.getpid())])
        self.backend_process=child['process'];state=self.wait_state('READY',1)
        self.controller=pkg.read(self.run_dir/'controller.json');self.backend_owner=self.controller['owner']
        pkg.require(self.backend_owner==child['owner'],'backend owner')
        self.server=SimpleNamespace(pid=state['backend']['pid']);self.owners=[self.backend_owner,state['owner'],state['backend']]
        self.assert_lease();pkg.save(OUT/'backend-ownership.json',dict(controller=self.controller,owners=self.owners))

    def assert_lease(self):
        pkg.require(self.backend_process is not None and self.backend_process.poll() is None,'backend lease owner exited')
        lock=a.guard.common_lock();info=lock.stat();value=pkg.read(lock)
        pkg.require([info.st_dev,info.st_ino]==[66305,3294067] and not a.lock_available(lock) and value['state']=='held' and
            value['pid']==self.backend_owner['pid'] and value['output']==str(self.run_dir/'lease'),'original lease not retained')

    def start_front(self,index):
        pkg.require(index==1 and not self.fronts,'gateway start budget');selected=self.plan['gateways'][0];package=Path(selected['package'])
        run=OUT/'gateway-run';child=self.spawn('gateway',[sys.executable,'-I','-B',package/'source/tools/serve_gateway/managed.py','run',
            '--package',package,'--manifest-sha256',selected['manifest_sha256'],'--backend-run',self.run_dir,'--output',run])
        self.fronts[1]=dict(process=child['process'],owner=child['owner'],run=run,package=package)
        value=self.wait_front(1,{'READY'});self.fronts[1]['nginx']=value['observation']['nginx']
        pkg.save(OUT/'gateway-ready.json',value)

    def front_cli(self,index,label,action):
        pkg.require(action=='stop','only explicit gateway stop')
        front=self.fronts[index]
        return self.command(label,[sys.executable,'-I','-B',front['package']/'source/tools/serve_gateway/managed.py','stop','--run',front['run']])

    def wait_gone(self,index):
        front=self.fronts[index];pkg.require(front['process'].wait(20)==0,'gateway did not stop cleanly')
        value=pkg.read(front['run']/'terminal.json')
        pkg.require(value['state']=='STOPPED' and value['cleanup_confirmed'] and not value['backend_stopped_by_gateway'],'gateway terminal')
        pkg.require(not sup.processes.group_members(front['nginx']['pid']),'gateway descendants remained')
        with gateway.claim(front['package']):pass
        self.source_after_stop={p.name:pkg.sha(p) for p in front['run'].iterdir() if p.is_file()}
        pkg.save(OUT/'gateway-source-after-stop.json',self.source_after_stop)

    def api(self,label,port,path,*,raw=False):
        self.budget('controls');self.assert_lease();self.api_index+=1
        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
        receipt=dict(label=label,port=port,path=path,monotonic=time.monotonic(),unix=time.time())
        try:
            connection.request('GET',path);response=connection.getresponse();data=response.read(1024**2+1)
            pkg.require(len(data)<=1024**2,'HTTP response bound');receipt.update(status=response.status,body=data.decode())
            pkg.require(response.status==200,'HTTP status: '+label)
            return data.decode() if raw else json.loads(data)
        finally:connection.close();pkg.save(OUT/f'api-{self.api_index:03d}.json',receipt)

    def wait_alert(self,name,wanted,timeout,label):
        def found():
            value=self.api(label,PORTS['prometheus'],'/api/v1/alerts')
            rows=[v for v in value['data']['alerts'] if v['labels']['alertname']==name and v['state']=='firing']
            if bool(rows)!=wanted:return None
            pkg.require(all(v['labels']['generation']==self.plan['observer']['policy_sha256'] for v in rows),'alert generation')
            pkg.save(OUT/(label+'.json'),dict(name=name,firing=wanted,response=value,monotonic=time.monotonic()))
            return True
        self.wait(found,timeout,label,5)

    def delivery(self,name,status,timeout,label):
        rows=self.wait(lambda:self.receiver.selected(name,status),timeout,label,.25)
        selected=[a for r in rows for a in r['payload']['alerts'] if a['labels']['alertname']==name and a['status']==status]
        pkg.require(all(v['labels']['generation']==self.plan['observer']['policy_sha256'] and
            set(v['labels'])<=set(('alertname','severity','component','deployment','job','instance','generation')) for v in selected),
            'delivered labels/privacy')
        pkg.save(OUT/(label+'.json'),dict(name=name,status=status,receipt_indices=[v['index'] for v in rows],monotonic=time.monotonic()))

    def projection(self,label,state):
        def stable():
            raw=(self.observer/'metrics.prom').read_bytes();value=pkg.read(self.observer/'status.json')
            if raw!=(self.observer/'metrics.prom').read_bytes():return None
            return (raw,value) if value['state']==state and sorted(raw.splitlines())==sorted(o.c.metrics(value,'thor-acceptance',
                self.plan['observer']['policy_sha256']).encode().splitlines()) else None
        raw,value=self.wait(stable,10,'consistent projection')
        (OUT/(label+'-metrics.prom')).write_bytes(raw);pkg.save(OUT/(label+'-projection.json'),value)
        wire=self.api(label+'-metrics',PORTS['tls'],'/metrics',raw=True)
        pkg.require('state="'+state+'"} 1\n' in wire and 'generation="'+self.plan['observer']['policy_sha256']+'"' in wire,'live metrics')
        return value

    def start_observation(self):
        root=BASE/'private/notification';policy=pkg.read(root/'policy-private.json')
        sink=OUT/'receiver';sink.mkdir(mode=0o700)
        self.receiver=receiver.Receiver(sink,root/'certificate.pem',root/'key.pem',Path(policy['receiver_token_file']).read_text().strip(),PORTS['receiver'])
        self.receiver.failed_once={'Q4TNotifyHeartbeat','Q4TTargetDown'};self.receiver.redirect=False
        self.spawn('observer',[sys.executable,'-I','-B',self.observer/'source/tools/serve_tls/observer.py','watch',
            '--state',self.observer,'--policy-sha256',self.plan['observer']['policy_sha256']])
        def ready():
            pkg.require(self.children['observer']['process'].poll() is None,'observer exited')
            if not (self.observer/'exporter.json').exists():return None
            value=pkg.read(self.observer/'status.json');return value if value['state']=='READY' else None
        value=self.wait(ready,15,'TLS observation READY')
        pkg.require(value['certificate']['state']=='EXPIRING','actual warning certificate')
        selected=self.plan['notification'];package=Path(selected['package'])
        for role in ('alertmanager','prometheus'):
            self.spawn(role,[sys.executable,'-I','-B',package/'source/tools/serve_notify/notify.py','run','--package',package,
                '--manifest-sha256',selected['manifest_sha256'],'--role',role,'--state',BASE/'monitor-state','--output',OUT/(role+'-run')])
            def listening(role=role):
                pkg.require(self.children[role]['process'].poll() is None,role+' exited')
                path=OUT/(role+'-run/status.json');return pkg.read(path) if path.exists() else None
            self.wait(listening,30,role+' listener')
        self.projection('running','READY')

    def pause_resume(self):
        owner=self.children['observer']['owner'];pkg.require(sup.signal_owner(owner,signal.SIGSTOP),'pause owned observer');self.observer_paused=True
        self.wait(lambda:o.source.inspection.process_row(owner['pid'])['state']=='T',3,'observer stopped')
        raw=(self.observer/'metrics.prom').read_bytes();value=pkg.read(self.observer/'status.json')
        pkg.save(OUT/'paused.json',dict(owner=owner,metrics_sha256=o.sha(raw),projection=value,monotonic=time.monotonic()))
        self.wait_alert('Q4TTLSObserverStale',True,50,'stale-alert');self.delivery('Q4TTLSObserverStale','firing',15,'stale-delivery')
        pkg.require((self.observer/'metrics.prom').read_bytes()==raw,'paused source refreshed')
        pkg.require(self.api('paused-still-served',PORTS['tls'],'/metrics',raw=True).encode()==raw,'exporter refreshed old timestamp')
        pkg.require(sup.signal_owner(owner,signal.SIGCONT),'resume observer');self.observer_paused=False
        self.wait(lambda:pkg.read(self.observer/'status.json')['evaluated_unix']>value['evaluated_unix'],5,'observer resumed')
        self.wait_alert('Q4TTLSObserverStale',False,25,'recovered-alert');self.delivery('Q4TTLSObserverStale','resolved',15,'recovered-delivery')
        self.projection('recovered','READY')

    def business(self):
        self.start_front(1);self.start_observation();store.init(TASKS,2)
        self.task('alpha',512);self.task('beta',64)
        metrics=self.drain('final-idle',4);pkg.save(OUT/'final-metrics.json',metrics)
        pkg.require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,'grammar cleanup')
        self.wait_alert('Q4TTLSCertificateExpiring',True,40,'expiring-alert')
        self.delivery('Q4TTLSCertificateExpiring','firing',15,'expiring-delivery')
        self.pause_resume()
        self.front_cli(1,'gateway-stop','stop');self.wait_front(1,{'STOPPED'});self.wait_gone(1)
        self.projection('terminal','STOPPED');self.wait_alert('Q4TTLSGenerationTerminal',True,25,'terminal-alert')
        self.delivery('Q4TTLSGenerationTerminal','firing',15,'terminal-delivery');self.assert_lease()

    def stop_backend(self):
        if self.backend_stopped or self.backend_process is None:return
        if self.backend_process.poll() is None and (self.run_dir/'controller.json').exists():
            self.service_cli('backend-stop','stop')
        pkg.require(self.backend_process.wait(60)==0,'backend exited with failure')
        value=pkg.read(self.run_dir/'terminal.json')
        pkg.require(value['state']=='STOPPED' and value['cleanup_confirmed'] and value['guard']['status']=='COMPLETED' and
            value['host_resource_cleanup']['scope_removed'],'backend terminal')
        self.backend_stopped=True;self.gpu_finished=time.monotonic()

    def stop_component(self,label):
        if label not in self.children:return
        child=self.children[label]
        if child['process'].poll() is None:
            if child['owner'] is not None:sup.signal_owner(child['owner'],signal.SIGTERM)
            else:sup.processes.stop_children({label:child['process']},{},grace=5,kill_grace=5)
        pkg.require(child['process'].wait(35)==0,'component stop failed: '+label)
        if child['log'] is not None and child['log'].thread is not None:
            child['log'].thread.join(3);pkg.require(not child['log'].thread.is_alive() and child['log'].error is None,'log cleanup: '+label)

    def reap(self,labels):
        children={k:self.pending[k] for k in labels if k in self.pending}
        readers={k:v['log'].thread for k,v in self.children.items() if k in children and
            v['log'] is not None and v['log'].thread is not None}
        sup.processes.stop_children(children,readers,grace=5,kill_grace=5)
        for name,child in children.items():
            pkg.require(child.poll() is not None and not sup.processes.group_members(child.pid),'child group remained: '+name)
            if name not in readers and child.stdout is not None:child.stdout.close()

    def execute(self):
        OUT.mkdir(mode=0o700);pkg.save(OUT/'begin.json',dict(monotonic=self.started,unix_seconds=time.time()))
        result=dict(status='FAILED',performance_acceptance=False,long_run=False,external_notifications=False)
        def interrupted(sig,_):raise RuntimeError('validation interrupted: '+str(sig))
        handlers={s:signal.signal(s,interrupted) for s in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT)}
        try:
            a.require_idle(OUT/'gpu-before.json');self.launch();self.business()
            result.update(status='PASS_TLS_OBSERVATION_SHORT',model_starts=1,gateway_starts=1,complete_responses=4,real_tools=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if str(error).startswith('ENV_ABORT:') else 'FAILED',error=repr(error));raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0);self.cleaning=True;errors=[]
            def cleanup(fn):
                try:fn()
                except BaseException as error:errors.append(repr(error))
            if self.observer_paused and 'observer' in self.children:
                cleanup(lambda:sup.signal_owner(self.children['observer']['owner'],signal.SIGCONT));self.observer_paused=False
            for label in ('gateway','observer','prometheus','alertmanager'):cleanup(lambda label=label:self.stop_component(label))
            # Even an early identity/logger failure is registered in pending.
            # Reap auxiliary groups before releasing the backend's GPU lease.
            cleanup(lambda:self.reap([k for k in self.pending if k!='backend']))
            if self.receiver is not None:cleanup(self.receiver.stop)
            # All auxiliary HTTP endpoints have stopped before the backend can
            # release the sole production GPU lease.
            cleanup(self.stop_backend)
            try:
                self.reap(list(self.pending))
                pkg.require(not errors,'owned cleanup failed: '+str(errors))
                a.require_idle(OUT/'gpu-final.json');pkg.require(a.lock_available(a.guard.common_lock()),'GPU lock retained')
                if (self.run_dir/'lease/exit.json').exists():
                    guard=pkg.read(self.run_dir/'lease/exit.json')
                    if guard['status'] in ('ENV_ABORT','LEASE_BUSY','RECOVERY_REQUIRED'):result['status']='ENV_ABORT'
                    pkg.require(guard['status']=='COMPLETED','guard terminal')
                    pkg.save(OUT/'guard-archive.json',evidence.guard_archive(self.run_dir/'lease/events.jsonl',guard))
                if self.source_after_stop is not None:
                    pkg.require(self.source_after_stop=={p.name:pkg.sha(p) for p in (OUT/'gateway-run').iterdir() if p.is_file()},'observer changed source')
                if self.gpu_started is not None:pkg.require((self.gpu_finished or time.monotonic())-self.gpu_started<=GPU_WINDOW,'GPU bound')
                pkg.require(time.monotonic()-self.started<=LIMITS['seconds'] and self.counts['controls']<=64,'stage bounds')
                check();result['cleanup_confirmed']=True
            except BaseException as error:
                result.update(status='ENV_ABORT' if result['status']=='ENV_ABORT' or str(error).startswith('ENV_ABORT:') else 'FAILED',cleanup_error=repr(error));raise
            finally:
                for sig,handler in handlers.items():signal.signal(sig,handler)
                result.update(counts=self.counts,seconds=time.monotonic()-self.started)
                pkg.save(OUT/'result.json',result)


if __name__=='__main__':
    os.umask(0o077);configure();parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','check','run'))
    action=parser.parse_args().action
    if action=='prepare':print(json.dumps(prepare()))
    elif action=='check':check();print('TLS observation stage checked; no model/HTTP started')
    else:Run(check()).execute()
