"""Client-scoped managed backend + TLS gateway; short run and explicit operator day."""
import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_clients import managed_cpu as cpu
from tools.serve_gateway import managed_stage as previous, managed as m
from tools.serve_gateway import acceptance as gw, client_policy as cp
from tools.serve_gateway.test_gateway import example,VENDOR
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from tools.serve_soak import stage as soak, evidence

BASE=cpu.BASE
BUNDLE=BASE/'bundle'
PROFILES=dict(short=dict(seconds=30,cycles=2,soak_max=120,gpu_max=420,total_max=600,controls=128),
              day=dict(seconds=86400,cycles=5760,soak_max=87300,gpu_max=87720,total_max=87900,controls=60000))
PORT,TLS_PORT=18128,18448
SOURCES=sorted(set((*previous.SOURCES,*soak.SOURCES,*pkg.V4_SOURCES,
    'tools/serve_clients/managed_cpu.py','tools/serve_clients/managed_stage.py',
    'tools/serve_clients/test_managed_stage.py','docs/SERVE_CLIENT_MANAGED_2026-11-04.md')))


def configure(profile):
    global PROFILE,PLAN,OUT,LIMITS,GPU_WINDOW
    pkg.require(profile in PROFILES,'unknown managed duration')
    PROFILE=profile
    config=PROFILES[profile]
    soak.PHASE=BASE
    soak.PROFILES=copy.deepcopy(PROFILES)
    soak.configure(profile)
    PLAN,OUT=BASE/profile/'plan.json',BASE/profile/'execution'
    LIMITS=dict(starts=1,generations=config['cycles']*5+2,negatives=3,
                observations=config['controls'],seconds=config['total_max'])
    GPU_WINDOW=config['gpu_max']
    a.BASE,a.PLAN,a.OUT=BASE/profile,PLAN,OUT
    a.LIMITS,a.CONTROL_LIMIT,a.GPU_WINDOW=LIMITS,config['controls'],GPU_WINDOW
    gw.BASE,gw.PLAN,gw.OUT=BASE,PLAN,OUT
    gw.LIMITS,gw.GPU_WINDOW=LIMITS,GPU_WINDOW
    previous.BASE,previous.PLAN,previous.OUT=BASE,PLAN,OUT
    previous.LIMITS,previous.GPU_WINDOW=LIMITS,GPU_WINDOW


