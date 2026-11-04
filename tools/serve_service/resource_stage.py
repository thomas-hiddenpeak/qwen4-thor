"""One opt-in host-bounded backend and TLS gateway; four real tool responses."""
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
from tools.serve_service import package as pkg,supervisor as sup,resources as resource,resource_cpu as cpu
from tools.serve_gateway import managed as gateway
from tools.serve_retention import stage as retained
from tools.serve_clients import managed_stage as previous
from tools.serve_service import acceptance as a
from tools.serve_soak import evidence
from tools.project_tasks import engine,store,transport
from tools.project_tasks.live_worker import QUESTION

BASE=cpu.BASE
PLAN,OUT,TASKS=BASE/'plan.json',BASE/'execution',BASE/'tasks'
LIMITS=dict(starts=1,generations=4,negatives=0,observations=64,seconds=480)
GPU_WINDOW=420
SOURCES=sorted(set((*cpu.SOURCES,*retained.SOURCES,*engine.RUNTIME_FILES,
    'tools/serve_service/resource_kernel_fix.py','tools/serve_service/resource_stage.py',
    'tools/serve_service/resource_driver_cpu.py','tools/serve_service/resource_report.py',
    'tools/project_tasks/test_stage.py','tools/project_tasks/live_worker.py')))


def configure():
    retained.BASE,retained.PLAN,retained.OUT=BASE,PLAN,OUT
    retained.STORE=BASE/'owned-generations'
    retained.LIMITS,retained.GPU_WINDOW=LIMITS,GPU_WINDOW
    retained.configure()


def kernel_gate():
    original=pkg.read(BASE/'cpu-01/result.json')
    corrected=pkg.read(BASE/'cpu-fix/result.json')
    pkg.require(original['passed'] and original['tests_run']==155 and corrected['passed'] and
        corrected['tests_run']==1,'CPU acceptance')
    pkg.require(pkg.read(BASE/'kernel-01/normal/result.json')['passed'],'normal lifecycle')
    kernel=pkg.read(BASE/'kernel-02/result.json')
    pkg.require(kernel['passed'] and [row['case'] for row in kernel['cases']]==
        ['high','oom','tasks','drift','death'],'real kernel gate')
    for name,checksum in corrected['sources'].items():
        pkg.require(pkg.sha(ROOT/name)==checksum,'kernel-tested source changed')
    return kernel


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists() and not TASKS.exists(),'preserve prior attempt')
    for name in SOURCES:
        pkg.require(subprocess.check_output(['git','show','HEAD:'+name],cwd=ROOT)==(ROOT/name).read_bytes(),
            'commit reviewed dependency: '+name)
    kernel_gate()
    driver=pkg.read(BASE/'cpu-driver/result.json');pkg.require(driver['passed'],'driver CPU gate')
    for name,checksum in driver['sources'].items():pkg.require(pkg.sha(ROOT/name)==checksum,'tested driver changed')
    previous.configure('day');old=pkg.read(previous.PLAN)
    pkg.require(not previous.OUT.exists(),'user long run already executed')
    old_backend=pkg.verify(old['package']['package'],old['package']['manifest_sha256'])
    gateway.verify(old['gateways'][0]['package'],old['gateways'][0]['manifest_sha256'])
    days={**old['old_day_plans'],str(previous.PLAN):pkg.sha(previous.PLAN)}
    configure()
    private=previous.BASE/'model-private'
    selected=resource.policy(16*1024,20*1024,512)
    backend=pkg.prepare(ROOT/'build/q4t',ROOT/'build/q4t.release.json',old_backend['model_dir'],
        BASE/'backend-package',port=old_backend['settings']['port'],restart_limit=0,
        client_policy=private/'clients.json',host_resources=selected)
    frontend=gateway.prepare(private/'policy-private.json',Path(old['gateways'][0]['package'])/'nginx',
        private/'certificate.pem',private/'key.pem',BASE/'gateway-package',private/'clients.json')
    policy=pkg.read(private/'policy-private.json');profiles={};bindings={}
    target=BASE/'private';target.mkdir(mode=0o700)
    for row in policy['clients']:
        path=target/(row['id']+'.json')
        protocol=pkg.read(ROOT/'build/q4t.release.json')['chat_protocol']
        pkg.save(path,dict(client_id=row['id'],api_key=row['api_key'],connect_host='127.0.0.1',
            base_url=f'https://{policy["server_name"]}:{policy["listen_port"]}/v1',
            ca_file=str(BASE/'gateway-package/gateway/certificate.pem'),model='qwen3.8-flash-next',
            protocol_sha256=protocol['protocol_sha256'],template_sha256=protocol['template']['reference_sha256']))
        _,bindings[row['id']]=transport.profile(path)
        profiles[row['id']]=dict(path=str(path),sha256=pkg.sha(path))
    manifest,_=gateway.verify(frontend['package'],frontend['manifest_sha256'])
    pkg.save(PLAN,dict(package=backend,gateway=manifest['gateway'],gateways=[frontend],host_resources=selected,
        unit='unused-host-envelope.service',token='srv02k-short',limits=LIMITS,gpu_window_seconds=GPU_WINDOW,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        source_files={name:pkg.sha(ROOT/name) for name in SOURCES},default=old['default'],private=old['private'],
        days=days,profiles=profiles,bindings=bindings,original_packages=[old['package'],old['gateways'][0]],
        receipts={folder:pkg.sha(BASE/folder/'result.json') for folder in ('cpu-01','cpu-fix','kernel-02','cpu-driver')},
        store_policy=dict(max_bytes=128*1024**2,max_generations=2,max_age_days=14),
        model_starts=1,gateway_starts=1,complete_responses=4,real_tools=2,performance_acceptance=False))
    check()
    return dict(prepared=True,models_started=0,listeners_started=0)


