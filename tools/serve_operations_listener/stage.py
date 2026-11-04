"""One shared lease: native listener boundaries, TLS quality and delivery."""
import argparse
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_operations_listener import cpu
from tools.serve_error_params.stage import Children,h,a,DAYS
from tools.serve_clients import short as s
from tools.serve_ops import contracts,diagnose,identity
from tools.serve_service import package as pkg,supervisor as sup

BASE=cpu.BASE
PACKAGE,OUTPUT=BASE/'package',BASE/'execution'
PORT,OPS_PORT,TLS_PORT=18132,18133,18452
LIMITS=dict(starts=1,generation_attempts=6,controls=100,seconds=300)
PLANNED=dict(models=1,successes=5,cancelled=1,real_tools=2,negative_probes=6,
             public_slow_headers=135,native_processes=1)
CPU_LABEL='cpu-01'
BUILD_LABEL='build-02'
DRIVER_LABEL='driver-cpu-final'


def cpu_gate():
    cpu.require(cpu.read(BASE/CPU_LABEL/'result.json')['passed'] and
                cpu.read(BASE/'cpu-diagnostic-fix/result.json')['passed'],'CPU gate')
    original=cpu.read(BASE/CPU_LABEL/'sources.json')
    repaired=cpu.read(BASE/'cpu-diagnostic-fix/sources.json')
    overrides={'tools/serve_ops/diagnose.py','tools/serve_operations_listener/test_tools.py'}
    driver={'tools/serve_operations_listener/stage.py','tools/serve_operations_listener/test_stage.py'}
    for name,digest in original.items():
        if name not in driver:
            cpu.require(cpu.sha(ROOT/name)==(repaired[name] if name in overrides else digest),
                        'CPU source drift: '+name)


def source_paths():
    paths=set(cpu.sources())
    paths.update(subprocess.check_output(['git','ls-files','tools','cmake'],cwd=ROOT,text=True).splitlines())
    paths.update(('docs/MODEL.md','docs/SERVE_OPERATIONS_LISTENER_2026-11-04.md'))
    return {name:cpu.sha(ROOT/name) for name in sorted(paths)}


def model_manifest(plan):
    return dict(model_dir=plan['model_dir'],settings=pkg.settings(PORT,0,operations_port=OPS_PORT),
                client_policy=True)


def model_command(plan):
    manifest=model_manifest(plan)
    # The acceptance candidate is not a managed release yet. Use the exact
    # reviewed serving profile; release packaging follows independent acceptance.
    command=pkg.command(PACKAGE/'candidate',manifest)
    command[0]=plan['binary']
    command[command.index('--client-policy-file')+1]=plan['policy']
    return command


def saturation_evidence(metrics):
    cpu.require(metrics['q4t_public_connections']==136 and
                metrics['q4t_business_connections']==1 and
                metrics['q4t_active_chats']==1 and
                metrics['q4t_operations_listener_enabled']==1 and
                metrics['q4t_operations_connections']>=1,
                'not saturated while active through operations listener')


