"""One lease for bounded client TLS probes and six real model responses."""
import argparse
import copy
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_client_transport import cpu, probes
from tools.serve_clients import short as shared
from tools.serve_error_params.stage import Children, DAYS
from tools.serve_protocol import stage as protocol
from tools.serve_protocol.js_client import JsSdkClient
from tools.serve_service import package as pkg

BASE = cpu.BASE
PLAN, OUT = BASE/'plan.json', BASE/'execution'
PORT, TLS_PORT = 18138, 18458
LIMITS = dict(starts=1, generation_attempts=6, controls=120, network=80, seconds=360)
WORK_SECONDS = 315
CPU_LABEL = 'cpu-01'


def source_files():
    names = subprocess.check_output(['git', 'ls-files', 'tools', 'cmake'], cwd=ROOT, text=True).splitlines()
    names += ['docs/MODEL.md', 'docs/SERVE_CLIENT_CLOSEOUT_2026-11-05.md']
    names += [str(p.relative_to(ROOT)) for p in (ROOT/'docs').glob('serve-*.json')]
    return {name: pkg.sha(ROOT/name) for name in sorted(set(names))}


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve original attempt')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT), 'commit reviewed stage first')
    pkg.require(pkg.read(BASE/CPU_LABEL/'result.json')['passed'], 'CPU gate required')
    for name, digest in pkg.read(BASE/CPU_LABEL/'sources.json').items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'CPU source drift: '+name)
    release = pkg.read(ROOT/'build/q4t.release.json')
    pkg.verify_declarations(release, required=True)
    runtime = pkg.read(release['tested_source_manifest'])
    pkg.require(len(runtime) == 223 and all(pkg.sha(ROOT/n) == d for n,d in runtime.items()), 'native source drift')
    sdk = protocol.sdk_identity()
    private = BASE/'private'
    private.mkdir(mode=0o700)
    cert, key = shared.certificate(private)
    (private/'wrong').mkdir()
    wrong_ca, _ = shared.certificate(private/'wrong')
    front = shared.example(TLS_PORT, PORT)
    front.update(version=2, allowed_client_cidrs=['127.0.0.1/32'],
        per_client_inflight=4, global_inflight=8, per_client_rps=100, burst=128)
    policy = dict(version=1, gateway_token=shared.secrets.token_hex(32), clients=[
        dict(id='alpha', max_inflight=1, max_control_inflight=2, max_prompt_tokens=8192,
             max_output_tokens=512, max_body_bytes=131072),
        dict(id='beta', max_inflight=1, max_control_inflight=2, max_prompt_tokens=8192,
             max_output_tokens=512, max_body_bytes=131072)])
    pkg.save(private/'gateway.json', front)
    pkg.save(private/'clients.json', policy)
    gateway = shared.g.prepare(private/'gateway.json', pkg.read(shared.VENDOR)['binary'],
        cert, key, private/'gateway', private/'clients.json', trusted_ca=cert)
    previous = pkg.read(ROOT/'.q4t-work/serve-gateway-sources-20261104/plan.json')
    preserved = dict(previous['preserved'])
    preserved.update({str(p):pkg.sha(p) for p in DAYS})
    pkg.save(BASE/'sdk.json', sdk)
    value = dict(root=str(ROOT), limits=LIMITS, work_seconds=WORK_SECONDS, port=PORT, tls_port=TLS_PORT,
        binary=str(ROOT/'build/q4t'), protocol=release['chat_protocol']['protocol_sha256'],
        default={n:pkg.sha(ROOT/n) for n in ('build/q4t','build/q4t.release.json')}, runtime=runtime,
        model_dir=previous['model_dir'], model_inputs=previous['model_inputs'],
        frontend=str(private/'gateway.json'), policy=str(private/'clients.json'), gateway=gateway,
        cert=str(cert), key=str(key), wrong_ca=str(wrong_ca),
        sdk_sha256=pkg.sha(BASE/'sdk.json'), sdk_directory=sdk['sdk_directory'],
        preserved=preserved, day_plans=list(map(str,DAYS)),
        local={str(p):pkg.sha(p) for p in (private/'gateway.json',private/'clients.json',cert,key,wrong_ca)},
        sources=source_files(), cpu_receipt=pkg.sha(BASE/CPU_LABEL/'result.json'),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        planned_network_cases=2*len(probes.CASES), planned_successes=6, planned_real_tools=2,
        runtime_config=dict(max_seq=1,max_len=8192,max_prefill=2048,mtp_k=3),
        performance_acceptance=False, installation=False, long_run=False)
    pkg.save(PLAN,value)
    check()
    return dict(prepared=True, limits=LIMITS, model_starts=0)


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['limits'] == LIMITS and plan['work_seconds'] == WORK_SECONDS and
                (plan['port'],plan['tls_port']) == (PORT,TLS_PORT), 'frozen scope changed')
    for name,digest in {**plan['sources'],**plan['runtime'],**plan['default']}.items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'source/default drift: '+name)
    for name,digest in {**plan['local'],**plan['preserved']}.items():
        pkg.require(pkg.sha(name) == digest, 'local/preserved identity changed')
    for name,digest in plan['model_inputs'].items():
        pkg.require(pkg.sha(Path(plan['model_dir'])/name) == digest, 'model metadata changed')
    for name in plan['day_plans']:
        pkg.require(not (Path(name).parent/'execution').exists(), 'user long test started')
    pkg.require(pkg.sha(BASE/CPU_LABEL/'result.json') == plan['cpu_receipt'] and
                pkg.sha(BASE/'sdk.json') == plan['sdk_sha256'], 'gate identity changed')
    pkg.require(protocol.sdk_identity() == pkg.read(BASE/'sdk.json'), 'Node/SDK identity changed')
    shared.g.check(plan['gateway']['package'],plan['gateway']['manifest_sha256'])
    return plan


