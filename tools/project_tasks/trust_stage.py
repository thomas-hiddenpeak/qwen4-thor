"""One backend, two trust roots and explicit recovery of actual saved tools."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.project_tasks import trust_cpu as cpu,engine,store,transport,migration_fixtures as history,retention
from tools.serve_gateway import tls_stage as prior,managed as gateway,tls_fixture as fixture,tls
from tools.serve_tls import stage as owned
from tools.serve_service import package as pkg,supervisor as sup,acceptance as a
from tools.serve_soak import evidence

BASE=cpu.BASE;PLAN=BASE/'plan.json';OUT=BASE/'execution'
HISTORICAL=BASE/'historical-runtime';TASKS=HISTORICAL/'.q4t-work/tasks'
LIMITS=dict(starts=1,generations=4,negatives=2,observations=64,seconds=240);GPU_WINDOW=210
SOURCES=sorted(set((*cpu.SOURCES,*prior.SOURCES,*owned.SOURCES,'tools/serve_tls/stage.py','tools/serve_tls/observer.py','tools/serve_tls/core.py',
    'tools/serve_tls/source.py','tools/serve_tls/child.py','tools/serve_tls/cpu.py','tools/serve_tls/rules.py','tools/serve_tls/test_tls.py',
    'tools/serve_notify/owned_exec.py','tools/project_tasks/migration_worker.py','tools/project_tasks/trust_stage.py',
    'tools/project_tasks/trust_driver_cpu.py','tools/project_tasks/trust_report.py')))


def configure():
    prior.BASE,prior.PLAN,prior.OUT,prior.TASKS=BASE,PLAN,OUT,TASKS
    prior.LIMITS,prior.GPU_WINDOW=LIMITS,GPU_WINDOW;prior.configure()
    owned.OUT=OUT;owned.LIMITS=LIMITS;owned.GPU_WINDOW=GPU_WINDOW


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists() and not HISTORICAL.exists(),'preserve prior attempt')
    for name in SOURCES:pkg.require(subprocess.check_output(['git','show','HEAD:'+name],cwd=ROOT)==(ROOT/name).read_bytes(),'commit reviewed source: '+name)
    for folder,count in (('cpu-01',124),('cpu-driver',4)):
        tested=pkg.read(BASE/folder/'result.json');pkg.require(tested['passed'] and tested['tests_run']==count,'CPU gate')
        for name,digest in tested['sources'].items():pkg.require(pkg.sha(ROOT/name)==digest,'CPU source changed: '+name)
    old_path=ROOT/'.q4t-work/serve-tls-observation-20261104/plan.json';old=pkg.read(old_path)
    backend=pkg.verify(old['package']['package'],old['package']['manifest_sha256'])
    pkg.require(backend['version']==5 and backend['settings']['restart_limit']==0,'pinned no-restart backend')
    historical=history.extract_runtime(5,HISTORICAL);(HISTORICAL/'.q4t-work').mkdir(mode=0o700)
    private=HISTORICAL/'.q4t-work/private';private.mkdir(mode=0o700)
    original=ROOT/'.q4t-work/serve-clients-managed-20261104/model-private'
    gateways=[];authorities=[];profiles={};bindings={};now=int(time.time());policy=pkg.read(original/'policy-private.json')
    protocol=pkg.read(ROOT/'build/q4t.release.json')['chat_protocol']
    for index in (1,2):
        ca=fixture.ca(private/f'ca-{index}');leaf=fixture.issue(private/f'leaf-{index}','server',ca,now-60,now+10*86400)
        front=gateway.prepare(original/'policy-private.json',Path(old['gateways'][0]['package'])/'nginx',leaf[0],leaf[1],
            BASE/f'gateway-{index}',original/'clients.json',trusted_ca=ca[0]);gateways.append(front)
        authorities.append(dict(path=str(ca[0]),sha256=pkg.sha(ca[0])))
        for client in policy['clients']:
            key=client['id']+'-'+str(index);path=private/(key+'.json')
            pkg.save(path,dict(client_id=client['id'],api_key=client['api_key'],connect_host='127.0.0.1',
                base_url=f'https://{policy["server_name"]}:{policy["listen_port"]}/v1',ca_file=str(ca[0]),model='qwen3.8-flash-next',
                protocol_sha256=protocol['protocol_sha256'],template_sha256=protocol['template']['reference_sha256']))
            _,bindings[key]=transport.profile(path);profiles[key]=dict(path=str(path),sha256=pkg.sha(path))
    outer,_=gateway.verify(gateways[0]['package'],gateways[0]['manifest_sha256'])
    pkg.save(PLAN,dict(package=old['package'],gateway=outer['gateway'],gateways=gateways,ca=authorities[0],authorities=authorities,
        profiles=profiles,bindings=bindings,historical=historical,default=old['default'],private=old['private'],days=old['days'],
        original_packages=old['original_packages']+old['gateways'],reused_plan=dict(path=str(old_path),sha256=pkg.sha(old_path)),
        unit='unused-task-trust.service',token='srv11g-short',limits=LIMITS,gpu_window_seconds=GPU_WINDOW,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),source_files={n:pkg.sha(ROOT/n) for n in SOURCES},
        receipts={n:pkg.sha(BASE/n/'result.json') for n in ('cpu-01','cpu-driver')},
        historical_archive=history.release(5)['evidence'],historical_archive_sha256=history.release(5)['archive_sha256'],
        model_starts=1,gateway_starts=2,complete_responses=4,real_tools=2,application_kills=2,
        performance_acceptance=False,long_run=False))
    check();return dict(prepared=True,models_started=0,http_started=0)


def check():
    value=pkg.read(PLAN);pkg.require(value['limits']==LIMITS and value['gpu_window_seconds']==GPU_WINDOW,'stage bounds')
    for name,digest in value['source_files'].items():pkg.require(pkg.sha(ROOT/name)==digest,'frozen source changed: '+name)
    for name,digest in {**value['default'],**value['private'],**value['days']}.items():pkg.require(pkg.sha(ROOT/name)==digest,'fixed artifact changed')
    for name in value['days']:pkg.require(not (Path(name).parent/'execution').exists(),'user day executed')
    for folder,digest in value['receipts'].items():pkg.require(pkg.sha(BASE/folder/'result.json')==digest,'CPU receipt changed')
    pkg.require(pkg.sha(value['reused_plan']['path'])==value['reused_plan']['sha256'] and
        pkg.sha(ROOT/value['historical_archive'])==value['historical_archive_sha256'],'historical evidence changed')
    for row in value['original_packages']:pkg.require(pkg.sha(Path(row['package'])/'manifest.json')==row['manifest_sha256'],'old package changed')
    pkg.verify(value['package']['package'],value['package']['manifest_sha256'])
    fronts=[gateway.verify(row['package'],row['manifest_sha256'])[1] for row in value['gateways']]
    pkg.require(fronts[0]['tls']['anchor']!=fronts[1]['tls']['anchor'] and fronts[0]['client_policy']==fronts[1]['client_policy'],'trust-only gateway handoff')
    for front in fronts:pkg.require(tls.observe(front['tls'])['remaining_seconds']>GPU_WINDOW+3600,'certificate window')
    for row in value['authorities']:pkg.require(pkg.sha(row['path'])==row['sha256'],'CA changed')
    for key,row in value['profiles'].items():pkg.require(pkg.sha(row['path'])==row['sha256'] and transport.profile(row['path'])[1]==value['bindings'][key],'profile changed')
    for name,digest in value['historical']['sources'].items():pkg.require(pkg.sha(HISTORICAL/name)==digest,'historical engine changed')
    return value


class Run(prior.Run):
    spawn=owned.Run.spawn;reap=owned.Run.reap;wait=owned.Run.wait
    stop_backend=owned.Run.stop_backend;stop_component=owned.Run.stop_component;assert_lease=owned.Run.assert_lease

    def __init__(self,plan):
        super().__init__(plan);self.children={};self.negative_tls=0

    def launch(self):
        for port in (self.port,self.keys['listen_port']):
            with socket.socket() as sock:sock.bind(('127.0.0.1',port))
        self.gpu_started=time.monotonic();signal.setitimer(signal.ITIMER_REAL,GPU_WINDOW)
        child=self.spawn('backend',[self.manifest['python']['path'],'-I','-B',ROOT/'tools/serve_service/controller_exec.py',
            '--plan',PLAN,'--output',self.run_dir,'--parent',str(os.getpid())])
        self.backend_process=child['process'];state=self.wait_state('READY',1)
        self.controller=pkg.read(self.run_dir/'controller.json');self.backend_owner=self.controller['owner']
        pkg.require(self.backend_owner==child['owner'],'backend owner');self.server=SimpleNamespace(pid=state['backend']['pid'])
        self.owners=[self.backend_owner,state['owner'],state['backend']];self.assert_lease()
        pkg.save(OUT/'backend-ownership.json',dict(controller=self.controller,owners=self.owners))

    def start_front(self,index):
        pkg.require(index in (1,2) and index not in self.fronts,'gateway start budget')
        selected=self.plan['gateways'][index-1];package=Path(selected['package']);run=OUT/f'gateway-{index}-run'
        child=self.spawn('gateway-'+str(index),[sys.executable,'-I','-B',package/'source/tools/serve_gateway/managed.py','run',
            '--package',package,'--manifest-sha256',selected['manifest_sha256'],'--backend-run',self.run_dir,'--output',run])
        self.fronts[index]=dict(process=child['process'],owner=child['owner'],run=run,package=package)
        value=self.wait_front(index,{'READY'});self.fronts[index]['nginx']=value['observation']['nginx'];pkg.save(OUT/f'gateway-{index}-ready.json',value)

    def front_cli(self,index,label,action):
        pkg.require(action=='stop','explicit gateway stop');front=self.fronts[index]
        return self.command(label,[sys.executable,'-I','-B',front['package']/'source/tools/serve_gateway/managed.py','stop','--run',front['run']])

    def wait_gone(self,index):
        front=self.fronts[index];pkg.require(front['process'].wait(20)==0,'gateway did not stop cleanly');value=pkg.read(front['run']/'terminal.json')
        pkg.require(value['state']=='STOPPED' and value['cleanup_confirmed'] and not value['backend_stopped_by_gateway'],'gateway terminal')
        pkg.require(not sup.processes.group_members(front['nginx']['pid']),'gateway descendants remained')
        with gateway.claim(front['package']):pass

    def probe(self,index,*,reject=False):
        self.budget();self.assert_lease();key='alpha-'+str(1 if reject else index)
        client=transport.Client(transport.profile(self.plan['profiles'][key]['path'])[0]);failure=None;fingerprint=None
        try:
            with socket.create_connection(('127.0.0.1',self.keys['listen_port']),3) as raw:
                with client.context.wrap_socket(raw,server_hostname=self.keys['server_name']) as peer:fingerprint=tls.sha(peer.getpeercert(binary_form=True))
        except ssl.SSLCertVerificationError as error:failure=error.verify_code
        pkg.require((failure is not None)==reject and (reject or fingerprint==pkg.read(self.fronts[index]['package']/'gateway/manifest.json')['tls']['chain'][0]['sha256']),
            'real TLS trust outcome')
        self.negative_tls+=int(reject);self.tls_probes+=int(not reject)
        pkg.save(OUT/('old-ca-rejected.json' if reject else f'gateway-{index}-tls.json'),dict(rejected=reject,verify_code=failure,leaf_sha256=fingerprint,
            ca_sha256=self.plan['bindings'][key]['ca_sha256'],http_requests_sent=0,monotonic=time.monotonic()))

    def profile(self,client,index):return self.plan['profiles'][client+'-'+str(index)]['path']

    def cli(self,client,index,label,action,*extra,expected=0):
        result=self.command(client+'-'+label,[sys.executable,'-B',ROOT/'tools/project_tasks/runner.py',action,'--root',TASKS,
            '--profile',self.profile(client,index),'--key','migration-query',*extra],expected=expected,timeout=90)
        return json.loads(result.stdout if expected in (0,2) else result.stderr)

    def known(self,client,tokens):
        folder=OUT/(client+'-old');folder.mkdir(mode=0o700)
        child=self.spawn('app-'+client,[sys.executable,'-B',ROOT/'tools/project_tasks/migration_worker.py',HISTORICAL,TASKS,
            self.profile(client,1),ROOT,folder,str(tokens),prior.QUESTION])
        def saved():
            exists=(folder/'boundary.json').exists()
            pkg.require(exists or child['process'].poll() is None,'historical application exited before saved tool')
            return exists
        self.wait(saved,60,'saved actual tool')
        boundary=pkg.read(folder/'boundary.json');pkg.require(boundary['pid']==child['owner']['pid'] and boundary['kind']=='tool_result','actual saved boundary')
        pkg.require(sup.signal_owner(child['owner'],signal.SIGKILL),'kill only owned application')
        pkg.require(child['process'].wait(10)==-signal.SIGKILL,'application kill outcome');self.reap(['app-'+client])
        name=store.task_name(client,'migration-query')
        with store.open_task(TASKS,name) as ledger:
            value=engine.replay(ledger);pkg.require(value['requests']==value['tool_results']==1,'known task boundary')
            pkg.save(OUT/(client+'-known-ledger.json'),dict(meta=ledger.meta,events=ledger.events(),database_sha256=pkg.sha(ledger.path/'ledger.db')))
        pkg.save(folder/'killed.json',dict(owner=child['owner'],returncode=child['process'].returncode,monotonic=time.monotonic()))
        self.counts['generations']+=1

    def resume_task(self,client,tokens):
        denied=self.cli(client,2,'unmigrated','resume',expected=1);pkg.require(denied['code']=='binding_changed','unmigrated CA refusal')
        path=BASE/'plans'/(client+'-trust.json')
        prepared=self.cli(client,2,'plan-trust','plan-trust','--plan',path,'--reason','explicit same-service CA replacement')
        wrong=self.cli(client,2,'wrong-operation','apply-migration','--plan',path,'--plan-sha256',prepared['plan_sha256'],expected=1)
        pkg.require(wrong['code']=='plan_changed','wrong migration route refused')
        value=self.cli(client,2,'applied','apply-trust','--plan',path,'--plan-sha256',prepared['plan_sha256'])
        pkg.require(value['requests']==value['saved_tool_results']==1 and value['no_execution'],'metadata-only migration')
        duplicate=self.cli(client,2,'duplicate-apply','apply-trust','--plan',path,'--plan-sha256',prepared['plan_sha256'])
        pkg.require(duplicate['already_applied'] and duplicate['revision']==value['revision'],'duplicate apply changed task')
        completed=self.cli(client,2,'completed','resume')
        pkg.require(completed['state']=='COMPLETED' and completed['requests']==2 and completed['tool_attempts']==completed['saved_tool_results']==1 and
            'Qwen4ExpForConditionalGeneration' in completed['answer'] and '[docs/MODEL.md:L1-L24]' in completed['answer'],'real final answer/citation')
        duplicate=self.cli(client,2,'duplicate-submit','submit','--question',prior.QUESTION,'--max-requests','2','--max-tools','1','--max-tokens',str(tokens))
        pkg.require(not duplicate['created'] and duplicate['revision']==completed['revision'],'duplicate submit executed')
        with store.open_task(TASKS,store.task_name(client,'migration-query')) as ledger:
            pkg.save(OUT/(client+'-ledger.json'),dict(meta=ledger.meta,events=ledger.events(),database_sha256=pkg.sha(ledger.path/'ledger.db')))
            shutil.copytree(ledger.path,OUT/(client+'-completed-task'))
        self.counts['generations']+=1

    def archive_trust(self,client,prune):
        for kind in (('archive','prune') if prune else ('archive',)):
            path=BASE/'plans'/(client+'-'+kind+'.json')
            p=self.cli(client,2,'plan-'+kind,'plan-'+kind,'--plan',path,'--reason','retain evidence then exercise terminal contract')
            self.cli(client,2,'apply-'+kind,'apply-'+kind,'--plan',path,'--plan-sha256',p['plan_sha256'])
        before=retention.files(TASKS/'retention/payloads'/store.task_name(client,'migration-query')) if not prune else None
        path=BASE/'plans'/(client+'-archived-trust.json')
        p=self.cli(client,1,'archive-plan-trust','plan-trust','--plan',path,'--reason','metadata-only permanent key CA handoff')
        self.cli(client,1,'archive-applied','apply-trust','--plan',path,'--plan-sha256',p['plan_sha256'])
        duplicate=self.cli(client,1,'archive-duplicate-apply','apply-trust','--plan',path,'--plan-sha256',p['plan_sha256'])
        pkg.require(duplicate['already_applied'],'archive repeated migration')
        duplicate=self.cli(client,1,'archive-duplicate-submit','submit','--question',prior.QUESTION,'--max-requests','2','--max-tools','1',
            '--max-tokens','512' if client=='alpha' else '64',expected=2)
        pkg.require(duplicate['state']=='ARCHIVED' and not duplicate['created'] and duplicate['payload_state']==('purged' if prune else 'retained'),
            'permanent key duplicated task')
        resumed=self.cli(client,1,'archive-resume','resume',expected=2);pkg.require(resumed['no_execution'],'archived resume executed')
        if not prune:pkg.require(before==retention.files(TASKS/'retention/payloads'/store.task_name(client,'migration-query')),'archived payload changed')
        pkg.save(OUT/(client+'-archive-key.json'),retention.key_value(TASKS/'retention',store.task_name(client,'migration-query')))

    def business(self):
        store.init(TASKS,2);(BASE/'plans').mkdir(mode=0o700)
        self.start_front(1);self.probe(1);self.known('alpha',512);self.known('beta',64);self.drain('before-handoff',2)
        before=pkg.read(self.run_dir/'status.json')['backend']
        self.front_cli(1,'gateway-1-stop','stop');self.wait_front(1,{'STOPPED'});self.wait_gone(1)
        self.start_front(2);self.probe(2,reject=True);self.probe(2)
        pkg.require(pkg.read(self.run_dir/'status.json')['backend']==before,'backend changed across CA replacement')
        pkg.save(OUT/'handoff.json',dict(before_backend=before,after_backend=pkg.read(self.run_dir/'status.json')['backend'],no_backend_restart=True))
        self.resume_task('alpha',512);self.resume_task('beta',64)
        retention.enable(TASKS,max_keys=2,min_retain_seconds=0)
        self.archive_trust('alpha',False);self.archive_trust('beta',True)
        pkg.save(OUT/'retention-status.json',retention.status(TASKS))
        metrics=self.drain('final-idle',4);pkg.save(OUT/'final-metrics.json',metrics)
        pkg.require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,'grammar lifecycle')
        self.front_cli(2,'gateway-2-stop','stop');self.wait_front(2,{'STOPPED'});self.wait_gone(2);self.assert_lease()

    def execute(self):
        OUT.mkdir(mode=0o700);pkg.save(OUT/'begin.json',dict(monotonic=self.started,unix_seconds=time.time()))
        result=dict(status='FAILED',performance_acceptance=False,long_run=False)
        def interrupted(sig,_):raise RuntimeError('validation interrupted: '+str(sig))
        handlers={sig:signal.signal(sig,interrupted) for sig in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT)}
        try:
            a.require_idle(OUT/'gpu-before.json');self.launch();self.business()
            result.update(status='PASS_TASK_TRUST_SHORT',model_starts=1,gateway_starts=2,complete_responses=4,real_tools=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if str(error).startswith('ENV_ABORT:') else 'FAILED',error=repr(error));raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0);self.cleaning=True;errors=[]
            def cleanup(fn):
                try:fn()
                except BaseException as error:errors.append(repr(error))
            cleanup(lambda:self.reap([k for k in self.pending if k.startswith('app-')]))
            for label in ('gateway-1','gateway-2'):cleanup(lambda label=label:self.stop_component(label))
            cleanup(lambda:self.reap([k for k in self.pending if k!='backend']))
            cleanup(self.stop_backend)
            try:
                self.reap(list(self.pending));pkg.require(not errors,'owned cleanup failed: '+str(errors))
                a.require_idle(OUT/'gpu-final.json');pkg.require(a.lock_available(a.guard.common_lock()),'GPU lock retained')
                if (self.run_dir/'lease/exit.json').exists():
                    guard=pkg.read(self.run_dir/'lease/exit.json')
                    if guard['status'] in ('ENV_ABORT','LEASE_BUSY','RECOVERY_REQUIRED'):result['status']='ENV_ABORT'
                    pkg.require(guard['status']=='COMPLETED','guard terminal');pkg.save(OUT/'guard-archive.json',evidence.guard_archive(self.run_dir/'lease/events.jsonl',guard))
                if self.gpu_started is not None:pkg.require((self.gpu_finished or time.monotonic())-self.gpu_started<=GPU_WINDOW,'GPU bound')
                pkg.require(time.monotonic()-self.started<=LIMITS['seconds'] and self.counts['controls']<=64,'stage bound')
                check();result['cleanup_confirmed']=True
            except BaseException as error:
                result.update(status='ENV_ABORT' if result['status']=='ENV_ABORT' or str(error).startswith('ENV_ABORT:') else 'FAILED',cleanup_error=repr(error));raise
            finally:
                for sig,handler in handlers.items():signal.signal(sig,handler)
                result.update(counts=self.counts,seconds=time.monotonic()-self.started,tls_probes=self.tls_probes,negative_tls=self.negative_tls)
                pkg.save(OUT/'result.json',result)


if __name__=='__main__':
    os.umask(0o077);configure();parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','check','run'));action=parser.parse_args().action
    if action=='prepare':print(json.dumps(prepare()))
    elif action=='check':check();print('task trust plan checked; no model/HTTP')
    else:Run(check()).execute()
