"""Preparation cancellation, one model and two actual tool loops; bounded delivery."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_prepare_lock import cpu
from tools.serve_operations_listener import stage as previous
from tools.serve_error_params.stage import h,a,DAYS
from tools.serve_clients import short as s
from tools.serve_service import package as pkg

BASE=cpu.BASE
PACKAGE,OUTPUT=BASE/'package',BASE/'execution'
PORT,OPS_PORT,TLS_PORT=18136,18137,18456
LIMITS=dict(starts=1,generation_attempts=6,controls=100,seconds=300)
PLANNED=dict(models=1,successes=5,cancelled=1,real_tools=2,native_cases=23)
CPU_LABEL,BUILD_LABEL,DRIVER_LABEL='cpu-finish','build-02','driver-03'


def cpu_gate():
    cpu.require(cpu.read(BASE/CPU_LABEL/'result.json')['passed'],'CPU gate')
    for name,digest in cpu.read(BASE/CPU_LABEL/'sources.json').items():
        cpu.require(cpu.sha(ROOT/name)==digest,'CPU source drift: '+name)
    probe=cpu.read(BASE/(BUILD_LABEL+'-probe')/'result.json')
    cpu.require(probe['passed'] and cpu.sha(cpu.ENGINE/'q4t_prepare_lock_probe')==
                probe['binary_sha256'],'native binary identity')


def source_paths():
    paths=set(cpu.sources())
    paths.update(subprocess.check_output(['git','ls-files','tools','cmake'],cwd=ROOT,text=True).splitlines())
    paths.update(('docs/MODEL.md','docs/SERVE_PREPARE_LOCK_2026-11-04.md'))
    return {name:cpu.sha(ROOT/name) for name in sorted(paths)}


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
    h.copy_checked(h.identity(old['acceptance_decision']),PACKAGE/'previous/acceptance.json')
    h.copy_checked(h.identity(old['tested_source_manifest']),PACKAGE/'previous/runtime-source.json')
    shutil.copytree(old['release_declarations']['directory'],PACKAGE/'previous/declarations')
    for name in ('q4t','CMakeCache.txt','compile_commands.json','q4t_prepare_lock_probe'):
        h.copy_checked(h.identity(cpu.ENGINE/name),PACKAGE/'candidate'/name)
    shutil.copytree(cpu.ENGINE/'generated',PACKAGE/'candidate/generated')
    cpu.save(PACKAGE/'candidate/runtime-source.json',cpu.runtime())
    schema=PACKAGE/'candidate/schemas';schema.mkdir()
    for name in pkg.DECLARATION_SCHEMAS:
        h.copy_checked(h.identity(old['chat_protocol'][name+'_schema']),schema/(name+'.json'))
    h.copy_checked(h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'),PACKAGE/'candidate/q4t.rules.yml')
    previous=cpu.read(old['tested_source_manifest'])
    changed=sorted(n for n,d in cpu.runtime().items() if previous.get(n)!=d)
    cpu.require(set(changed)==cpu.CHANGED,'unreviewed runtime changes')
    cpu.save(PACKAGE/'numerical-reuse.json',dict(changed=changed,
        previous=h.identity(old['tested_source_manifest']),current=h.identity(PACKAGE/'candidate/runtime-source.json'),
        unchanged_runtime_sources=sum(previous.get(n)==d for n,d in cpu.runtime().items()),
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
            BASE/CPU_LABEL/'result.json',BASE/DRIVER_LABEL/'result.json')},
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        performance_acceptance=False)
    cpu.save(PACKAGE/'plan.json',plan);h.seal(PACKAGE)
    return dict(prepared=True,limits=LIMITS,planned=PLANNED,models_started=0)


def bind():
    previous.BASE,previous.PACKAGE,previous.OUTPUT=BASE,PACKAGE,OUTPUT
    previous.cpu,previous.LIMITS,previous.PLANNED=cpu,LIMITS,PLANNED
    previous.PORT,previous.OPS_PORT,previous.TLS_PORT=PORT,OPS_PORT,TLS_PORT
    a.BASE,a.PACKAGE,a.OUTPUT=BASE,PACKAGE,OUTPUT
    a.MARKER=ROOT/'build/serve-prepare-lock-recovery.json'
    a.check,a.verifier,a.release=check,verifier,release


def check():
    bind()
    return previous.check()


def verify_native(rows,returncode):
    expected={(mode,cause) for mode in ('encode-json','encode-sse','thinking-json',
        'thinking-sse','scoped-sse','preflight') for cause in ('explicit','deadline',
        'disconnect','shutdown') if (mode,cause)!=('preflight','explicit')}
    cpu.require(returncode==0 and len(rows)==len(expected) and
        {(r['mode'],r['cause']) for r in rows}==expected and all(
        r['passed'] is True and r['returned_before_owner_release'] is True and
        0<=r['seconds']<1.1 for r in rows),'native cancellation evidence')


class Run(previous.Run):
    def cancel_active(self):
        body=s.a.ordinary(True)
        body['messages'][0]['content']='逐行列出从1到1000的数字，不要省略。'
        body['max_tokens']=512
        connection,response=self.begin('cancel-active',body)
        prefix=response.readline()
        cpu.require(response.status==200 and prefix.startswith(b'data: '),'SSE not started')
        self.request('cancel-request',dict(request_id=body['request_id'],
            cancel_token=body['cancel_token']),path='/v1/requests/cancel',expected=202)
        raw=self.record('cancel-active',response,prefix+response.read(8*1024**2+1),200)
        cpu.save(s.OUT/'cancel-terminal.json',s.cancelled_wire(raw,body['request_id']))
        self.cancelled+=1;connection.close();self.idle('cancel-recovery')

    def execute(self):
        result=dict(status='FAILED',limits=LIMITS,counts=self.counts)
        try:
            s.a.require_idle(s.OUT/'initial-gpu.json')
            for port in (PORT,OPS_PORT,TLS_PORT):
                with socket.socket() as sock:
                    sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                    sock.bind(('127.0.0.1',port))
            folder=s.OUT/'native'
            native=self.children.launch(folder,[str(PACKAGE/'candidate/q4t_prepare_lock_probe')])
            self.children.wait(native,folder,15)
            rows=[json.loads(line) for line in (folder/'stdout.log').read_text().splitlines()]
            verify_native(rows,cpu.read(folder/'waitable.json')['returncode'])
            cpu.save(s.OUT/'native-result.json',dict(passed=True,cases=rows,models=0))
            self.launch()
            self.request('capabilities',path='/v1/capabilities',method='GET')
            self.cancel_active()
            body=s.a.ordinary(True);body['thinking_token_budget']=0
            value=self.complete('thinking-recovery',body)
            cpu.require(value.finish_reason=='stop' and value.message['content'].strip()=='710003',
                        'thinking preparation/recovery quality')
            for client,name in enumerate(('alpha','beta')):
                self.current_client=client;self.tools('project-'+name)
            metrics=self.idle('final-idle');cpu.save(s.OUT/'final-metrics.json',metrics)
            cpu.require(self.successes==metrics['q4t_requests_success_total']==5 and self.cancelled==1 and
                metrics['q4t_requests_aborted_total']==1 and
                metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,
                'request/grammar accounting')
            self.stop(True);self.children.reap(s.OUT);s.a.require_idle(s.OUT/'final-gpu.json')
            result.update(status='PASS_BOUNDED_PREPARE_LOCK',successes=self.successes,
                cancelled=self.cancelled,native_cases=len(rows),real_tools=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED',error=repr(error));raise
        finally:
            for connection in self.sockets:connection.close()
            try:self.children.cleanup()
            except BaseException as error:
                result.update(status='FAILED',cleanup_error=repr(error));raise
            finally:
                result['seconds']=time.monotonic()-self.started;cpu.save(s.OUT/'result.json',result)


def verifier(plan,installed=False):
    if not installed:Run(plan).execute();return
    cpu.require(cpu.sha(ROOT/'build/q4t')==plan['candidate_sha256'] and
        cpu.read(OUTPUT/'business/result.json')['status']=='PASS_BOUNDED_PREPARE_LOCK','acceptance identity')
    folder=OUTPUT/'health';folder.mkdir()
    cpu.require(cpu.command(folder,'version',[ROOT/'build/q4t','version'],10),'installed CLI')
    cpu.save(folder/'result.json',dict(passed=True,installed=h.identity(ROOT/'build/q4t'),
        acceptance=h.identity(OUTPUT/'business/result.json'),new_models=0,new_http=0))


def release(plan):
    result=OUTPUT/'business/result.json'
    cpu.require(cpu.read(result)['status']=='PASS_BOUNDED_PREPARE_LOCK','business gate')
    metadata=cpu.read(PACKAGE/'previous/q4t.release.json')
    inherited={k:copy.deepcopy(metadata[k]) for k in ('acceptance','acceptance_decision',
        'acceptance_decision_sha256','chat_protocol','deployment_completion','scope') if k in metadata}
    inherited['release']=h.identity(PACKAGE/'previous/q4t.release.json')
    for key in ('deployment_completion','implementation_capabilities','metadata_correction'):
        metadata.pop(key,None)
    metadata.update(version='serve-prepare-lock-20261104',binary_sha256=plan['candidate_sha256'],
        source_commit=plan['source_commit'],tool_source_commit=plan['source_commit'],
        source_note='Cooperative tokenizer lock acquisition in thinking preparation, prompt encoding and preflight; waiter cancellation leaves owner work intact.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=cpu.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),build_cache_sha256=cpu.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_PREPARE_LOCK',acceptance_decision=str(result),acceptance_decision_sha256=cpu.sha(result),
        tool_acceptance=str(result),tool_acceptance_sha256=cpu.sha(result),inherited_acceptance=inherited,
        previous_binary=str(PACKAGE/'previous/q4t'),previous_release=str(PACKAGE/'previous/q4t.release.json'),
        observed_configuration=cpu.read(OUTPUT/'business/scoped-model-config-wire.bin'),
        operations_rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'),rollback_operations_rules=h.identity(PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor',production='default MTP k3, full head, text',
            validation='23 real handler cancellation cases under contention; HTTP cancellation/thinking preparation recovery and two real project tool/citation loops',
            installation_health='identical accepted ELF, installed CLI; HTTP reused',
            boundaries='Cooperative wait only, not hard realtime or running GPU/model-lock preemption. No new managed/real LAN/long soak/physical RAM/numerical or performance acceptance; old packages and tasks pinned.',
            performance_acceptance=False,numerical_evidence=str(PACKAGE/'numerical-reuse.json')))
    folder=OUTPUT/'release';folder.mkdir();declarations=folder/'declarations';declarations.mkdir()
    for name,label in (('capabilities.json','capabilities-wire.bin'),
                       ('configuration.json','scoped-model-config-wire.bin')):
        h.copy_checked(h.identity(OUTPUT/'business'/label),declarations/name)
    shutil.copytree(PACKAGE/'candidate/schemas',declarations/'schemas')
    caps=cpu.read(declarations/'capabilities.json');protocol=metadata['chat_protocol']
    protocol['protocol_sha256']=plan['protocol']
    for key in caps.keys() & protocol.keys():protocol[key]=copy.deepcopy(caps[key])
    for name in pkg.DECLARATION_SCHEMAS:
        path=declarations/'schemas'/(name+'.json')
        protocol[name+'_schema']=str(path);protocol[name+'_schema_sha256']=cpu.sha(path)
    protocol['declaration_evidence']='Actual SRV-03c capabilities/config and frozen schemas; new protocol identity requires separate explicit task compatibility review.'
    metadata['release_declarations']=dict(profile=pkg.DECLARATION_PROFILE,directory=str(declarations),
        binary_sha256=plan['candidate_sha256'],protocol_sha256=plan['protocol'],
        files={name:cpu.sha(declarations/name) for name in pkg.DECLARATION_FILES})
    pkg.verify_declarations(metadata,required=True)
    cpu.save(folder/'q4t.release.json',metadata)
    cpu.save(folder/'binding.json',dict(old=plan['old_hashes'],new=dict(q4t=plan['candidate_sha256'],
        **{'q4t.release.json':cpu.sha(folder/'q4t.release.json')})))
    h.seal(folder);return folder


def execute():
    bind();h.require_guard(OUTPUT);cpu.save(OUTPUT/'begin.json',dict(started=time.time(),limits=LIMITS))
    def expired(*_):raise RuntimeError('short deadline; cleanup reserve only')
    signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,LIMITS['seconds']-45)
    try:a.execute()
    finally:signal.setitimer(signal.ITIMER_REAL,0)


def finalize():
    bind();a.finalize();h.seal(OUTPUT)


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','execute','finalize','check'))
    print(json.dumps(globals()[p.parse_args().action]() or dict(passed=True)))
