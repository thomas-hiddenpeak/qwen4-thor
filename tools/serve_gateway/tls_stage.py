"""One managed model, same-CA renewal, two real project tasks, bounded cleanup."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_gateway import managed as gateway,tls,tls_cpu as cpu,tls_fixture as fixture
from tools.serve_service import package as pkg,supervisor as sup,acceptance as a
from tools.serve_retention import stage as retained
from tools.serve_soak import evidence
from tools.project_tasks import store,engine,transport
from tools.project_tasks.live_worker import QUESTION

BASE=cpu.BASE;PLAN=BASE/'plan.json';OUT=BASE/'execution';TASKS=BASE/'tasks'
LIMITS=dict(starts=1,generations=4,negatives=0,observations=64,seconds=240)
GPU_WINDOW=210
STORE_POLICY=dict(max_bytes=192*1024**2,max_generations=3,max_age_days=14)
OLD=ROOT/'.q4t-work/serve-host-envelope-20261104/plan.json'
SOURCES=sorted(set((*cpu.SOURCES,*retained.SOURCES,*engine.RUNTIME_FILES,
    'tools/serve_service/test_service.py','tools/serve_gateway/tls_stage.py',
    'tools/serve_gateway/tls_driver_cpu.py','tools/serve_gateway/tls_driver_fix.py',
    'tools/serve_gateway/tls_report.py','tools/serve_host/report.py')))


def configure():
    retained.BASE,retained.PLAN,retained.OUT=BASE,PLAN,OUT
    retained.STORE=BASE/'owned-generations'
    retained.LIMITS,retained.GPU_WINDOW=LIMITS,GPU_WINDOW
    retained.configure()


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists() and not TASKS.exists(),'preserve original stage')
    for name in SOURCES:
        pkg.require(subprocess.check_output(['git','show','HEAD:'+name],cwd=ROOT)==(ROOT/name).read_bytes(),
            'commit reviewed source: '+name)
    failed=pkg.read(BASE/'cpu-driver/result.json')
    pkg.require(not failed['passed'] and failed['tests_run']==4 and failed['errors']==1 and failed['failures']==0,'original driver fixture result')
    for folder in ('cpu-01','cpu-driver-fix'):
        receipt=pkg.read(BASE/folder/'result.json');pkg.require(receipt['passed'],'CPU gate')
        for name,digest in receipt['sources'].items():pkg.require(pkg.sha(ROOT/name)==digest,'CPU source changed: '+name)
    old=pkg.read(OLD);manifest=pkg.verify(old['package']['package'],old['package']['manifest_sha256'])
    pkg.require(manifest['version']==5 and manifest['settings']['restart_limit']==0,'pinned backend policy')
    private=BASE/'private';private.mkdir(mode=0o700)
    ca=fixture.ca(private/'ca');now=int(time.time())
    intermediate=fixture.issue(private/'intermediate','intermediate',ca,now-60,now+40*86400,is_ca=True)
    original=ROOT/'.q4t-work/serve-clients-managed-20261104/model-private'
    selected=[]
    for index in (1,2):
        leaf=fixture.issue(private/f'leaf-{index}',f'leaf-{index}',intermediate,now-60,now+(index+1)*10*86400)
        cert=fixture.chain(private/f'leaf-{index}',leaf[0],intermediate[0])
        selected.append(gateway.prepare(original/'policy-private.json',Path(old['gateways'][0]['package'])/'nginx',
            cert,leaf[1],BASE/f'gateway-{index}',original/'clients.json',trusted_ca=ca[0]))
    profiles={};bindings={};policy=pkg.read(original/'policy-private.json')
    protocol=pkg.read(ROOT/'build/q4t.release.json')['chat_protocol']
    for client in policy['clients']:
        path=private/(client['id']+'.json')
        pkg.save(path,dict(client_id=client['id'],api_key=client['api_key'],connect_host='127.0.0.1',
            base_url=f'https://{policy["server_name"]}:{policy["listen_port"]}/v1',ca_file=str(ca[0]),
            model='qwen3.8-flash-next',protocol_sha256=protocol['protocol_sha256'],
            template_sha256=protocol['template']['reference_sha256']))
        _,bindings[client['id']]=transport.profile(path)
        profiles[client['id']]=dict(path=str(path),sha256=pkg.sha(path))
    # Old tools remain usable through their snapshot, without listeners/model/HTTP.
    old_front=old['gateways'][0];entry=Path(old_front['package'])/'source/tools/serve_gateway/managed.py'
    result=subprocess.run([sys.executable,'-I','-B',str(entry),'unit','--package',old_front['package'],
        '--manifest-sha256',old_front['manifest_sha256'],'--backend-run',str(BASE/'not-started'),
        '--output',str(BASE/'not-started-front')],capture_output=True,timeout=20)
    pkg.require(result.returncode==0 and b'Restart=no' in result.stdout,'old pinned entry unusable')
    pkg.save(BASE/'old-entry.json',dict(returncode=result.returncode,stdout=result.stdout.decode(),stderr=result.stderr.decode(),
        entry=str(entry),entry_sha256=pkg.sha(entry),listeners_started=0))
    first,_=gateway.verify(selected[0]['package'],selected[0]['manifest_sha256'])
    pkg.save(PLAN,dict(package=old['package'],gateway=first['gateway'],gateways=selected,profiles=profiles,bindings=bindings,
        ca=dict(path=str(ca[0]),sha256=pkg.sha(ca[0])),default=old['default'],private=old['private'],days=old['days'],
        original_packages=[old['package'],old_front],reused_plan=dict(path=str(OLD),sha256=pkg.sha(OLD)),
        unit='unused-tls-renewal.service',token='srv10e-short',limits=LIMITS,gpu_window_seconds=GPU_WINDOW,
        store_policy=STORE_POLICY,source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        source_files={n:pkg.sha(ROOT/n) for n in SOURCES},receipts={n:pkg.sha(BASE/n/'result.json') for n in ('cpu-01','cpu-driver','cpu-driver-fix')},
        model_starts=1,gateway_starts=2,complete_responses=4,real_tools=2,tls_probes=4,performance_acceptance=False,
        reused_archive_sha256=pkg.sha(ROOT/'docs/evidence/serve-host-envelope-20261104/records.tar.gz'),long_run=False))
    check();return dict(prepared=True,models_started=0,listeners_started=0)


def check():
    value=pkg.read(PLAN)
    pkg.require(value['limits']==LIMITS and value['gpu_window_seconds']==GPU_WINDOW and value['store_policy']==STORE_POLICY,'stage bounds')
    for name,digest in value['source_files'].items():pkg.require(pkg.sha(ROOT/name)==digest,'source changed: '+name)
    for name,digest in {**value['default'],**value['private'],**value['days']}.items():pkg.require(pkg.sha(ROOT/name)==digest,'fixed identity changed')
    for name in value['days']:pkg.require(not (Path(name).parent/'execution').exists(),'user day executed')
    for folder,digest in value['receipts'].items():pkg.require(pkg.sha(BASE/folder/'result.json')==digest,'CPU receipt changed')
    pkg.require(pkg.sha(value['reused_plan']['path'])==value['reused_plan']['sha256'],'original plan changed')
    pkg.verify(value['package']['package'],value['package']['manifest_sha256'])
    pkg.require(pkg.sha(value['ca']['path'])==value['ca']['sha256'],'CA changed')
    for client,row in value['profiles'].items():
        pkg.require(pkg.sha(row['path'])==row['sha256'] and transport.profile(row['path'])[1]==value['bindings'][client],'client profile changed')
    fronts=[gateway.verify(v['package'],v['manifest_sha256'])[1] for v in value['gateways']]
    pkg.require(len(fronts)==2 and fronts[0]['tls']['anchor']==fronts[1]['tls']['anchor'] and
        fronts[0]['tls']['chain'][0]['sha256']!=fronts[1]['tls']['chain'][0]['sha256'] and
        fronts[0]['client_policy']==fronts[1]['client_policy'],'renewal identity contract')
    for v in fronts:pkg.require(tls.observe(v['tls'])['remaining_seconds']>GPU_WINDOW+3600,'stage certificate lifetime')
    for row in value['original_packages']:pkg.require(pkg.sha(Path(row['package'])/'manifest.json')==row['manifest_sha256'],'old package changed')
    return value


class Run(retained.Run):
    def __init__(self,plan):
        super().__init__(plan)
        self.context=ssl.create_default_context(cafile=plan['ca']['path'])
        self.applications=[];self.pending={};self.tls_probes=0

    def launch_retained(self,kind,label,selected):
        log=(OUT/(label+'-wrapper.log')).open('xb');self.logs.append(log)
        argv=[sys.executable,'-B',ROOT/'tools/serve_retention/runner.py','run','--root',retained.STORE,
            '--kind',kind,'--package',selected['package'],'--manifest-sha256',selected['manifest_sha256']]
        if kind=='gateway':argv+=['--backend-run',self.run_dir]
        process=subprocess.Popen(list(map(str,argv)),stdin=subprocess.DEVNULL,stdout=log,
            stderr=subprocess.STDOUT,start_new_session=True)
        self.pending[label]=process
        owner=sup.process_identity(process.pid);until=time.monotonic()+10
        while time.monotonic()<until:
            self.budget();pkg.require(process.poll() is None,'wrapper exited before reservation: '+label)
            for path in (retained.STORE/'generations').iterdir():
                if path.name in self.seen:continue
                try:record=retained.store.read(path/'reservation.json')
                except FileNotFoundError:continue
                if record['owner']==owner:
                    self.seen.add(path.name);return process,path,owner
            time.sleep(.02)
        raise RuntimeError('reservation deadline: '+label)

    def probe(self,index):
        self.budget();pkg.require(self.tls_probes<self.plan['tls_probes'],'TLS probe budget');self.tls_probes+=1
        with socket.create_connection(('127.0.0.1',self.keys['listen_port']),3) as raw:
            with self.context.wrap_socket(raw,server_hostname=self.keys['server_name']) as peer:
                digest=tls.sha(peer.getpeercert(binary_form=True));protocol=peer.version();cipher=peer.cipher()
        expected=pkg.read(self.fronts[index]['package']/'gateway/manifest.json')['tls']
        pkg.require(digest==expected['chain'][0]['sha256'],'wrong served leaf')
        pkg.save(OUT/f'front-{index}-tls.json',dict(leaf_sha256=digest,ca_sha256=self.plan['ca']['sha256'],
            hostname=self.keys['server_name'],protocol=protocol,cipher=cipher,verified=True,probe_number=self.tls_probes))

    def task(self,client,tokens):
        response=self.command(client+'-completed',[sys.executable,'-B',ROOT/'tools/project_tasks/runner.py',
            'submit','--root',TASKS,'--profile',self.plan['profiles'][client]['path'],'--key','same',
            '--question',QUESTION,'--max-requests','2','--max-tools','1','--max-tokens',str(tokens)],timeout=90)
        value=json.loads(response.stdout)
        pkg.require(value['created'] and value['state']=='COMPLETED' and value['outcome']=='ANSWERED' and
            value['requests']==2 and value['tool_attempts']==value['saved_tool_results']==1 and
            'Qwen4ExpForConditionalGeneration' in value['answer'] and '[docs/MODEL.md:L1-L24]' in value['answer'],'real tool answer quality')
        with store.open_task(TASKS,store.task_name(client,'same'),exclusive=False) as ledger:
            state=engine.replay(ledger)
            pkg.require(dict(engine.public(ledger,state),created=True)==value,'ledger differs')
            pkg.save(OUT/(client+'-ledger.json'),dict(meta=ledger.meta,events=ledger.events(),database_sha256=pkg.sha(ledger.path/'ledger.db')))
        self.counts['generations']+=2;pkg.require(self.counts['generations']<=4,'generation budget')

    def business(self):
        store.init(TASKS,2);self.start_front(1);self.probe(1);self.task('alpha',512)
        self.drain('initial-idle',2)
        before=pkg.read(self.run_dir/'status.json');begin=time.monotonic()
        self.front_cli(1,'front-1-stop','stop');self.wait_front(1,{'STOPPED'});self.wait_gone(1)
        self.start_front(2);self.probe(2)
        after=pkg.read(self.run_dir/'status.json')
        pkg.require(before['backend']==after['backend'] and after['attempts']==1 and after['restarts']==0,
            'renewal changed backend')
        pkg.save(OUT/'renewal.json',dict(before_backend=before['backend'],after_backend=after['backend'],
            same_ca=True,old_gateway_stopped=True,seconds=time.monotonic()-begin,zero_downtime_claim=False))
        self.task('beta',64);metrics=self.drain('final-idle',4)
        pkg.require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,'grammar lifecycle')
        pkg.save(OUT/'final-metrics.json',metrics)
        self.front_cli(2,'front-2-stop','stop');self.wait_front(2,{'STOPPED'});self.wait_gone(2)
        self.stop_backend();self.gpu_finished=time.monotonic()

    def execute(self):
        OUT.mkdir(mode=0o700);pkg.save(OUT/'begin.json',dict(monotonic=self.started,unix_seconds=time.time()))
        result=dict(status='FAILED',performance_acceptance=False,long_run=False)
        def interrupted(signum,_):raise RuntimeError('validation interrupted: '+str(signum))
        handlers={s:signal.signal(s,interrupted) for s in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT)}
        try:
            a.require_idle(OUT/'gpu-before.json');self.launch();self.business()
            time.sleep(1)
            pkg.require(all(p.poll()==0 for p in self.pending.values()),'wrapper reactivated or failed')
            result.update(status='PASS_TLS_RENEWAL_SHORT',model_starts=1,gateway_starts=2,complete_responses=4,real_tools=2,tls_probes=self.tls_probes)
        except BaseException as error:
            result.update(status='ENV_ABORT' if str(error).startswith('ENV_ABORT:') else 'FAILED',error=repr(error));raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0);self.cleaning=True
            cleanup_errors=[]
            try:
                for index,front in self.fronts.items():
                    try:
                        if front['process'].poll() is None:
                            self.front_cli(index,f'front-{index}-cleanup-stop','stop');front['process'].wait(110)
                    except BaseException as error:cleanup_errors.append(repr(error))
                try:self.stop_backend()
                except BaseException as error:cleanup_errors.append(repr(error))
            finally:
                try:
                    # Covers a Popen that failed identity/reservation before it
                    # was entered in the frontend/backend maps above.
                    sup.processes.stop_children(self.pending,{},grace=5,kill_grace=5)
                    pkg.require(not cleanup_errors,'formal cleanup failed: '+str(cleanup_errors))
                    a.require_idle(OUT/'gpu-final.json')
                    pkg.require(a.lock_available(a.guard.common_lock()),'GPU lock retained')
                    path=self.run_dir/'lease/exit.json'
                    if path.exists():
                        guard=pkg.read(path)
                        if guard['status'] in ('ENV_ABORT','LEASE_BUSY','RECOVERY_REQUIRED'):result['status']='ENV_ABORT'
                        pkg.require(guard['status']=='COMPLETED','guard did not complete')
                        pkg.save(OUT/'guard-archive.json',evidence.guard_archive(self.run_dir/'lease/events.jsonl',guard))
                    if self.gpu_started is not None:
                        pkg.require((self.gpu_finished or time.monotonic())-self.gpu_started<=GPU_WINDOW,'GPU time bound')
                    pkg.require(time.monotonic()-self.started<=LIMITS['seconds'],'stage time bound')
                    check();result['cleanup_confirmed']=True
                except BaseException as error:
                    result.update(status='ENV_ABORT' if result['status']=='ENV_ABORT' or str(error).startswith('ENV_ABORT:') else 'FAILED',cleanup_error=repr(error));raise
                finally:
                    for sig,handler in handlers.items():signal.signal(sig,handler)
                    for log in self.logs:log.close()
                    result.update(counts=self.counts,seconds=time.monotonic()-self.started)
                    pkg.save(OUT/'result.json',result)


if __name__=='__main__':
    os.umask(0o077);configure()
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','check','run'))
    args=parser.parse_args()
    if args.action=='prepare':print(json.dumps(prepare()))
    elif args.action=='check':check();print('frozen TLS stage checked; no model/HTTP started')
    else:Run(check()).execute()
