"""One-lease TLS rejection/quality acceptance and existing release transaction."""
import argparse
import copy
import hashlib
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
from tools.serve_error_params import cpu
from tools.serve_error_params import finish_cpu
from tools.serve_error_params.cases import cases, HTTP_CASES
from tools.serve_clients import short as s
from tools.serve_cuda_quota_integration import verify as child
from tools.serve_media_composite import stage as transaction

h,a=transaction.h,transaction.a
BASE=cpu.BASE
PACKAGE,OUTPUT=BASE/'package',BASE/'execution'
PORT,TLS_PORT=18129,18449
LIMITS=dict(starts=1,generation_attempts=30,controls=80,seconds=300)
DAYS=[ROOT/'.q4t-work'/name/'day/plan.json' for name in
      ('serve-soak-20261103','serve-clients-managed-20261104')]
DAYS.insert(1,ROOT/'.q4t-work/serve-cuda-quota-integration-20261104/operator-soak/day/plan.json')


class Children(child.Children):
    def launch(self,folder,argv,env=None):
        # Register immediately after Popen, including identity-capture failures.
        folder.mkdir()
        cpu.save(folder/'command.json',dict(argv=list(map(str,argv))))
        with (folder/'stdout.log').open('xb') as out,(folder/'stderr.log').open('xb') as err:
            process=subprocess.Popen(argv,cwd=ROOT,stdout=out,stderr=err,env=env)
        self.children.append((process,folder))
        info=dict(child.custody.proc(process.pid),argv=list(map(str,argv)),
                  registered_monotonic=time.monotonic())
        self.started.append(info)
        cpu.save(folder/'identity.json',info)
        return process

    def cleanup(self):
        # A timed-out wait can already have reaped one child. It must not
        # prevent cleanup of the remaining gateway/backend children.
        failures=[]
        for process,folder in reversed(self.children):
            try:
                if process.poll() is None:
                    try:self.signal(process,signal.SIGTERM)
                    except ProcessLookupError:pass
                try:process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    try:self.signal(process,signal.SIGKILL)
                    except ProcessLookupError:pass
                    process.wait(timeout=5)
            except BaseException as error:failures.append((str(folder),repr(error)))
        self.children.clear()
        cpu.require(not failures,'own child cleanup failed: '+repr(failures))


def source_paths():
    paths=set(cpu.sources())
    paths.update(subprocess.check_output(['git','ls-files','tools','cmake'],cwd=ROOT,text=True).splitlines())
    paths.update(str(p.relative_to(ROOT)) for p in (ROOT/'tools/serve_error_params').glob('*') if p.is_file())
    paths.update(('docs/MODEL.md','docs/serve-config-v1.schema.json'))
    return {name:cpu.sha(ROOT/name) for name in sorted(paths)}


