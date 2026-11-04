"""One leased model, four explicit gateway lifetimes, bounded real TLS work."""
import argparse
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_gateway import managed as m,managed_cpu as cpu,acceptance as gw
from tools.serve_gateway.test_gateway import certificate,example,VENDOR
from tools.serve_service import acceptance as a,package as pkg,supervisor as sup,lease_custody
from chat_client import ProtocolError

BASE=cpu.BASE;PLAN=BASE/'plan.json';OUT=BASE/'execution'
LIMITS=dict(starts=1,gateway_starts=4,generations=8,negatives=0,observations=128,seconds=420)
GPU_WINDOW=360
SOURCES=sorted(set((*cpu.SOURCES,*gw.SOURCES,'tools/serve_gateway/managed_stage.py',
                    'tools/serve_service/lease_custody.py')))


def configure():
    gw.BASE,gw.PLAN,gw.OUT=BASE,PLAN,OUT
    gw.LIMITS,gw.GPU_WINDOW=LIMITS,GPU_WINDOW
    gw.configure()


def prepare():
    m.g.require(not PLAN.exists() and not OUT.exists(),'preserve original stage')
    for name in SOURCES:
        m.g.require(subprocess.check_output(['git','show','HEAD:'+name])==(ROOT/name).read_bytes(),
                    'commit reviewed dependency first: '+name)
    tested=pkg.read(BASE/'cpu-corrected/result.json')
    m.g.require(tested['passed'] and tested['effective_tests']==18,'CPU acceptance missing')
    for name,digest in tested['sources'].items():m.g.require(pkg.sha(ROOT/name)==digest,'CPU source changed')
    accepted=pkg.read(ROOT/'.q4t-work/serve-inspection-20261103/accepted-package.json')['package']
    backend=pkg.verify(accepted['package'],accepted['manifest_sha256'])
    m.g.require(backend['settings']['restart_limit']==0,'backend restart not allowed')
    private=BASE/'model-private';private.mkdir(mode=0o700)
    cert,key=certificate(private)
    policy=example(port=18444,upstream=backend['settings']['port'])
    policy.update(per_client_inflight=2,global_inflight=4,per_client_rps=100,burst=128)
    path=private/'policy-private.json';m.g.save(path,json.dumps(policy).encode())
    gateways=[]
    for index in range(1,5):
        gateways.append(m.prepare(path,pkg.read(VENDOR)['binary'],cert,key,BASE/f'package-{index}'))
    gateway=pkg.read(Path(gateways[0]['package'])/'manifest.json')['gateway']
    release=pkg.read(ROOT/'build/q4t.release.json')
    for name,digest in pkg.read(release['tested_source_manifest']).items():
        m.g.require(pkg.sha(ROOT/name)==digest,'runtime source changed')
    pkg.save(PLAN,dict(version=1,limits=LIMITS,gpu_window_seconds=GPU_WINDOW,
        package=accepted,gateway=gateway,gateways=gateways,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        source_files={n:pkg.sha(ROOT/n) for n in SOURCES},
        cpu_sha256=pkg.sha(BASE/'cpu-corrected/result.json'),
        private_policy_sha256=pkg.sha(path),default_release_sha256=pkg.sha(ROOT/'build/q4t.release.json'),
        unit='q4t-gateway-10b-20261104.service',token='srv10b-explicit-short',
        supervisor_http_upper_bound=300,performance_acceptance=False,long_test_started=False))
    return dict(prepared=True,model_or_http_started=False,limits=LIMITS)


def check():
    plan=pkg.read(PLAN)
    m.g.require(plan['limits']==LIMITS and plan['gpu_window_seconds']==GPU_WINDOW,'budget changed')
    for name,digest in plan['source_files'].items():m.g.require(pkg.sha(ROOT/name)==digest,'source changed: '+name)
    m.g.require(pkg.sha(BASE/'cpu-corrected/result.json')==plan['cpu_sha256'],'CPU evidence changed')
    pkg.verify(plan['package']['package'],plan['package']['manifest_sha256'])
    for gateway in plan['gateways']:m.verify(gateway['package'],gateway['manifest_sha256'])
    m.g.require(pkg.sha(BASE/'model-private/policy-private.json')==plan['private_policy_sha256'] and
        pkg.sha(ROOT/'build/q4t')==plan['package']['binary_sha256'] and
        pkg.sha(ROOT/'build/q4t.release.json')==plan['default_release_sha256'],'default/keys changed')
    return plan


