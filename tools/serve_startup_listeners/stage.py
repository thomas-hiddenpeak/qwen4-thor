"""Bounded native startup, dual-listener HTTP quality and default delivery."""
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
from tools.serve_startup_listeners import cpu
from tools.serve_operations_listener import stage as previous, business_fix
from tools.serve_error_params.stage import Children,h,a,DAYS
from tools.serve_clients import short as s
from tools.serve_service import package as pkg

BASE=cpu.BASE
PACKAGE,OUTPUT=BASE/'package',BASE/'execution'
PORT,OPS_PORT,TLS_PORT=previous.PORT,previous.OPS_PORT,previous.TLS_PORT
LIMITS=dict(starts=1,generation_attempts=6,controls=100,seconds=300)
PLANNED=dict(previous.PLANNED,cli_no_model_probes=5)
CPU_LABEL,BUILD_LABEL,DRIVER_LABEL='cpu-01','build-01','driver-cpu-final'


def cpu_gate():
    from tools.serve_startup_listeners import finish_cpu
    finish_cpu.check()


def source_paths():
    paths=set(cpu.sources())
    paths.update(subprocess.check_output(['git','ls-files','tools','cmake'],cwd=ROOT,text=True).splitlines())
    paths.update(('docs/MODEL.md','docs/SERVE_STARTUP_LISTENERS_2026-11-04.md'))
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
    for name in ('q4t','CMakeCache.txt','compile_commands.json','q4t_http_lifecycle_tests'):
        h.copy_checked(h.identity(cpu.ENGINE/name),PACKAGE/'candidate'/name)
    shutil.copytree(cpu.ENGINE/'generated',PACKAGE/'candidate/generated')
    cpu.save(PACKAGE/'candidate/runtime-source.json',cpu.runtime())
    schema=PACKAGE/'candidate/schemas';schema.mkdir()
    for name in pkg.DECLARATION_SCHEMAS:
        h.copy_checked(h.identity(old['chat_protocol'][name+'_schema']),schema/(name+'.json'))
    h.copy_checked(h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'),PACKAGE/'candidate/q4t.rules.yml')
    previous=cpu.read(old['tested_source_manifest'])
    changed=sorted(n for n,d in cpu.runtime().items() if previous.get(n)!=d)
    cpu.require(set(changed)=={'include/q4t/server/chat_server.h',
        'include/q4t/server/http_listener.h','src/server/chat_http.cpp',
        'src/server/chat_server.cpp'},'unreviewed runtime changes')
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
    a.BASE,a.PACKAGE,a.OUTPUT=BASE,PACKAGE,OUTPUT
    a.MARKER=ROOT/'build/serve-startup-listeners-recovery.json'
    a.check,a.verifier,a.release=check,verifier,release


def check():
    bind()
    return previous.check()


def verify_probe(label,text,returncode,public_port,operations_port):
    expected=2 if label=='invalid-options' else 1
    cpu.require(returncode==expected,'wrong startup refusal exit')
    if label in ('baseline-conflict','tokenizer-failure'):
        cpu.require('tokenizer load failed' in text and 'listener bind failed' not in text,
                    'expected tokenizer failure')
    elif label in ('public-conflict','operations-conflict'):
        role='public' if label=='public-conflict' else 'operations'
        port=public_port if role=='public' else operations_port
        cpu.require(f'{role} listener bind failed at 127.0.0.1:{port}' in text and
                    '(errno 98:' in text and 'tokenizer' not in text and
                    '[q4t][startup-memory]' not in text,'conflict not rejected before loading')
    else:
        cpu.require(label=='invalid-options' and 'serve options:' in text,'options refusal')
    cpu.require('serving on port' not in text and '[q4t][startup] model' not in text,
                'negative probe entered serving/model load')


class Run(previous.Run):
    def request(self,label,body=None,expected=200,**kwargs):
        if label not in ('bad-key','gateway-local-admin'):
            return super().request(label,body,expected,**kwargs)
        connection,response=self.begin(label,body,**kwargs)
        try:
            value=json.loads(self.record(label,response,response.read(262145),expected))
            business_fix.gateway_error(value,cpu.read(s.OUT/(label+'-http.json')),expected,
                'gateway_unauthorized' if label=='bad-key' else 'gateway_route_not_found')
            return value
        finally:connection.close()

    def launch(self,scoped=True):
        # Five short native CLI processes, never given a real model path. Keep
        # their exact identities waitable for the same guard as the HTTP phase.
        missing=BASE/'absent-model'
        cpu.require(not missing.exists(),'negative model path must be absent')
        rows=[]
        env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and
             k not in ('LD_PRELOAD','LD_LIBRARY_PATH')}
        for label in ('baseline-conflict','public-conflict','operations-conflict',
                      'tokenizer-failure','invalid-options'):
            with socket.socket() as occupied,socket.socket() as spare:
                for fd in (occupied,spare):
                    fd.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                    fd.bind(('127.0.0.1',0));fd.listen(1)
                taken=occupied.getsockname()[1];other=spare.getsockname()[1]
                spare.close()
                public_port,operations_port=(other,taken) if label=='operations-conflict' else (taken,other)
                if label in ('tokenizer-failure','invalid-options'):occupied.close()
                binary=PACKAGE/('previous' if label=='baseline-conflict' else 'candidate')/'q4t'
                argv=[str(binary),'serve','--model-dir',str(missing),'--host','127.0.0.1',
                      '--port',str(public_port),'--operations-port',str(operations_port),
                      '--max-seq','1','--max-prefill','8192','--max-len','208896']
                if label=='invalid-options':argv+=['--operations-port',str(public_port)]
                folder=s.OUT/label;started=time.monotonic()
                process=self.children.launch(folder,argv,env)
                self.children.wait(process,folder,10,expected=2 if label=='invalid-options' else 1)
                seconds=time.monotonic()-started
                text=(folder/'stderr.log').read_text()
                verify_probe(label,text,cpu.read(folder/'waitable.json')['returncode'],public_port,operations_port)
                if label.endswith('conflict'):
                    cpu.require(occupied.getsockname()[1]==taken,'original listener lost')
                    with socket.create_connection(('127.0.0.1',taken),1):
                        peer,_=occupied.accept();peer.close()
                occupied.close()
                for port in (public_port,operations_port):
                    with socket.socket() as probe:
                        probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                        probe.bind(('127.0.0.1',port));probe.listen(1)
                rows.append(dict(label=label,seconds=seconds,public_port=public_port,
                    operations_port=operations_port,returncode=cpu.read(folder/'waitable.json')['returncode'],
                    ports_released=True,original_owner_preserved=label.endswith('conflict')))
        cpu.save(s.OUT/'startup-probes.json',dict(passed=True,models=0,probes=rows))
        super().launch(scoped)


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
    inherited={k:copy.deepcopy(metadata[k]) for k in ('acceptance','acceptance_decision',
        'acceptance_decision_sha256','chat_protocol','deployment_completion','scope') if k in metadata}
    inherited['release']=h.identity(PACKAGE/'previous/q4t.release.json')
    for key in ('deployment_completion','implementation_capabilities','metadata_correction'):
        metadata.pop(key,None)
    metadata.update(version='serve-startup-listeners-20261104',binary_sha256=plan['candidate_sha256'],
        source_commit=plan['source_commit'],tool_source_commit=plan['source_commit'],
        source_note='Actual dual listeners reserved before tokenizer/model load; scoped ownership through failure, Start, Run, and destruction. TCP backlog is not HTTP readiness.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=cpu.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),build_cache_sha256=cpu.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_STARTUP_LISTENERS',acceptance_decision=str(result),acceptance_decision_sha256=cpu.sha(result),
        tool_acceptance=str(result),tool_acceptance_sha256=cpu.sha(result),inherited_acceptance=inherited,
        previous_binary=str(PACKAGE/'previous/q4t'),previous_release=str(PACKAGE/'previous/q4t.release.json'),
        observed_configuration=cpu.read(OUTPUT/'business/scoped-model-config-wire.bin'),
        operations_rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'),rollback_operations_rules=h.identity(PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor',production='default MTP k3, full head, text',
            validation='native startup ownership/negative CLI; dual-plane HTTP cancellation/recovery and two real project tool/citation loops',
            installation_health='identical accepted ELF, installed CLI; HTTP reused',
            boundaries='TCP may queue before HTTP ready; no startup HTTP 503 or startup signal handler change. No managed/real LAN/long soak/physical RAM/new numerical or performance acceptance; old packages and tasks pinned.',
            performance_acceptance=False,numerical_evidence=str(PACKAGE/'numerical-reuse.json')))
    folder=OUTPUT/'release';folder.mkdir();declarations=folder/'declarations';declarations.mkdir()
    for name,label in (('capabilities.json','saturated-capabilities-wire.bin'),
                       ('configuration.json','scoped-model-config-wire.bin')):
        h.copy_checked(h.identity(OUTPUT/'business'/label),declarations/name)
    shutil.copytree(PACKAGE/'candidate/schemas',declarations/'schemas')
    caps=cpu.read(declarations/'capabilities.json');protocol=metadata['chat_protocol']
    protocol['protocol_sha256']=plan['protocol']
    for key in caps.keys() & protocol.keys():protocol[key]=copy.deepcopy(caps[key])
    for name in pkg.DECLARATION_SCHEMAS:
        path=declarations/'schemas'/(name+'.json')
        protocol[name+'_schema']=str(path);protocol[name+'_schema_sha256']=cpu.sha(path)
    protocol['declaration_evidence']='Actual SRV-12k capabilities/config and frozen schemas, new protocol identity; HTTP quality and lifecycle accepted separately.'
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