def prepare():
    cpu.require(not PACKAGE.exists() and not OUTPUT.exists(),'preserve prior package/run')
    cpu.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit reviewed stage first')
    cpu.require(cpu.read(BASE/'cpu-finish/result.json')['passed'] and
                finish_cpu.gate()==cpu.read(BASE/'cpu-finish/result.json')['bindings'],'CPU gate')
    cpu.require(cpu.read(BASE/'driver-cpu-v3/result.json')['passed'],'driver gate')
    cpu.require(cpu.sources()==cpu.read(BASE/'build-01/sources.json'),'CPU/build source drift')
    old=cpu.read(ROOT/'build/q4t.release.json')
    previous=cpu.read(old['tested_source_manifest'])
    runtime={name:cpu.sha(ROOT/name) for name in previous}
    cpu.require({name for name in previous if previous[name]!=runtime[name]}==cpu.CHANGED,'unreviewed runtime delta')
    PACKAGE.mkdir()
    for name in ('candidate','previous'):(PACKAGE/name).mkdir()
    for name in a.NAMES:h.copy_checked(h.identity(ROOT/'build'/name),PACKAGE/'previous'/name)
    h.copy_checked(h.identity(old['build_cache']),PACKAGE/'previous/CMakeCache.txt')
    for name in ('q4t','CMakeCache.txt','compile_commands.json'):
        h.copy_checked(h.identity(cpu.ENGINE/name),PACKAGE/'candidate'/name)
    import shutil
    shutil.copytree(cpu.ENGINE/'generated',PACKAGE/'candidate/generated')
    cpu.save(PACKAGE/'candidate/runtime-source.json',runtime)
    for name in ('candidate','previous'):
        h.copy_checked(h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'),PACKAGE/name/'q4t.rules.yml')
    cpu.save(PACKAGE/'numerical-reuse.json',dict(changed=sorted(cpu.CHANGED),
        previous_manifest=h.identity(old['tested_source_manifest']),
        current_manifest=h.identity(PACKAGE/'candidate/runtime-source.json'),
        model_math_tokenizer_template_cache_scheduler_unchanged=True,
        differential=h.identity(BASE/'cpu-01/differential.json'),performance_acceptance=False))
    private=BASE/'private';private.mkdir(mode=0o700)
    cert,key=s.certificate(private)
    front=s.example(TLS_PORT,PORT)
    front.update(per_client_inflight=4,global_inflight=8,per_client_rps=100,burst=128)
    policy=dict(version=1,gateway_token=s.secrets.token_hex(32),clients=[
        dict(id='alpha',max_inflight=1,max_control_inflight=2,max_prompt_tokens=12000,
             max_output_tokens=512,max_body_bytes=131072),
        dict(id='beta',max_inflight=1,max_control_inflight=2,max_prompt_tokens=9000,
             max_output_tokens=64,max_body_bytes=65536)])
    s.g.save(private/'clients.json',json.dumps(policy).encode())
    s.g.save(private/'gateway.json',json.dumps(front).encode())
    gateway=s.g.prepare(private/'gateway.json',cpu.read(s.VENDOR)['binary'],cert,key,
                        private/'gateway',private/'clients.json')
    prior=cpu.read(ROOT/'.q4t-work/serve-clients-20261104/cache-plan.json')
    protocol=cpu.read(PACKAGE/'candidate/generated/q4t/server/chat_protocol_sources.json')
    digest=hashlib.sha256(''.join(f'{name}:{protocol[name]}\n' for name in sorted(protocol)).encode()).hexdigest()
    cpu.save(PACKAGE/'sources.json',source_paths())
    originals=cpu.read(ROOT/'.q4t-work/serve-task-trust-20261104/plan.json')
    # Keep the prior stage's packages and private inputs intact; no credential output.
    preserved={str(path):cpu.sha(path) for path in DAYS}
    for name in originals['private']:
        preserved[name]=cpu.sha(name)
    for item in originals['original_packages']:
        path=Path(item['package'])/'manifest.json';preserved[str(path)]=cpu.sha(path)
    plan=dict(root=str(ROOT),limits=LIMITS,port=PORT,tls_port=TLS_PORT,
        candidate=str(PACKAGE/'candidate/q4t'),binary=str(PACKAGE/'candidate/q4t'),
        candidate_sha256=cpu.sha(PACKAGE/'candidate/q4t'),protocol_sha256=digest,protocol=digest,
        previous_binary_sha256=cpu.sha(PACKAGE/'previous/q4t'),
        old_hashes={name:cpu.sha(PACKAGE/'previous'/name) for name in a.NAMES},
        model_dir=prior['model_dir'],model_inputs=prior['model_inputs'],
        gateway=gateway,frontend=str(private/'gateway.json'),policy=str(private/'clients.json'),
        local={str(p):cpu.sha(p) for p in (private/'gateway.json',private/'clients.json',
            BASE/'cpu-01/result.json',BASE/'cpu-finish/result.json',
            BASE/'driver-cpu-v3/result.json')},preserved=preserved,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        planned=dict(models=1,successes=6,negatives=36,real_tools=2),performance_acceptance=False)
    cpu.save(PACKAGE/'plan.json',plan);h.seal(PACKAGE)
    return dict(prepared=True,limits=LIMITS,models_started=0)


def check():
    h.verify_seal(PACKAGE);plan=cpu.read(PACKAGE/'plan.json')
    cpu.require(plan['root']==str(ROOT) and plan['limits']==LIMITS,'stage scope')
    a.verify_hashes(ROOT,cpu.read(PACKAGE/'sources.json'))
    a.verify_hashes(ROOT,cpu.read(PACKAGE/'candidate/runtime-source.json'))
    a.verify_hashes(plan['model_dir'],plan['model_inputs'])
    for path,digest in {**plan['preserved'],**plan['local']}.items():cpu.require(cpu.sha(path)==digest,'fixed artifact changed')
    for path in DAYS:cpu.require(not (path.parent/'execution').exists(),'user day executed')
    s.g.check(plan['gateway']['package'],plan['gateway']['manifest_sha256'])
    if (OUTPUT/'begin.json').exists():
        plan['started']=plan['original_started']=cpu.read(OUTPUT/'begin.json')['started']
    return plan


class Run(s.Run):
    def __init__(self,plan):
        s.OUT=OUTPUT/'business';s.PORT=PORT;s.TLS_PORT=TLS_PORT;s.LIMITS=LIMITS
        s.OUT.mkdir()
        super().__init__(plan)
        self.children=Children(OUTPUT/'lease')
        self.current_client=0;self.negative_results=[]

    def complete(self,label,body,client=None):
        client=self.current_client if client is None else client
        body=copy.deepcopy(body)
        body['max_tokens']=min(body.get('max_tokens',512),512 if client==0 else 64)
        return super().complete(label,body,client)

    def negatives(self):
        chosen=[row for row in cases() if row['name'] in HTTP_CASES]
        cpu.require(len(chosen)==12,'frozen HTTP matrix')
        before=self.idle('before-negative')
        for index,row in enumerate(chosen):
            for kind in ('json','stream','preflight'):
                label=f'negative-{row["name"]}-{kind}'
                body=copy.deepcopy(row['request'])
                body.update(request_id=label,cancel_token=s.secrets.token_hex(32),max_tokens=64)
                body['stream']=kind=='stream'
                path='/v1/chat/preflight' if kind=='preflight' else '/v1/chat/completions'
                wrapped=dict(request=body) if kind=='preflight' else body
                response=self.request(label,wrapped,expected=400,path=path,client=index%2,
                                      headers={'X-Request-Id':label})
                error=response['error'];receipt=cpu.read(s.OUT/(label+'-http.json'))
                headers={k.lower():v for k,v in receipt['headers'].items()}
                cpu.require(error['param']==row['param'] and error['code']=='invalid_request' and
                    error['type']=='invalid_request_error','wrong error location/code: '+label)
                cpu.require(headers.get('x-request-id')==label and
                    headers.get('content-type','').startswith('application/json') and
                    headers.get('x-q4t-gateway-request-id'),'error framing/identity: '+label)
                self.negative_results.append(dict(label=label,param=error['param'],client=index%2))
        after=self.idle('after-negative')
        for key in ('q4t_requests_success_total','q4t_structured_workers_started_total',
                    'q4t_structured_workers_reaped_total'):
            cpu.require(before[key]==after[key]==0,'negative caused execution: '+key)
        cpu.save(s.OUT/'negatives.json',self.negative_results)

    def execute(self):
        result=dict(status='FAILED',limits=LIMITS,counts=self.counts)
        try:
            s.a.require_idle(s.OUT/'initial-gpu.json')
            for port in (PORT,TLS_PORT):
                with socket.socket() as sock:sock.bind(('127.0.0.1',port))
            self.launch(True)
            self.negatives()
            value=self.complete('ordinary',s.a.ordinary(True))
            cpu.require(value.finish_reason=='stop' and value.message['content'].strip()=='710003','ordinary quality')
            value=self.complete('structured',s.a.strict_json())
            cpu.require(value.finish_reason=='stop' and json.loads(value.message['content'])=={'ok':True},'strict quality')
            for client,name in enumerate(('alpha','beta')):
                self.current_client=client;self.tools('project-'+name)
            metrics=self.idle('final-idle');cpu.save(s.OUT/'final-metrics.json',metrics)
            cpu.require(self.successes==metrics['q4t_requests_success_total']==6 and
                metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==5,
                'generation/grammar accounting')
            self.stop(True)
            self.children.reap(s.OUT)
            s.a.require_idle(s.OUT/'final-gpu.json')
            result.update(status='PASS_BOUNDED_ERROR_PARAMS',successes=self.successes,
                          negatives=len(self.negative_results),tools=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED',error=repr(error))
            raise
        finally:
            for connection in self.sockets:connection.close()
            try:self.children.cleanup()
            except BaseException as error:
                result.update(status='FAILED',cleanup_error=repr(error));raise
            finally:
                result['seconds']=time.monotonic()-self.started
                cpu.save(s.OUT/'result.json',result)


def verifier(plan,installed=False):
    if not installed:
        Run(plan).execute();return
    cpu.require(cpu.sha(ROOT/'build/q4t')==plan['candidate_sha256'] and
                cpu.read(OUTPUT/'business/result.json')['status']=='PASS_BOUNDED_ERROR_PARAMS','accepted ELF mismatch')
    folder=OUTPUT/'health';folder.mkdir()
    cpu.require(cpu.command(folder,'version',[str(ROOT/'build/q4t'),'version'],10),'installed version')
    cpu.save(folder/'result.json',dict(passed=True,installed=h.identity(ROOT/'build/q4t'),
        acceptance=h.identity(OUTPUT/'business/result.json'),new_model_starts=0,new_http=0))


def release(plan):
    result=OUTPUT/'business/result.json'
    cpu.require(cpu.read(result)['status']=='PASS_BOUNDED_ERROR_PARAMS','business gate')
    metadata=cpu.read(PACKAGE/'previous/q4t.release.json')
    inherited=copy.deepcopy({key:metadata[key] for key in ('acceptance','acceptance_decision',
        'acceptance_decision_sha256','chat_protocol','deployment_completion','scope') if key in metadata})
    inherited['release']=h.identity(PACKAGE/'previous/q4t.release.json')
    metadata.pop('deployment_completion',None)
    metadata.pop('implementation_capabilities',None)
    caps=cpu.read(OUTPUT/'business/scoped-model-config-wire.bin')
    metadata.update(version='serve-error-params-20261104',binary_sha256=plan['candidate_sha256'],
        source_commit=plan['source_commit'],tool_source_commit=plan['source_commit'],
        source_note='Explicit tool/control/recursive schema error paths; unchanged input acceptance, messages, codes, templates and grammar.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=cpu.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),build_cache_sha256=cpu.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_ERROR_PARAMS',acceptance_decision=str(result),acceptance_decision_sha256=cpu.sha(result),
        tool_acceptance=str(result),tool_acceptance_sha256=cpu.sha(result),inherited_acceptance=inherited,
        previous_binary=str(PACKAGE/'previous/q4t'),previous_release=str(PACKAGE/'previous/q4t.release.json'),
        observed_configuration=caps,operations_rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'),
        rollback_operations_rules=h.identity(PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor',production='default MTP k3, full head, text',
            validation='36 TLS input refusals; ordinary and strict JSON plus two real project tool/citation round trips',
            installation_health='Identical accepted ELF and installed CLI; HTTP reused',
            boundaries='No actual LAN or long soak, physical RAM or new numerical/performance acceptance; fixed old packages and task identities do not auto-upgrade',
            performance_acceptance=False,numerical_evidence=str(PACKAGE/'numerical-reuse.json')))
    metadata['chat_protocol'].update(protocol_sha256=plan['protocol_sha256'],
        declaration_evidence='Capability shape/template unchanged; explicit error paths verified in current HTTP and CPU differential.')
    folder=OUTPUT/'release';folder.mkdir()
    cpu.save(folder/'q4t.release.json',metadata)
    cpu.save(folder/'binding.json',dict(old=plan['old_hashes'],new=dict(q4t=plan['candidate_sha256'],
             **{'q4t.release.json':cpu.sha(folder/'q4t.release.json')})))
    h.seal(folder);return folder


def bind():
    a.BASE,a.PACKAGE,a.OUTPUT=BASE,PACKAGE,OUTPUT
    a.MARKER=ROOT/'build/serve-error-params-recovery.json'
    a.check,a.verifier,a.release=check,verifier,release


def execute():
    bind();h.require_guard(OUTPUT)
    cpu.save(OUTPUT/'begin.json',dict(started=time.time(),limits=LIMITS))
    def expired(*_):raise RuntimeError('short work deadline; cleanup reserve only')
    signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,LIMITS['seconds']-45)
    try:a.execute()
    finally:signal.setitimer(signal.ITIMER_REAL,0)


def finalize():
    bind();a.finalize();h.seal(OUTPUT)


def rollback(output):
    h.verify_seal(PACKAGE);plan=cpu.read(PACKAGE/'plan.json')
    h.verify_pair(PACKAGE/'previous/q4t',PACKAGE/'previous/q4t.release.json',plan['previous_binary_sha256'])
    bind();a.check=lambda:plan;a.restore(output)


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','execute','finalize','check','rollback'))
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.action=='rollback':
        cpu.require(args.output is not None,'fresh rollback lease output required');rollback(args.output.resolve())
    else:
        value=globals()[args.action]()
        print(json.dumps(value if args.action=='prepare' else dict(action=args.action,passed=True)))