def incomplete(raw,request_id):
    m.g.require(raw and b'data: [DONE]' not in raw,'crashed gateway claimed completion')
    values=[json.loads(line[6:]) for line in raw.splitlines(keepends=True)
            if line.startswith(b'data: {') and line.endswith(b'\n')]
    chunks=[v for v in values if 'choices' in v]
    m.g.require(chunks and all(v['id']==request_id for v in chunks) and
        all(c.get('finish_reason') is None for v in chunks for c in v['choices']),
        'interrupted stream identity/success mismatch')
    try:a.read_sse(io.BytesIO(raw))
    except ProtocolError as error:return dict(rejected=True,reason=str(error),done=False)
    raise ValueError('client accepted a truncated stream')


class Run(gw.Run):
    def __init__(self,plan):
        super().__init__(plan)
        self.fronts={};self.custody=None;self.unit_linked=False
        self.front_logs=[]

    def budget(self,kind=None):
        super().budget(kind)
        if self.custody is not None and not self.cleaning:self.custody.check()

    def launch(self):
        self.log=(OUT/'controller.log').open('xb')
        argv=[self.manifest['python']['path'],'-I','-B',ROOT/'tools/serve_service/controller_exec.py',
            '--plan',PLAN,'--output',self.run_dir,'--parent',str(os.getpid())]
        self.gpu_started=time.monotonic();signal.setitimer(signal.ITIMER_REAL,GPU_WINDOW)
        self.process=subprocess.Popen(list(map(str,argv)),stdin=subprocess.DEVNULL,
            stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        state=self.wait_state('READY',1);self.controller=pkg.read(self.run_dir/'controller.json')
        m.g.require(self.controller['owner']==sup.process_identity(self.process.pid),'model controller mismatch')
        self.owners=[self.controller['owner'],state['owner'],state['backend']]
        pkg.save(OUT/'model-owners.json',self.owners)
        path=OUT/'custody';path.mkdir()
        self.custody=lease_custody.Custody(self.controller['owner'],a.guard.common_lock(),path,
            on_failure=lambda:os.kill(os.getpid(),signal.SIGTERM))
        self.custody.start()

    def front_cli(self,index,label,action):
        return self.command(label,[self.manifest['python']['path'],'-I','-B',
            ROOT/'tools/serve_gateway/managed.py',action,'--run',self.fronts[index]['run']])

    def wait_front(self,index,states,timeout=15):
        until=time.monotonic()+timeout;path=self.fronts[index]['run']
        while time.monotonic()<until:
            self.budget()
            if (path/'controller.json').exists():
                state=m.status(path)
                if state['state'] in states:return state
                m.g.require(state['state'] not in ('FAILED','STOPPED','OWNER_ABSENT'),
                            'gateway terminated before expected state: '+str(state))
            time.sleep(.05)
        raise RuntimeError('gateway state deadline')

    def start_front(self,index,*,unit=False):
        self.budget();m.g.require(index not in self.fronts and len(self.fronts)<4,'gateway start budget')
        selected=self.plan['gateways'][index-1]
        path=OUT/f'front-{index}'
        self.fronts[index]=dict(run=path,package=Path(selected['package']),process=None,unit=unit)
        if unit:
            before=self.command('unit-before',['systemctl','--user','show',self.plan['unit'],'-p','LoadState'])
            m.g.require(before.stdout.strip()=='LoadState=not-found','unit already exists')
            text=m.unit(selected['package'],selected['manifest_sha256'],self.run_dir,path)
            text+='RuntimeMaxSec=420\n'
            self.unit_file.write_text(text)
            self.command('unit-verify',['systemd-analyze','--user','verify',self.unit_file])
            self.command('unit-link',['systemctl','--user','link','--runtime',self.unit_file])
            m.g.require(self.runtime_link.is_symlink() and self.runtime_link.resolve()==self.unit_file.resolve(),
                        'unit link not owned')
            self.unit_linked=True
            self.command('unit-start',['systemctl','--user','start',self.plan['unit']])
        else:
            log=(OUT/f'front-{index}-controller.log').open('xb');self.front_logs.append(log)
            argv=[self.manifest['python']['path'],'-I','-B',ROOT/'tools/serve_gateway/managed.py',
                'run','--package',selected['package'],'--manifest-sha256',selected['manifest_sha256'],
                '--backend-run',self.run_dir,'--output',path]
            self.fronts[index]['process']=subprocess.Popen(list(map(str,argv)),stdin=subprocess.DEVNULL,
                stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        state=self.wait_front(index,{'READY'})
        control=m.read(path/'controller.json');observation=state['observation']
        self.fronts[index].update(owner=control['owner'],nginx=observation['nginx'])
        if unit:
            props=self.command('unit-running',['systemctl','--user','show',self.plan['unit'],
                '-p','MainPID','-p','Restart','-p','NRestarts','-p','KillMode','-p','FragmentPath'])
            values=dict(x.split('=',1) for x in props.stdout.splitlines())
            m.g.require(int(values['MainPID'])==control['owner']['pid'] and values['Restart']=='no' and
                values['NRestarts']=='0' and values['KillMode']=='mixed' and
                Path(values['FragmentPath']).resolve()==self.unit_file.resolve(),'wrong unit policy/owner')
        else:m.g.require(control['owner']==sup.process_identity(self.fronts[index]['process'].pid),'front owner differs')
        fd=Path(f"/proc/{observation['nginx']['pid']}/fd")
        inherited=[]
        for p in fd.iterdir():
            try:info=p.stat()
            except FileNotFoundError:continue
            if [info.st_dev,info.st_ino]==control['claim_identity']:inherited.append(p.name)
        m.g.require(inherited,'nginx did not retain package claim')
        pkg.save(OUT/f'front-{index}-ready.json',dict(control=control,status=state,claim_fds=inherited))

    def wait_gone(self,index,timeout=15):
        front=self.fronts[index];until=time.monotonic()+timeout
        while time.monotonic()<until:
            if not self.cleaning:self.budget()
            process=front['process']
            if process is not None:process.poll()
            # Parent-death acceptance never signals surviving nginx helpers.
            for pid,info in sup.processes.group_members(front['nginx']['pid']).items():
                if info['state']=='Z' and info['parent']==os.getpid():
                    try:os.waitpid(pid,os.WNOHANG)
                    except ChildProcessError:pass
            if not m.alive(front['owner']) and not sup.processes.group_members(front['nginx']['pid']):break
            time.sleep(.05)
        else:raise RuntimeError('gateway processes remained')
        with m.claim(front['package']):pass
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            probe.bind(('127.0.0.1',18444))
        pkg.save(OUT/f'front-{index}-gone.json',dict(passed=True,claim_available=True,port_available=True,
            auxiliary_nginx_signals=False,observed_monotonic=time.monotonic()))

    def idle(self,label,total,successful,aborted):
        until=time.monotonic()+5
        for index in range(20):
            metrics=self.metrics(f'{label}-{index:02d}')
            if all(metrics[n]==0 for n in a.EMPTY):break
            m.g.require(time.monotonic()<until,'backend did not recover from gateway disconnect')
            time.sleep(.1)
        m.g.require(metrics['q4t_requests_total']==total and metrics['q4t_requests_success_total']==successful and
            metrics['q4t_requests_aborted_total']==aborted and metrics['q4t_requests_error_total']==0 and
            metrics['q4t_seq_slots_free']==1,'backend accounting differs')
        return metrics

    def graceful(self):
        body=a.make_request([dict(role='user',content='逐行输出从1到40的整数，每行一个数字，不要解释。')],
                            [],stream=True,max_tokens=256,timeout_ms=30000)
        entered=threading.Event()
        with self.concurrent_requests() as pool:
            future=pool.submit(self.wire,'graceful-stream',body,content_event=entered)
            m.g.require(entered.wait(15),'drain stream never became active')
            before=self.metrics('graceful-active')
            m.g.require(before['q4t_active_chats']==1 and not future.done(),'stream finished before stop')
            self.front_cli(1,'front-1-stop','stop')
            raw=future.result(35);value=a.read_sse(io.BytesIO(raw))
            m.g.require(value.id==body['request_id'] and value.finish_reason=='stop' and
                value.message['content'].strip().splitlines()==[str(n) for n in range(1,41)],'drain stream quality')
            pkg.save(OUT/'graceful-stream-response.json',gw.asdict(value))
        terminal=self.wait_front(1,{'STOPPED'})
        m.g.require(terminal['cleanup_confirmed'] and terminal['nginx_returncode']==0,'graceful cleanup incomplete')
        self.wait_gone(1)
        before=(self.fronts[1]['run']/'terminal.json').read_bytes()
        self.front_cli(1,'front-1-stop-idempotent','stop')
        m.g.require((self.fronts[1]['run']/'terminal.json').read_bytes()==before,'terminal rewritten')

    def crash(self,index,kind):
        body=a.make_request([dict(role='user',content='逐行输出从1到10000的所有整数，每行一个，不要省略。')],
                            [],stream=True,max_tokens=4096,timeout_ms=180000)
        entered=threading.Event();label=f'front-{index}-interrupted'
        with self.concurrent_requests() as pool:
            future=pool.submit(self.wire,label,body,content_event=entered)
            m.g.require(entered.wait(15) and not future.done(),'crash stream not active')
            front=self.fronts[index];owner=front['owner' if kind=='manager' else 'nginx']
            self.budget('controls')
            m.g.require(sup.signal_owner(owner,signal.SIGKILL),'owned crash target absent')
            pkg.save(OUT/f'front-{index}-crash.json',dict(kind=kind,owner=owner,signal='SIGKILL'))
            self.wait_gone(index)
            try:future.result(10)
            except (OSError,__import__('http.client',fromlist=['IncompleteRead']).IncompleteRead):pass
            raw=(OUT/(label+'-wire.bin')).read_bytes()
            pkg.save(OUT/(label+'-verified.json'),incomplete(raw,body['request_id']))
        state=m.status(front['run'])
        if kind=='manager':
            m.g.require(state['state']=='OWNER_ABSENT' and not state['cleanup_confirmed'] and
                not (front['run']/'terminal.json').exists(),'crashed manager forged terminal')
            m.g.require(front['process'].returncode==-signal.SIGKILL,'manager crash exit differs')
        else:
            m.g.require(state['state']=='FAILED' and state['cleanup_confirmed'] and state['restarts']==0 and
                state['nginx_returncode']==-signal.SIGKILL and front['process'].returncode==1,'master crash not terminal')
        pkg.save(OUT/f'front-{index}-final-state.json',state)

    def business(self):
        self.start_front(1,unit=True)
        self.plain('ordinary')
        result=self.complete('strict',a.strict_json())
        m.g.require(result.finish_reason=='stop' and json.loads(result.message['content'])=={'ok':True},'strict quality')
        self.tools('project');self.graceful();self.idle('after-graceful',5,5,0)
        self.start_front(2);self.crash(2,'manager');self.idle('after-manager-crash',6,5,1)
        self.start_front(3);self.crash(3,'master');self.idle('after-master-crash',7,5,2)
        self.start_front(4);self.plain('final-recovery')
        metrics=self.idle('final-metrics',8,6,2)
        m.g.require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==3,
                    'grammar accounting differs')
        self.stop_backend()
        terminal=self.wait_front(4,{'FAILED'},timeout=10)
        m.g.require(terminal['cleanup_confirmed'] and not terminal['backend_stopped_by_gateway'],
                    'dependency-loss cleanup incomplete')
        self.wait_gone(4)

    def stop_backend(self):
        super().stop_backend()
        if self.custody is not None and self.custody.fd>=0:
            self.gpu_finished=None
            if not self.cleaning:signal.setitimer(signal.ITIMER_REAL,max(.01,GPU_WINDOW-(time.monotonic()-self.gpu_started)))

    def stop_owned(self):
        errors=[]
        for index,front in self.fronts.items():
            try:
                if (front['run']/'controller.json').exists() and m.alive(m.read(front['run']/'controller.json')['owner']):
                    self.front_cli(index,f'front-{index}-cleanup-stop','stop')
                if front['process'] is not None:front['process'].wait(50)
                if front.get('nginx') and sup.processes.group_members(front['nginx']['pid']):
                    # Failure-only cleanup, never proof of natural parent-death.
                    pkg.save(OUT/f'front-{index}-auxiliary-cleanup.json',dict(reason='acceptance failed with descendants'))
                    for pid,info in sup.processes.group_members(front['nginx']['pid']).items():
                        sup.processes.signal_member(pid,info['start'],signal.SIGTERM)
                    self.wait_gone(index)
            except BaseException as error:errors.append(repr(error))
        try:self.stop_backend()
        except BaseException as error:errors.append(repr(error))
        try:
            if self.unit_linked:
                m.g.require(self.runtime_link.is_symlink() and self.runtime_link.resolve()==self.unit_file.resolve(),
                            'unit ownership changed before cleanup')
                self.command('unit-final-stop',['systemctl','--user','stop',self.plan['unit']])
                self.command('unit-reset',['systemctl','--user','reset-failed',self.plan['unit']],expected=None)
                self.runtime_link.unlink()
                self.command('unit-daemon-reload',['systemctl','--user','daemon-reload'])
                self.unit_linked=False
        except BaseException as error:errors.append(repr(error))
        m.g.require(not errors,'owned cleanup failed: '+str(errors))

    def execute(self):
        OUT.mkdir(mode=0o700);pkg.save(OUT/'begin.json',dict(monotonic=self.started,unix_seconds=time.time()))
        result=dict(status='FAILED',performance_acceptance=False,long_test_started=False)
        def interrupted(*_):raise RuntimeError('gateway managed acceptance interrupted')
        previous={s:signal.signal(s,interrupted) for s in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT)}
        try:
            a.require_idle(OUT/'gpu-before.json');self.launch();self.business()
            self.stop_owned();time.sleep(10)
            for front in self.fronts.values():
                m.g.require(not m.alive(front['owner']) and not sup.processes.group_members(front['nginx']['pid']),
                            'gateway restarted')
            m.g.require(self.process.poll()==0 and not self.runtime_link.exists(),'backend/unit remained')
            for log in OUT.glob('front-*/gateway.*'):
                raw=log.read_bytes()
                m.g.require(all(c['api_key'].encode() not in raw for c in self.keys['clients']),'key leaked')
            result.update(status='PASS_MANAGED_TLS_GATEWAY_LIFECYCLE',model_starts=1,gateway_starts=4,
                successful_requests=6,interrupted_requests=2,real_tool_executions=1)
        except BaseException as error:
            result['error']=repr(error)
            if isinstance(error,lease_custody.EnvironmentAbort) or 'ENV_ABORT' in str(error):result['status']='ENV_ABORT'
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0);self.cleaning=True
            try:
                self.stop_owned();a.require_idle(OUT/'gpu-final.json')
                if self.custody is not None and self.custody.fd>=0:
                    m.g.require(not a.lock_available(a.guard.common_lock()),'copied lease lost before cleanup')
                    self.custody.finish(cleanup_confirmed=True)
                self.gpu_finished=time.monotonic()
                m.g.require(a.lock_available(a.guard.common_lock()),'lease not released')
                m.g.require((self.gpu_started is None or self.gpu_finished-self.gpu_started<GPU_WINDOW) and
                    time.monotonic()-self.started<LIMITS['seconds'],'time bound exceeded')
                result['cleanup_confirmed']=True
            except BaseException as error:
                result.update(status='ENV_ABORT' if result['status']=='ENV_ABORT' else 'FAILED',
                              cleanup_error=repr(error));raise
            finally:
                for s,h in previous.items():signal.signal(s,h)
                for stream in [self.log,*self.front_logs]:
                    if stream:stream.close()
                pkg.save(OUT/'result.json',dict(result,counts=self.counts,
                    elapsed_seconds=time.monotonic()-self.started,gpu_window_elapsed=None if self.gpu_finished is None
                    or self.gpu_started is None else self.gpu_finished-self.gpu_started,
                    supervisor_http_upper_bound=300))
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','check','run'));args=parser.parse_args();configure()
    value=prepare() if args.action=='prepare' else check()
    if args.action=='run':
        with sup.processes.child_subreaper():value=Run(value).execute()
    print(json.dumps(value))