def certificate(directory):
    cert,key=directory/'certificate.pem',directory/'key.pem'
    result=subprocess.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048',
        '-nodes','-days','30','-subj','/CN=q4t.internal.test',
        '-addext','subjectAltName=DNS:q4t.internal.test','-keyout',str(key),'-out',str(cert)],
        capture_output=True,timeout=15)
    pkg.require(result.returncode==0,'local certificate generation failed')
    key.chmod(0o600)
    return cert,key


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(),'preserve prior managed attempt')
    pkg.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit reviewed sources')
    tested=pkg.read(BASE/'cpu-accepted/result.json')
    pkg.require(tested['passed'],'CPU gate')
    for name,digest in tested['sources'].items():
        pkg.require(pkg.sha(ROOT/name)==digest,'CPU source changed: '+name)
    if PROFILE=='short':
        pkg.require(not BUNDLE.exists(),'preserve bundle')
        private=BASE/'model-private';private.mkdir(mode=0o700)
        cert,key=certificate(private)
        front=example(TLS_PORT,PORT)
        front.update(per_client_inflight=2,global_inflight=4,per_client_rps=100,burst=128)
        pkg.save(private/'policy-private.json',front)
        pkg.save(private/'clients.json',cpu.policy())
        model=pkg.read(ROOT/'.q4t-work/serve-clients-20261104/cache-plan.json')['model_dir']
        backend=pkg.prepare(ROOT/'build/q4t',ROOT/'build/q4t.release.json',model,
                            BASE/'backend-package',port=PORT,client_policy=private/'clients.json')
        nginx=pkg.read(VENDOR)['binary']
        good=m.prepare(private/'policy-private.json',nginx,cert,key,BASE/'gateway-package',private/'clients.json')
        wrong=pkg.read(private/'clients.json');wrong['gateway_token']=secrets.token_hex(32)
        pkg.save(private/'wrong-clients.json',wrong)
        mismatch=m.prepare(private/'policy-private.json',nginx,cert,key,
                           BASE/'gateway-mismatch-package',private/'wrong-clients.json')
        unscoped=m.prepare(private/'policy-private.json',nginx,cert,key,BASE/'gateway-unscoped-package')
        BUNDLE.mkdir(mode=0o700)
        pkg.save(BUNDLE/'receipt.json',dict(package=backend,gateways=[good,good],
            rejected_gateways=[mismatch,unscoped],gateway=pkg.read(Path(good['package'])/'manifest.json')['gateway'],
            private={str(p):pkg.sha(p) for p in private.iterdir() if p.is_file()},
            default={name:pkg.sha(ROOT/name) for name in ('build/q4t','build/q4t.release.json')},
            client_policy=cp.binding((private/'clients.json').read_bytes()),
            actual_lan_deployment=False,test_certificate_only=True))
    else:
        accepted=pkg.read(BASE/'short/execution/result.json')
        pkg.require(accepted['status']=='PASS_MANAGED_CLIENT_SHORT' and accepted['cleanup_confirmed'],
                    'short managed acceptance required before day handoff')
    receipt=pkg.read(BUNDLE/'receipt.json')
    token=secrets.token_hex(10)
    PLAN.parent.mkdir(exist_ok=True)
    old=pkg.read(ROOT/'.q4t-work/serve-clients-20261104/cache-plan.json')
    config=PROFILES[PROFILE]
    plan=dict(receipt,version=1,profile=PROFILE,limits=LIMITS,source_commit=subprocess.check_output(
        ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),source_files={name:pkg.sha(ROOT/name) for name in SOURCES},
        cpu_result_sha256=pkg.sha(BASE/'cpu-accepted/result.json'),bundle_sha256=pkg.sha(BUNDLE/'receipt.json'),
        unit='q4t-clients-'+PROFILE+'-'+token+'.service',token=token,
        cycles=config['cycles'],soak_seconds=config['seconds'],gpu_window_seconds=GPU_WINDOW,
        supervisor_http_upper_bound=4*(GPU_WINDOW//5+3),resource_envelope=evidence.ENVELOPE,
        warmup=evidence.WARMUP,first_hour_checkpoint_cycles=240,interval_seconds=15,
        old_day_plans={p:pkg.sha(p) for p in old['day_plans']},
        no_automatic_request_retry=True,no_backend_restart=True,operator_only=PROFILE=='day')
    if PROFILE=='day':
        plan['short_acceptance_sha256']=pkg.sha(BASE/'short/execution/result.json')
    pkg.save(PLAN,plan)
    check()
    return dict(prepared=True,profile=PROFILE,plan=str(PLAN),models_started=0)


def check():
    plan=pkg.read(PLAN)
    pkg.require(plan['profile']==PROFILE and plan['limits']==LIMITS and
        plan['cycles']==PROFILES[PROFILE]['cycles'] and plan['soak_seconds']==PROFILES[PROFILE]['seconds'] and
        plan['gpu_window_seconds']==GPU_WINDOW and plan['resource_envelope']==evidence.ENVELOPE and
        plan['warmup']==evidence.WARMUP and plan['first_hour_checkpoint_cycles']==240 and
        plan['interval_seconds']==15 and plan['no_automatic_request_retry'] is True and
        plan['no_backend_restart'] is True,'managed contract changed')
    for name,digest in plan['source_files'].items():
        pkg.require(pkg.sha(ROOT/name)==digest,'managed source changed: '+name)
    pkg.require(pkg.sha(BASE/'cpu-accepted/result.json')==plan['cpu_result_sha256'] and
                pkg.sha(BUNDLE/'receipt.json')==plan['bundle_sha256'],'CPU/bundle identity')
    for name,digest in plan['private'].items():
        pkg.require(pkg.sha(name)==digest,'private fixture changed')
    for name,digest in plan['default'].items():
        pkg.require(pkg.sha(ROOT/name)==digest,'default changed')
    for name,digest in plan['old_day_plans'].items():
        pkg.require(pkg.sha(name)==digest and not (Path(name).parent/'execution').exists(),'old day changed')
    manifest=pkg.verify(plan['package']['package'],plan['package']['manifest_sha256'])
    pkg.require(manifest['version']==4 and manifest['client_policy']==plan['client_policy'] and
                manifest['settings']['restart_limit']==0,'backend policy contract')
    for selected in [*plan['gateways'],*plan['rejected_gateways']]:
        m.verify(selected['package'],selected['manifest_sha256'])
    cert=Path(plan['gateway']['package'])/'certificate.pem'
    expiry=subprocess.run(['/usr/bin/openssl','x509','-checkend',str(GPU_WINDOW+3600),'-noout','-in',str(cert)],
                          capture_output=True,timeout=10)
    pkg.require(expiry.returncode==0,'certificate would expire during planned run; prepare new explicit bundle')
    if PROFILE=='day':
        pkg.require(pkg.sha(BASE/'short/execution/result.json')==plan['short_acceptance_sha256'],'short acceptance changed')
    return plan


def require_operator(profile,confirmed):
    pkg.require(profile!='day' or confirmed,'day execution requires --operator-run-day')


class Run(soak.Run):
    wire=gw.Run.wire
    metrics=gw.Run.metrics
    concurrent_requests=gw.Run.concurrent_requests
    wait_front=previous.Run.wait_front
    front_cli=previous.Run.front_cli
    graceful=previous.Run.graceful

    def __init__(self,plan):
        super().__init__(plan)
        self.keys=m.g.parse(m.g.read_file(BASE/'model-private/policy-private.json',private=True))
        self.context=ssl.create_default_context(cafile=Path(plan['gateway']['package'])/'certificate.pem')
        self.fronts={};self.front_logs=[];self.backend_stopped=False

    def complete(self,label,body,client=0):
        with self.mutex:
            pkg.require(body['cancel_token'] not in self.cancel_tokens,'cancel credentials reused')
            self.cancel_tokens.add(body['cancel_token'])
        return gw.Run.complete(self,label,body,client)

    def launch(self):
        super().launch()
        pkg.require(self.lock_stat==[66305,3294067],'original GPU lock changed')

    def start_front(self,index):
        pkg.require(index<=2 and len(self.fronts)<2,'gateway generation budget')
        previous.Run.start_front(self,index,unit=False)

    def wait_gone(self,index,timeout=15):
        front=self.fronts[index];until=time.monotonic()+timeout
        while time.monotonic()<until:
            if not self.cleaning:self.budget()
            front['process'].poll()
            if not m.alive(front['owner']) and not sup.processes.group_members(front['nginx']['pid']):
                break
            time.sleep(.05)
        else:raise RuntimeError('gateway descendants remained')
        with m.claim(front['package']):pass
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            probe.bind(('127.0.0.1',TLS_PORT))
        pkg.save(OUT/f'front-{index}-gone.json',dict(passed=True,claim_available=True,port_available=True))

    def rejected_fronts(self):
        for index,selected in enumerate(self.plan['rejected_gateways']):
            self.budget('negatives')
            output=OUT/f'rejected-front-{index}'
            result=self.command(f'rejected-front-{index}',[self.manifest['python']['path'],'-I','-B',
                Path(selected['package'])/'source/tools/serve_gateway/managed.py','run',
                '--package',selected['package'],'--manifest-sha256',selected['manifest_sha256'],
                '--backend-run',self.run_dir,'--output',output],expected=1)
            terminal=m.read(output/'terminal.json')
            pkg.require(terminal['state']=='FAILED' and terminal['cleanup_confirmed'] and
                'client policy differs' in terminal['error'] and not (output/'status.json').exists(),
                'wrong-policy gateway reached child start')
            with m.claim(Path(selected['package'])):pass
            pkg.require(self.observe_status()['backend']['pid']==self.server.pid,'negative stopped backend')

    def preflight(self,label,*,too_large=False):
        self.budget('negatives' if too_large else 'controls')
        body=a.ordinary()
        if too_large:body['max_tokens']=65
        else:body.pop('max_tokens')
        envelope=dict(request=body,include_token_ids=False)
        pkg.save(OUT/(label+'-request.json'),envelope)
        connection=gw.TLSConnection(self.keys['server_name'],TLS_PORT,context=self.context,timeout=15)
        try:
            connection.request('POST','/v1/chat/preflight',json.dumps(envelope),
                {'Content-Type':'application/json','Authorization':'Bearer '+self.keys['clients'][1]['api_key']})
            response=connection.getresponse();raw=response.read(1024**2+1)
            pkg.require(len(raw)<=1024**2,'preflight byte bound')
            value=json.loads(raw)
            pkg.save(OUT/(label+'-response.json'),dict(status=response.status,body=value))
            if too_large:
                pkg.require(response.status==400 and value['error']['code']=='client_output_limit_exceeded','client budget not enforced')
            else:
                pkg.require(response.status==200 and value['output']['requested_tokens']==64 and
                    value['client_limits']['max_output_tokens']==64,'gateway mapped wrong client budget')
        finally:connection.close()

    def drain(self,label,expected):
        until=time.monotonic()+5
        for number in range(40):
            values=self.metrics(label+f'-{number:02d}')
            raw=pkg.read(OUT/(label+f'-{number:02d}.json'))['response']['body']
            clients=[float(line.rsplit(' ',1)[1]) for line in raw.splitlines()
                     if line.startswith('q4t_client_requests_active{')]
            if all(values[key]==0 for key in a.EMPTY) and clients and not any(clients):
                a.healthy_idle(values,expected)
                return values
            pkg.require(time.monotonic()<until,'request cleanup deadline')
            time.sleep(.05)
        raise RuntimeError('request cleanup observation budget')

    def cycle(self,number):
        label=f'cycle-{number:04d}';folder=OUT/label;folder.mkdir()
        self.preflight(label+'/preflight')
        beta=a.ordinary(True);beta['max_tokens']=64
        with self.concurrent_requests() as pool:
            first=pool.submit(self.complete,label+'/plain',a.ordinary(),0)
            second=pool.submit(self.complete,label+'/stream',beta,1)
            values=[first.result(),second.result()]
        pkg.require(all(v.finish_reason=='stop' and v.message['content'].strip()=='710003' for v in values),'two-client quality')
        body=a.strict_json();body['stream']=bool(number%2)
        if body['stream']:body['stream_options']=dict(include_usage=True)
        value=self.complete(label+'/strict',body)
        pkg.require(value.finish_reason=='stop' and json.loads(value.message['content'])=={'ok':True},'strict quality')
        self.tools(label+'/project')
        metrics=self.drain(label+'/idle',number*5)
        pkg.require(metrics['q4t_structured_workers_started_total']==
            metrics['q4t_structured_workers_reaped_total']==number*3,'grammar lifetime accounting')
        self.control_get(label+'/live','/livez');self.control_get(label+'/ready','/readyz')
        sample=evidence.observe.sample(self.server,self.expected_start)
        pkg.save(folder/'quiescent.json',sample)
        self.resources.append(evidence.values(sample))
        if number>evidence.WARMUP:evidence.envelope(self.resources)

    def stop_backend(self):
        if self.backend_stopped or not (self.run_dir/'controller.json').exists():return
        if self.capture is not None:self.capture.detach()
        if not (self.run_dir/'terminal.json').exists():
            self.service_cli('explicit-backend-stop','stop')
            until=time.monotonic()+50
            while not (self.run_dir/'terminal.json').exists() and time.monotonic()<until:
                time.sleep(.05)
        terminal=pkg.read(self.run_dir/'terminal.json')
        pkg.require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and
            terminal['guard']['status']=='COMPLETED' and terminal['worker']['attempts']==1 and
            terminal['worker']['restarts']==0,'backend stop failed')
        self.backend_stopped=True

    def business(self):
        self.rejected_fronts();self.start_front(1)
        self.preflight('beta-over-budget',too_large=True)
        self.soak()
        self.graceful()
        self.drain('after-drain',soak.CYCLES*5+1)
        self.start_front(2)
        beta=a.ordinary(True);beta['max_tokens']=64
        value=self.complete('reconnected-beta',beta,1)
        pkg.require(value.message['content'].strip()=='710003','reconnected client quality')
        pkg.save(OUT/'final-metrics.json',self.drain('final-idle',soak.CYCLES*5+2))
        self.front_cli(2,'front-2-stop','stop')
        terminal=self.wait_front(2,{'STOPPED'},timeout=15)
        pkg.require(terminal['cleanup_confirmed'] and terminal['nginx_returncode']==0,
                    'second gateway cleanup failed')
        self.wait_gone(2)
        # Keep every HTTP listener and frontend cleanup inside the original
        # production controller lease. systemd controllers are not our child;
        # the direct-child-only pidfd custody helper does not apply here.
        self.stop_backend()

    def execute(self):
        OUT.mkdir(mode=0o700)
        pkg.save(OUT/'driver.json',sup.process_identity(os.getpid()))
        pkg.save(OUT/'begin.json',dict(monotonic=self.started,unix_seconds=time.time()))
        result=dict(status='FAILED',profile=PROFILE,long_run=PROFILE=='day',performance_acceptance=False)
        def interrupted(signum,_):raise evidence.EvidenceAbort('driver interrupted: '+str(signum))
        previous_signals={s:signal.signal(s,interrupted) for s in
                          (signal.SIGALRM,signal.SIGUSR1,signal.SIGTERM,signal.SIGINT)}
        try:
            a.require_idle(OUT/'gpu-before.json');self.launch();self.business()
            time.sleep(10)
            pkg.require(all(not m.alive(f['owner']) and not sup.processes.group_members(f['nginx']['pid'])
                            for f in self.fronts.values()),'gateway restarted')
            pkg.require(len(list(self.run_dir.glob('attempt-*.json')))==1,'backend restarted')
            secret=pkg.read(BASE/'model-private/clients.json')['gateway_token']
            for path in [self.run_dir/'backend.log',*OUT.glob('front-*/gateway.*')]:
                raw=path.read_bytes()
                pkg.require(secret.encode() not in raw and
                    all(c['api_key'].encode() not in raw for c in self.keys['clients']),'secret in runtime log')
            result.update(status='PASS_MANAGED_CLIENT_'+PROFILE.upper(),model_starts=1,
                gateway_starts=2,rejected_gateway_starts=2,successful_requests=soak.CYCLES*5+2,
                real_tool_executions=soak.CYCLES,day_scale_stability_proved=PROFILE=='day')
        except BaseException as error:
            result.update(status='ENV_ABORT' if isinstance(error,evidence.EvidenceAbort)
                          else 'FAILED',error=repr(error))
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0);self.cleaning=True
            signal.signal(signal.SIGUSR1,signal.SIG_IGN)
            errors=[]
            try:
                for index,front in self.fronts.items():
                    try:
                        if (front['run']/'controller.json').exists() and m.alive(m.read(front['run']/'controller.json')['owner']):
                            self.front_cli(index,f'front-{index}-cleanup-stop','stop')
                        if front['process'] is not None:front['process'].wait(50)
                        if front.get('nginx'):self.wait_gone(index)
                    except BaseException as error:errors.append(repr(error))
                try:self.stop_backend()
                except BaseException as error:errors.append(repr(error))
                try:self.cleanup()
                except BaseException as error:errors.append(repr(error))
                if self.capture is not None:
                    try:self.capture.close()
                    except BaseException as error:errors.append(repr(error))
                pkg.require(not errors,'owned cleanup failed: '+repr(errors))
                a.require_idle(OUT/'gpu-final.json')
                self.gpu_finished=time.monotonic()
                pkg.require(a.lock_available(a.guard.common_lock()),'GPU lock retained')
                pkg.require(self.gpu_started is not None and self.gpu_finished-self.gpu_started<=GPU_WINDOW and
                    time.monotonic()-self.started<=LIMITS['seconds'],'time budget exceeded')
                pkg.save(OUT/'guard-archive.json',evidence.guard_archive(OUT/'capture/events.jsonl',
                    pkg.read(self.run_dir/'lease/exit.json')))
                check()
                result['cleanup_confirmed']=True
            except BaseException as error:
                result.update(status='ENV_ABORT' if result['status']=='ENV_ABORT' else 'FAILED',cleanup_error=repr(error))
                raise
            finally:
                for sig,handler in previous_signals.items():signal.signal(sig,handler)
                for log in self.front_logs:log.close()
                result.update(counts=self.counts,elapsed_seconds=time.monotonic()-self.started,
                    gpu_window_elapsed=None if self.gpu_finished is None or self.gpu_started is None else self.gpu_finished-self.gpu_started,
                    supervisor_http_upper_bound=self.plan['supervisor_http_upper_bound'])
                pkg.save(OUT/'result.json',result)
        return result


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','check','run','stop'))
    parser.add_argument('--profile',choices=tuple(PROFILES),default='short')
    parser.add_argument('--operator-run-day',action='store_true')
    args=parser.parse_args();configure(args.profile)
    if args.action=='prepare':value=prepare()
    elif args.action=='stop':
        owner=pkg.read(OUT/'driver.json')
        value=dict(signal_sent=sup.signal_owner(owner,signal.SIGTERM))
    else:
        plan=check()
        if args.action=='run':
            require_operator(args.profile,args.operator_run_day)
            pkg.require(not OUT.exists(),'preserve previous execution')
            with sup.processes.child_subreaper():value=Run(plan).execute()
        else:value=dict(checked=True,profile=PROFILE,started=False)
    print(json.dumps(value))


if __name__=='__main__':
    main()