def check():
    plan=pkg.read(PLAN)
    pkg.require(plan['limits']==LIMITS and plan['gpu_window_seconds']==GPU_WINDOW,'stage bounds')
    for name,checksum in plan['source_files'].items():pkg.require(pkg.sha(ROOT/name)==checksum,'frozen source changed: '+name)
    for name,checksum in {**plan['default'],**plan['private'],**plan['days']}.items():
        pkg.require(pkg.sha(ROOT/name)==checksum,'fixed identity changed')
    for name in plan['days']:pkg.require(not (Path(name).parent/'execution').exists(),'user day executed')
    for folder,checksum in plan['receipts'].items():pkg.require(pkg.sha(BASE/folder/'result.json')==checksum,'receipt changed')
    for client,row in plan['profiles'].items():
        pkg.require(pkg.sha(row['path'])==row['sha256'] and transport.profile(row['path'])[1]==plan['bindings'][client],
            'client binding changed')
    selected=pkg.verify(plan['package']['package'],plan['package']['manifest_sha256'])
    pkg.require(selected['host_resources']==plan['host_resources']==resource.policy(16384,20480,512),'candidate policy')
    gateway.verify(plan['gateways'][0]['package'],plan['gateways'][0]['manifest_sha256'])
    pkg.verify(**dict(package=plan['original_packages'][0]['package'],digest=plan['original_packages'][0]['manifest_sha256']))
    gateway.verify(plan['original_packages'][1]['package'],plan['original_packages'][1]['manifest_sha256'])
    cert=Path(plan['gateway']['package'])/'certificate.pem'
    pkg.require(subprocess.run(['/usr/bin/openssl','x509','-checkend',str(GPU_WINDOW+3600),'-noout','-in',cert],
        capture_output=True,timeout=10).returncode==0,'certificate expiry')
    return plan


class Capture:
    def __init__(self,run):
        self.record=pkg.read(run/'resources.json');self.stopping=threading.Event();self.error=None
        self.stream=(OUT/'host-samples.jsonl').open('x',buffering=1);self.count=0;self.bytes=0
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def run(self):
        try:
            while not self.stopping.is_set():
                value=resource.sample(self.record);members=[]
                for pid in value['members']:
                    try:members.append(dict(owner=sup.process_identity(pid),membership=resource.membership(pid),
                        name=Path(f'/proc/{pid}/comm').read_text().strip()))
                    except (FileNotFoundError,ProcessLookupError):pass
                value['processes']=members
                raw=json.dumps(value)+'\n';self.bytes+=len(raw);self.count+=1
                pkg.require(self.count<=1024 and self.bytes<=2*1024**2,'host evidence bound')
                self.stream.write(raw)
                pkg.require(resource.violation(value) is None,'resource violation during business')
                self.stopping.wait(.25)
        except Exception as error:self.error=repr(error)

    def close(self):
        self.stopping.set();self.thread.join(10)
        pkg.require(not self.thread.is_alive(),'host observer did not exit')
        self.stream.close();pkg.require(self.error is None,'host observer failed: '+str(self.error))
        pkg.save(OUT/'host-capture.json',dict(passed=True,samples=self.count,bytes=self.bytes))