def prepare():
    cpu.require(not PACKAGE.exists() and not OUTPUT.exists(),'preserve previous attempt')
    cpu.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit reviewed stage first')
    cpu_gate()
    cpu.require(cpu.read(BASE/DRIVER_LABEL/'result.json')['passed'],'driver CPU gate')
    for name,digest in cpu.read(BASE/DRIVER_LABEL/'sources.json').items():
        cpu.require(cpu.sha(ROOT/name)==digest,'driver source drift: '+name)
    built=cpu.read(BASE/BUILD_LABEL/'result.json')
    cpu.require(built['passed'] and cpu.runtime()==cpu.read(BASE/BUILD_LABEL/'runtime.json'),'build identity')
    for path,digest in built['binaries'].items():cpu.require(cpu.sha(path)==digest,'binary changed')
    old=cpu.read(ROOT/'build/q4t.release.json')
    PACKAGE.mkdir()
    for name in ('candidate','previous'):(PACKAGE/name).mkdir()
    for name in a.NAMES:h.copy_checked(h.identity(ROOT/'build'/name),PACKAGE/'previous'/name)
    h.copy_checked(h.identity(old['build_cache']),PACKAGE/'previous/CMakeCache.txt')
    h.copy_checked(old['operations_rules'],PACKAGE/'previous/q4t.rules.yml')
    for name in ('q4t','CMakeCache.txt','compile_commands.json','q4t_http_lifecycle_tests'):
        h.copy_checked(h.identity(cpu.ENGINE/name),PACKAGE/'candidate'/name)
    import shutil
    shutil.copytree(cpu.ENGINE/'generated',PACKAGE/'candidate/generated')
    cpu.save(PACKAGE/'candidate/runtime-source.json',cpu.runtime())
    h.copy_checked(h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'),PACKAGE/'candidate/q4t.rules.yml')
    previous=cpu.read(old['tested_source_manifest'])
    changed=sorted(n for n,d in cpu.runtime().items() if previous[n]!=d)
    cpu.require(set(changed)=={'include/q4t/server/chat_server.h','include/q4t/server/http_connection.h',
        'include/q4t/server/server_options.h','src/main.cpp','src/server/chat_capabilities.cpp',
        'src/server/chat_http.cpp','src/server/chat_metrics.cpp','src/server/chat_plan.cpp',
        'src/server/chat_server.cpp','src/server/server_options.cpp'},'unreviewed runtime changes')
    cpu.save(PACKAGE/'numerical-reuse.json',dict(changed=changed,
        previous=h.identity(old['tested_source_manifest']),current=h.identity(PACKAGE/'candidate/runtime-source.json'),
        unchanged_runtime_sources=len(previous)-len(changed),
        model_math_tokenizer_template_cache_scheduler_unchanged=True,performance_acceptance=False))
    private=BASE/'private';private.mkdir(mode=0o700)
    cert,key=s.certificate(private)
    front=s.example(TLS_PORT,PORT)
    front.update(version=2,allowed_client_cidrs=['127.0.0.1/32'],upstream_operations_port=OPS_PORT,
        per_client_inflight=4,global_inflight=8,per_client_rps=100,burst=128)
    policy=dict(version=1,gateway_token=s.secrets.token_hex(32),clients=[
        dict(id='alpha',max_inflight=1,max_control_inflight=2,max_prompt_tokens=12000,
             max_output_tokens=512,max_body_bytes=131072),
        dict(id='beta',max_inflight=1,max_control_inflight=2,max_prompt_tokens=9000,
             max_output_tokens=64,max_body_bytes=65536)])
    s.g.save(private/'clients.json',json.dumps(policy).encode())
    s.g.save(private/'gateway.json',json.dumps(front).encode())
    gateway=s.g.prepare(private/'gateway.json',cpu.read(s.VENDOR)['binary'],cert,key,
                        private/'gateway',private/'clients.json',trusted_ca=cert)
    prior=cpu.read(ROOT/'.q4t-work/serve-gateway-sources-20261104/plan.json')
    protocol=cpu.read(PACKAGE/'candidate/generated/q4t/server/chat_protocol_sources.json')
    digest=hashlib.sha256(''.join(f'{n}:{protocol[n]}\n' for n in sorted(protocol)).encode()).hexdigest()
    cpu.save(PACKAGE/'candidate/diagnostic-identity.json',dict(binary_sha256=cpu.sha(PACKAGE/'candidate/q4t'),
        chat_protocol=dict(protocol_sha256=digest),candidate_identity_only=True,acceptance='PENDING'))
    preserved=dict(prior['preserved'])
    for path in DAYS:preserved[str(path)]=cpu.sha(path)
    for path in (prior['frontend'],prior['policy'],Path(prior['gateway']['package'])/'manifest.json'):
        preserved[str(path)]=cpu.sha(path)
    cpu.save(PACKAGE/'sources.json',source_paths())
    plan=dict(root=str(ROOT),limits=LIMITS,planned=PLANNED,port=PORT,operations_port=OPS_PORT,tls_port=TLS_PORT,
        binary=str(PACKAGE/'candidate/q4t'),candidate=str(PACKAGE/'candidate/q4t'),
        candidate_sha256=cpu.sha(PACKAGE/'candidate/q4t'),protocol=digest,protocol_sha256=digest,
        previous_binary_sha256=cpu.sha(PACKAGE/'previous/q4t'),
        old_hashes={n:cpu.sha(PACKAGE/'previous'/n) for n in a.NAMES},
        model_dir=prior['model_dir'],model_inputs=prior['model_inputs'],
        gateway=gateway,frontend=str(private/'gateway.json'),policy=str(private/'clients.json'),
        preserved=preserved,local={str(p):cpu.sha(p) for p in (private/'clients.json',private/'gateway.json',
            BASE/CPU_LABEL/'result.json',BASE/'cpu-diagnostic-fix/result.json',
            BASE/DRIVER_LABEL/'result.json')},
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        performance_acceptance=False)
    cpu.save(PACKAGE/'plan.json',plan);h.seal(PACKAGE)
    return dict(prepared=True,limits=LIMITS,planned=PLANNED,models_started=0)


def check():
    h.verify_seal(PACKAGE);plan=cpu.read(PACKAGE/'plan.json')
    cpu.require(plan['root']==str(ROOT) and plan['limits']==LIMITS and plan['planned']==PLANNED,'frozen scope')
    a.verify_hashes(ROOT,cpu.read(PACKAGE/'sources.json'))
    a.verify_hashes(ROOT,cpu.read(PACKAGE/'candidate/runtime-source.json'))
    a.verify_hashes(plan['model_dir'],plan['model_inputs'])
    for path,digest in {**plan['preserved'],**plan['local']}.items():
        cpu.require(cpu.sha(path)==digest,'preserved/frozen input changed')
    for path in DAYS:cpu.require(not (path.parent/'execution').exists(),'user long run executed')
    s.g.check(plan['gateway']['package'],plan['gateway']['manifest_sha256'])
    if (OUTPUT/'begin.json').exists():plan['started']=plan['original_started']=cpu.read(OUTPUT/'begin.json')['started']
    return plan


class Run(s.Run):
    def __init__(self,plan):
        s.OUT=OUTPUT/'business';s.PORT=PORT;s.TLS_PORT=TLS_PORT;s.LIMITS=LIMITS
        s.OUT.mkdir();super().__init__(plan)
        self.children=Children(OUTPUT/'lease');self.current_client=0
        self.held=[];self.negative_probes=0

    def complete(self,label,body,client=None):
        client=self.current_client if client is None else client
        body=copy.deepcopy(body);body['max_tokens']=min(body.get('max_tokens',512),512 if client==0 else 64)
        return super().complete(label,body,client)

    def operations(self,label,path,*,credentials=False,expected=200):
        self.budget('controls')
        headers={'Connection':'close'}
        if credentials:
            headers.update({'X-Q4T-Client-Id':'alpha','X-Q4T-Gateway-Token':self.policy['gateway_token']})
        connection=http.client.HTTPConnection('127.0.0.1',OPS_PORT,timeout=3)
        self.sockets.append(connection)
        try:
            connection.request('GET',path,headers=headers);response=connection.getresponse()
            cpu.save(s.OUT/(label+'-http.json'),dict(status=response.status,headers=dict(response.getheaders())))
            return self.record(label,response,response.read(262145),expected)
        finally:connection.close()

    def get_metrics(self,label):
        raw=self.operations(label,'/metrics').decode()
        contracts.parse_metrics(raw,version=5)
        return s.parse_metrics(raw),raw

    def launch(self,scoped=True):
        cpu.require(scoped,'scoped-only acceptance')
        self.budget('starts')
        env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and
             k not in ('LD_PRELOAD','LD_LIBRARY_PATH')}
        self.model=self.children.launch(s.OUT/'scoped-model',model_command(self.plan),env)
        until=time.monotonic()+180
        while 'serving on port' not in (s.OUT/'scoped-model/stderr.log').read_text():
            cpu.require(self.children.running(self.model) and time.monotonic()<until,'startup failed')
            time.sleep(.1)
        config=json.loads(self.operations('scoped-model-config','/v1/config'))
        s.Draft202012Validator(cpu.read(ROOT/'docs/serve-config-v1.schema.json')).validate(config)
        cpu.require(config['protocol_source_sha256']==self.plan['protocol'],'protocol drift')
        s.cp.observed_matches(config['client_policy'],s.cp.binding(Path(self.plan['policy']).read_bytes()))
        cpu.require(contracts.configuration(config,version=5)['operations_port']==OPS_PORT,'operations config')
        package=Path(self.plan['gateway']['package'])
        manifest=s.g.check(package,self.plan['gateway']['manifest_sha256'])
        self.nginx=self.children.launch(s.OUT/'nginx',[manifest['nginx']['path'],'-e','stderr',
            '-p',str(package)+'/','-c',str(package/'nginx.conf')],env)
        until=time.monotonic()+10
        while True:
            cpu.require(self.children.running(self.nginx) and time.monotonic()<until,'gateway startup failed')
            try:
                with socket.create_connection(('127.0.0.1',TLS_PORT),.1):break
            except ConnectionRefusedError:time.sleep(.05)
        self.tls=True

    def saturated(self):
        body=s.a.ordinary(True)
        body['messages'][0]['content']='逐行列出从1到1000的数字，不要省略。'
        body['max_tokens']=512
        connection,response=self.begin('cancel-active',body)
        prefix=response.readline()
        cpu.require(response.status==200 and prefix.startswith(b'data: '),'active SSE not started')
        for _ in range(PLANNED['public_slow_headers']):
            sock=socket.create_connection(('127.0.0.1',PORT),1)
            self.held.append(sock);sock.sendall(b'GET /metrics HTTP/1.1\r\nHost:')
        until=time.monotonic()+3
        while True:
            metrics,_=self.get_metrics('saturation-'+str(self.counts['controls']))
            if metrics['q4t_public_connections']==136:break
            cpu.require(time.monotonic()<until,'public handlers did not fill');time.sleep(.01)
        saturation_evidence(metrics)
        cpu.save(s.OUT/'saturated-metrics.json',metrics)
        self.request('public-overflow',path='/metrics',method='GET',direct=True,expected=503)
        self.negative_probes+=1
        # Incomplete public headers remain held throughout these operations.
        for _ in range(5):self.budget('controls')
        diagnostic=diagnose.snapshot(PORT,self.model.pid,PACKAGE/'candidate/diagnostic-identity.json',
            operations_port=OPS_PORT,client_policy_path=self.plan['policy'])
        cpu.save(s.OUT/'saturated-diagnostic.json',diagnostic)
        cpu.require(diagnostic['state']=='HEALTHY' and diagnostic['identity']['state']=='verified',
                    'diagnostic unavailable during public saturation')
        for _ in range(4):self.budget('controls')
        observed=sup.probe(self.model.pid,PORT,OPS_PORT)
        cpu.save(s.OUT/'saturated-supervisor.json',observed)
        cpu.require(sup.classify(observed,self.plan['protocol'],self.plan['candidate_sha256'],
            s.cp.binding(Path(self.plan['policy']).read_bytes()),OPS_PORT)=='READY','supervisor not healthy')
        for path in ('/v1/models','/v1/capabilities'):
            self.request('saturated-'+path.rsplit('/',1)[1],path=path,method='GET')
        self.operations('anonymous-discovery','/v1/capabilities',expected=401);self.negative_probes+=1
        self.operations('identity-local-admin','/metrics',credentials=True,expected=403);self.negative_probes+=1
        self.request('bad-key',path='/v1/models',method='GET',headers={'Authorization':'Bearer invalid'},expected=401)
        self.negative_probes+=1
        self.request('gateway-local-admin',path='/metrics',method='GET',expected=404);self.negative_probes+=1
        cancel=dict(request_id=body['request_id'],cancel_token=body['cancel_token'])
        self.request('cross-client-cancel',cancel,path='/v1/requests/cancel',client=1,expected=404)
        self.negative_probes+=1
        self.request('operations-cancel',cancel,path='/v1/requests/cancel',expected=202)
        raw=self.record('cancel-active',response,prefix+response.read(8*1024**2+1),200)
        cpu.save(s.OUT/'cancel-terminal.json',s.cancelled_wire(raw,body['request_id']))
        self.cancelled+=1;connection.close()
        for sock in self.held:sock.close()
        self.held.clear()
        self.idle('cancel-recovery')

    def execute(self):
        result=dict(status='FAILED',limits=LIMITS,counts=self.counts)
        try:
            s.a.require_idle(s.OUT/'initial-gpu.json')
            for port in (PORT,OPS_PORT,TLS_PORT):
                with socket.socket() as sock:
                    sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
            native=self.children.launch(s.OUT/'native',[str(PACKAGE/'candidate/q4t_http_lifecycle_tests')])
            self.children.wait(native,s.OUT/'native',60)
            self.launch();self.saturated()
            value=self.complete('ordinary-recovery',s.a.ordinary(True))
            cpu.require(value.finish_reason=='stop' and value.message['content'].strip()=='710003','recovery quality')
            for client,name in enumerate(('alpha','beta')):
                self.current_client=client;self.tools('project-'+name)
            metrics=self.idle('final-idle');cpu.save(s.OUT/'final-metrics.json',metrics)
            cpu.require(self.successes==metrics['q4t_requests_success_total']==5 and self.cancelled==1 and
                metrics['q4t_requests_aborted_total']==1 and self.negative_probes==6 and
                metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,
                'request/grammar accounting')
            self.stop(True);self.children.reap(s.OUT);s.a.require_idle(s.OUT/'final-gpu.json')
            result.update(status='PASS_BOUNDED_OPERATIONS_LISTENER',successes=self.successes,
                cancelled=self.cancelled,negative_probes=self.negative_probes,real_tools=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED',error=repr(error));raise
        finally:
            for sock in self.held:sock.close()
            for connection in self.sockets:connection.close()
            try:self.children.cleanup()
            except BaseException as error:
                result.update(status='FAILED',cleanup_error=repr(error));raise
            finally:
                result['seconds']=time.monotonic()-self.started;cpu.save(s.OUT/'result.json',result)


def verifier(plan,installed=False):
    if not installed:Run(plan).execute();return
    cpu.require(cpu.sha(ROOT/'build/q4t')==plan['candidate_sha256'] and
        cpu.read(OUTPUT/'business/result.json')['status']=='PASS_BOUNDED_OPERATIONS_LISTENER','acceptance identity')
    folder=OUTPUT/'health';folder.mkdir()
    cpu.require(cpu.command(folder,'version',[ROOT/'build/q4t','version'],10),'installed CLI')
    cpu.save(folder/'result.json',dict(passed=True,installed=h.identity(ROOT/'build/q4t'),
        acceptance=h.identity(OUTPUT/'business/result.json'),new_models=0,new_http=0))


def release(plan):
    result=OUTPUT/'business/result.json'
    cpu.require(cpu.read(result)['status']=='PASS_BOUNDED_OPERATIONS_LISTENER','business gate')
    metadata=cpu.read(PACKAGE/'previous/q4t.release.json')
    inherited=copy.deepcopy({k:metadata[k] for k in ('acceptance','acceptance_decision',
        'acceptance_decision_sha256','chat_protocol','deployment_completion','scope') if k in metadata})
    inherited['release']=h.identity(PACKAGE/'previous/q4t.release.json')
    metadata.pop('deployment_completion',None);metadata.pop('implementation_capabilities',None)
    metadata.update(version='serve-operations-listener-20261104',binary_sha256=plan['candidate_sha256'],
        source_commit=plan['source_commit'],tool_source_commit=plan['source_commit'],
        source_note='Explicit loopback operations listener, independent handlers, unchanged client authentication, v5 observability and scoped control routing.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=cpu.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),build_cache_sha256=cpu.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_OPERATIONS_LISTENER',acceptance_decision=str(result),acceptance_decision_sha256=cpu.sha(result),
        tool_acceptance=str(result),tool_acceptance_sha256=cpu.sha(result),inherited_acceptance=inherited,
        previous_binary=str(PACKAGE/'previous/q4t'),previous_release=str(PACKAGE/'previous/q4t.release.json'),
        observed_configuration=cpu.read(OUTPUT/'business/scoped-model-config-wire.bin'),
        operations_rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'),rollback_operations_rules=h.identity(PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor',production='default MTP k3, full head, text',
            validation='native dual-listener lifecycle, public header saturation, authenticated diagnostics/control/cancellation, recovery and two real project tool/citation loops',
            installation_health='identical accepted ELF, installed CLI; HTTP reused',
            boundaries='No actual LAN, managed lifecycle, long soak, physical RAM or new numerical/performance acceptance; old packages and task protocols stay pinned',
            performance_acceptance=False,numerical_evidence=str(PACKAGE/'numerical-reuse.json')))
    metadata['chat_protocol'].update(protocol_sha256=plan['protocol'],
        declaration_evidence='v5 observability and explicit private listener; existing Chat/template quality and client isolation verified by bounded CPU and HTTP.')
    folder=OUTPUT/'release';folder.mkdir();cpu.save(folder/'q4t.release.json',metadata)
    cpu.save(folder/'binding.json',dict(old=plan['old_hashes'],new=dict(q4t=plan['candidate_sha256'],
        **{'q4t.release.json':cpu.sha(folder/'q4t.release.json')})))
    h.seal(folder);return folder


def bind():
    a.BASE,a.PACKAGE,a.OUTPUT=BASE,PACKAGE,OUTPUT
    a.MARKER=ROOT/'build/serve-operations-listener-recovery.json'
    a.check,a.verifier,a.release=check,verifier,release


def execute():
    bind();h.require_guard(OUTPUT);cpu.save(OUTPUT/'begin.json',dict(started=time.time(),limits=LIMITS))
    def expired(*_):raise RuntimeError('short deadline; cleanup reserve only')
    signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,LIMITS['seconds']-45)
    try:a.execute()
    finally:signal.setitimer(signal.ITIMER_REAL,0)


def finalize():
    bind();a.finalize();h.seal(OUTPUT)


if __name__=='__main__':
    os.umask(0o077);parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','execute','finalize','check'))
    value=globals()[parser.parse_args().action]()
    print(json.dumps(value or dict(passed=True)))