def verify_counts(counts, successes, real_tools):
    pkg.require(counts['starts'] == 1 and counts['generation_attempts'] == successes == 6 and
                real_tools == 2 and counts['network'] == 2*len(probes.CASES), 'workload accounting')


class Run(shared.Run):
    def __init__(self, plan):
        shared.OUT, shared.PORT, shared.TLS_PORT = OUT, PORT, TLS_PORT
        super().__init__(plan)
        self.children = Children(OUT/'lease')
        self.counts['network'] = 0
        self.real_tools = 0
        self.clients = {}

    def budget(self, kind):
        pkg.require(time.monotonic()-self.started < WORK_SECONDS, 'work window expired; reserve cleanup')
        pkg.require(self.counts[kind] < LIMITS[kind], 'attempt budget: '+kind)
        self.counts[kind] += 1
        pkg.save(OUT/f'attempt-{kind}-{self.counts[kind]:03d}.json',dict(self.counts))

    def launch_model(self):
        self.budget('starts')
        env = {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and
               k not in ('LD_PRELOAD','LD_LIBRARY_PATH')}
        command = [self.plan['binary'],'serve','--model-dir',self.plan['model_dir'],
            '--host','127.0.0.1','--port',str(PORT),'--max-seq','1','--max-len','8192',
            '--max-prefill','2048','--max-inflight-requests','8','--max-queued-requests','2',
            '--client-policy-file',self.plan['policy']]
        self.model = self.children.launch(OUT/'scoped-model',command,env)
        until = min(self.started+WORK_SECONDS,time.monotonic()+180)
        while 'serving on port' not in (OUT/'scoped-model/stderr.log').read_text():
            pkg.require(self.children.running(self.model) and time.monotonic()<until, 'model startup failed')
            time.sleep(.1)
        config = self.request('config',path='/v1/config',method='GET',direct=True)
        pkg.require(config['protocol_source_sha256'] == self.plan['protocol'], 'wrong native protocol')
        package = Path(self.plan['gateway']['package'])
        manifest = shared.g.check(package,self.plan['gateway']['manifest_sha256'])
        self.nginx = self.children.launch(OUT/'nginx',[manifest['nginx']['path'],'-e','stderr',
            '-p',str(package)+'/','-c',str(package/'nginx.conf')],env)
        until = time.monotonic()+10
        while True:
            pkg.require(self.children.running(self.nginx) and time.monotonic()<until,'gateway startup failed')
            try:
                with socket.create_connection(('127.0.0.1',TLS_PORT),.1):
                    break
            except ConnectionRefusedError:
                time.sleep(.05)
        self.tls = True

    def complete_with(self, label, body, client):
        self.budget('generation_attempts')
        pkg.save(OUT/(label+'-request.json'),body)
        chunks = []
        began = time.monotonic()
        try:
            result = client.complete(body,sink=chunks.append)
        finally:
            (OUT/(label+'-wire.bin')).write_bytes(b''.join(chunks))
        self.successes += 1
        pkg.save(OUT/(label+'-response.json'),dict(asdict(result),seconds=time.monotonic()-began))
        return result

    def tool_loop(self, label, client, stream):
        a = shared.a
        tools = [copy.deepcopy(a.TOOLS[0])]
        tools[0]['function']['strict'] = True
        tools[0]['function']['parameters']['properties']['path'] = dict(type='string',enum=['docs/MODEL.md'])
        history = [dict(role='system',content=a.SYSTEM),dict(role='user',content=
            '先调用一次read_project_record读取docs/MODEL.md的第1到24行，start_line=1、line_count=24。'
            '再回答architectures字段的准确值，并附工具给出的引用。')]
        body = a.make_request(history,tools,stream=stream,max_tokens=512,timeout_ms=30000)
        body.update(tool_choice='required',parallel_tool_calls=False)
        first = self.complete_with(label+'-read',body,client)
        calls = a.prepare_tool_calls(first,a.VALIDATORS)
        pkg.require(len(calls)==1 and calls[0].arguments ==
            dict(path='docs/MODEL.md',start_line=1,line_count=24),'unexpected tool invocation')
        call = calls[0]
        pkg.save(OUT/(label+'-tool-started.json'),asdict(call))
        value = a.ProjectRecords(ROOT).execute(call)
        pkg.require(value['ok'],'real project tool failed')
        self.real_tools += 1
        value['application_budget'] = dict(remaining_model_requests=1,remaining_tool_calls=0,final_response_required=True)
        content = json.dumps(value,ensure_ascii=False)
        pkg.save(OUT/(label+'-tool-result.json'),dict(tool_call_id=call.call_id,content=content))
        history = a.append_tool_results(history,first,{call.call_id:content})
        body = a.make_request(history,tools,stream=not stream,max_tokens=512,timeout_ms=30000)
        body['tool_choice'] = 'none'
        answer = self.complete_with(label+'-answer',body,client)
        a.validate_answer(answer.message['content'],{value['source']['citation']},1)
        pkg.require(answer.finish_reason == 'stop' and 'Qwen4ExpForConditionalGeneration' in
                    answer.message['content'],'real answer/citation failed')

    def business(self):
        base = f'https://{self.front["server_name"]}:{TLS_PORT}/v1'
        from tools.examples.chat_client import ChatClient, RemoteError
        clients = [ChatClient(base,api_key=self.front['clients'][0]['api_key'],
                    ca_file=self.plan['cert'],connect_host='127.0.0.1',timeout=35),
            JsSdkClient(base,self.plan['sdk_directory'],protocol_sha256=self.plan['protocol'],
                api_key=self.front['clients'][1]['api_key'],ca_file=self.plan['cert'],
                connect_host='127.0.0.1',timeout=35)]
        from tools.serve_protocol.js_client import JsSdkError
        for i,client in enumerate(clients):
            label = 'python' if i == 0 else 'node'
            self.budget('controls')
            caps = client.capabilities(protocol_sha256=self.plan['protocol'])
            pkg.save(OUT/(label+'-capabilities.json'),caps)
            body = shared.a.ordinary(bool(i));body['request_timeout_ms']=30000
            value = self.complete_with(label+'-ordinary',body,client)
            pkg.require(value.message['content'].strip()=='710003','ordinary quality')
            self.tool_loop(label,client,bool(i))
            self.idle(label+'-idle')
        for cls,error_type in ((ChatClient,RemoteError),(JsSdkClient,JsSdkError)):
            kwargs = dict(api_key='invalid-key',ca_file=self.plan['cert'],connect_host='127.0.0.1',timeout=3)
            bad = cls(base,**kwargs) if cls is ChatClient else cls(base,self.plan['sdk_directory'],**kwargs)
            self.budget('controls')
            try:
                bad.capabilities()
            except error_type as error:
                status = error.status if isinstance(error,RemoteError) else error.details.get('status')
                pkg.require(status==401,'wrong Key error contract')
            else:
                raise RuntimeError('wrong Key accepted')
        pkg.save(OUT/'final-metrics.json',self.idle('final-idle'))

    def execute(self):
        lock = shared.a.guard.common_lock()
        st = lock.stat()
        pkg.require((st.st_dev,st.st_ino)==(66305,3294067),'original GPU lock identity')
        lease = pkg.read(lock)
        pkg.require(lease['pid']==os.getppid() and lease['state']=='held' and
                    not shared.a.lock_available(lock),'full-stage GPU lease required')
        result = dict(status='FAILED',limits=LIMITS,installed=False,long_run=False)
        def interrupted(*_):
            raise RuntimeError('bounded stage interrupted')
        handlers = {sig:signal.signal(sig,interrupted) for sig in (signal.SIGALRM,signal.SIGINT,signal.SIGTERM)}
        signal.alarm(WORK_SECONDS)
        try:
            shared.a.require_idle(OUT/'initial-gpu.json')
            probes.execute(OUT/'network',self.plan['cert'],self.plan['key'],self.plan['wrong_ca'],
                           self.plan['sdk_directory'],self.budget)
            self.launch_model()
            self.business()
            self.children.signal(self.nginx,signal.SIGQUIT)
            self.children.signal(self.model,signal.SIGTERM)
            self.children.wait(self.nginx,OUT/'nginx',20)
            self.children.wait(self.model,OUT/'scoped-model',20)
            pkg.require('shutdown complete (0 in-flight remaining)' in
                (OUT/'scoped-model/stderr.log').read_text(),'backend drain missing')
            self.children.reap(OUT)
            shared.a.require_idle(OUT/'final-gpu.json')
            verify_counts(self.counts,self.successes,self.real_tools)
            check()
            result.update(status='PASS_CLIENT_TRANSPORT_SHORT',successes=self.successes,
                          real_tools=self.real_tools,cleanup_confirmed=True)
        except BaseException as error:
            result['error'] = repr(error)
            raise
        finally:
            signal.alarm(0)
            try:
                for connection in self.sockets:
                    connection.close()
                self.children.cleanup()
            finally:
                for sig,handler in handlers.items():
                    signal.signal(sig,handler)
                result.update(counts=self.counts,seconds=time.monotonic()-self.started)
                pkg.save(OUT/'result.json',result)
        pkg.require(result['seconds']<=LIMITS['seconds'],'overall stage budget exceeded')
        return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','check','run'))
    args = parser.parse_args()
    if args.action == 'prepare':
        result = prepare()
    else:
        plan = check()
        if args.action == 'run':
            pkg.require(not (OUT/'result.json').exists(),'preserve original execution')
            with shared.sup.processes.child_subreaper():
                result = Run(plan).execute()
        else:
            result = dict(checked=True,models=0)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