class Run(retained.Run):
    def __init__(self,plan):
        super().__init__(plan);self.applications=[];self.capture=None

    def business(self):
        record=pkg.read(self.run_dir/'resources.json')
        state=pkg.read(self.run_dir/'status.json')
        root=resource.verify_record(record)
        pkg.require(resource.membership(state['owner']['pid'])=='/'+str((root/'control').relative_to(resource.CGROUP)) and
            resource.membership(state['backend']['pid'])=='/'+str((root/'payload').relative_to(resource.CGROUP)) and
            not resource.membership(self.controller['owner']['pid']).startswith('/'+str(root.relative_to(resource.CGROUP))),
            'actual supervisor/backend/lease separation')
        pkg.save(OUT/'cgroup-binding.json',dict(record=record,state=state,
            controller_membership=resource.membership(self.controller['owner']['pid'])))
        self.capture=Capture(self.run_dir)
        store.init(TASKS,2);self.start_front(1)
        for client,tokens in (('alpha',512),('beta',64)):
            response=self.command(client+'-completed',[sys.executable,'-B',ROOT/'tools/project_tasks/runner.py',
                'submit','--root',TASKS,'--profile',self.plan['profiles'][client]['path'],'--key','same',
                '--question',QUESTION,'--max-requests','2','--max-tools','1','--max-tokens',str(tokens)],timeout=90)
            value=json.loads(response.stdout)
            pkg.require(value['created'] and value['state']=='COMPLETED' and value['outcome']=='ANSWERED' and
                value['requests']==2 and value['tool_attempts']==value['saved_tool_results']==1 and
                'Qwen4ExpForConditionalGeneration' in value['answer'] and '[docs/MODEL.md:L1-L24]' in value['answer'],
                'actual tool answer quality')
            with store.open_task(TASKS,store.task_name(client,'same'),exclusive=False) as ledger:
                state=engine.replay(ledger)
                pkg.require(dict(engine.public(ledger,state),created=True)==value,'ledger differs')
                pkg.save(OUT/(client+'-ledger.json'),dict(meta=ledger.meta,events=ledger.events(),
                    database_sha256=pkg.sha(ledger.path/'ledger.db')))
            self.counts['generations']+=2
            pkg.require(self.capture.error is None,'host observation failed')
        metrics=self.drain('final',4)
        pkg.require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,
            'grammar lifecycle')
        pkg.save(OUT/'final-metrics.json',metrics)
        self.close_capture()
        self.front_cli(1,'front-1-stop','stop');self.wait_front(1,{'STOPPED'});self.wait_gone(1)
        self.preserve(self.fronts[1]['folder']);self.stop_backend();self.preserve(self.backend_folder)
        final=pkg.read(self.run_dir/'resources-final.json')
        pkg.require(final['payload_removed'] and final['violation'] is None and
            pkg.read(self.run_dir/'terminal.json')['host_resource_cleanup']['scope_removed'],'resource final cleanup')

    def close_capture(self):
        capture=getattr(self,'capture',None)
        if capture is not None:
            self.capture=None;capture.close()

    def execute(self):
        OUT.mkdir(mode=0o700)
        pkg.save(OUT/'begin.json',dict(monotonic=self.started,unix_seconds=time.time()))
        result=dict(status='FAILED',performance_acceptance=False,long_run=False)
        def interrupted(signum,_):raise RuntimeError('validation interrupted: '+str(signum))
        handlers={sig:signal.signal(sig,interrupted) for sig in (signal.SIGALRM,signal.SIGINT,signal.SIGTERM)}
        try:
            a.require_idle(OUT/'gpu-before.json');self.launch();self.business()
            time.sleep(10)
            pkg.require(self.backend_process.poll()==0 and all(f['process'].poll()==0 for f in self.fronts.values()),'service restarted')
            result.update(status='PASS_HOST_ENVELOPE_SHORT',model_starts=1,gateway_starts=1,
                complete_responses=4,completed_tasks=2,real_tools=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if str(error).startswith('ENV_ABORT:') else 'FAILED',error=repr(error));raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0);self.cleaning=True
            try:
                try:self.close_capture()
                finally:
                    for child in self.applications:
                        if child['process'].poll() is None:
                            sup.signal_owner(child['owner'],signal.SIGTERM);child['process'].wait(5)
                    for index,front in self.fronts.items():
                        if front['process'].poll() is None:
                            self.front_cli(index,f'front-{index}-cleanup-stop','stop');front['process'].wait(110)
                    self.stop_backend()
                a.require_idle(OUT/'gpu-final.json')
                pkg.require(a.lock_available(a.guard.common_lock()),'GPU lock retained')
                pkg.require(time.monotonic()-self.started<=LIMITS['seconds'] and self.gpu_started is not None and
                    time.monotonic()-self.gpu_started<=GPU_WINDOW,'stage deadline')
                pkg.save(OUT/'guard-archive.json',evidence.guard_archive(self.run_dir/'lease/events.jsonl',
                    pkg.read(self.run_dir/'lease/exit.json')))
                check();result['cleanup_confirmed']=True
            except BaseException as error:
                result.update(status='ENV_ABORT' if result['status']=='ENV_ABORT' or str(error).startswith('ENV_ABORT:') else 'FAILED',
                    cleanup_error=repr(error));raise
            finally:
                path=self.run_dir/'lease/exit.json'
                if path.exists() and pkg.read(path)['status'] in ('ENV_ABORT','LEASE_BUSY','RECOVERY_REQUIRED'):result['status']='ENV_ABORT'
                for sig,handler in handlers.items():signal.signal(sig,handler)
                for log in self.logs:log.close()
                result.update(counts=self.counts,seconds=time.monotonic()-self.started);pkg.save(OUT/'result.json',result)
        return result


if __name__=='__main__':
    os.umask(0o077);configure()
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','check','run'))
    action=parser.parse_args().action
    if action=='prepare':value=prepare()
    elif action=='check':check();value=dict(checked=True,started=False)
    else:
        plan=check();pkg.require(not OUT.exists(),'preserve original run')
        with sup.processes.child_subreaper():value=Run(plan).execute()
    print(json.dumps(value))
