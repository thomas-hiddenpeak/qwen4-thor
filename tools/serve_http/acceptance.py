"""Bounded N05 transport acceptance with one real managed model generation.

Only this validation harness accepts the exact pending release. Runtime module
verification, ownership, admission and cleanup remain production code. A local
CPU proxy is a test peer, never a required deployed process.
"""
import argparse
from contextlib import contextmanager
import copy
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_access.acceptance import fingerprint, verify_sdk

spec = importlib.util.spec_from_file_location('q4t_http_common', ROOT/'tools/serve_recovery/acceptance.py')
common = importlib.util.module_from_spec(spec);spec.loader.exec_module(common)
common.__file__ = __file__
original_load = common.load_modules
LIMITS = dict(starts=1, chats=10, probes=0, controls=2000, seconds=600)
COMPLETION_LIMITS = dict(LIMITS, starts=2, chats=12)
save, Budget = common.save, common.Budget
BODY_LIMIT = 4*1024*1024


def require(value, message):
    if not value:raise ValueError(message)


def validation_gate(original, expected):
    def check(metadata, decision):
        if metadata.get('validation_only') is not True:return original(metadata, decision)
        require(metadata.get('acceptance') == 'VALIDATION_ONLY_N05' and
            decision.get('status') == 'PENDING_REAL_BUSINESS_N05' and
            fingerprint(metadata) == expected['metadata'] and
            fingerprint(decision) == expected['decision'], 'N05 pending release identity changed')
    return check


def load_modules(plan=None):
    result = original_load(plan)
    if plan:
        engine = result[-1]
        original = getattr(engine, '_n05_original_acceptance', engine.accepted_release)
        engine._n05_original_acceptance = original
        engine.accepted_release = validation_gate(original, plan['validation_release'])
    return result


def verify(plan):
    bundle, _, _, _, _, engine = load_modules(plan)
    limits = COMPLETION_LIMITS if plan.get('resume_after_prefix') else LIMITS
    require(plan['scenario'] == 'native_http_transport' and plan['limits'] == limits,
            'N05 frozen budget changed')
    for name, digest in plan['sources'].items():
        require(engine.sha(ROOT/name) == digest, 'N05 source changed: '+name)
    for row in [*plan['protected'], *plan['tls_inputs'].values()]:
        require(engine.sha(row['path']) == row['sha256'], 'N05 protected input changed')
    verify_sdk(plan['sdk'])
    verify_prior(plan)
    value = bundle.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    require(value['version'] == 9 and value['ingress_mode'] == 'native' and value['tls'],
            'N05 requires native TLS transport profile')
    return value


def verify_prior(plan):
    if not plan.get('resume_after_prefix'):return
    import hashlib
    for row in plan['prior_evidence']:
        require(hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()==row['sha256'],
                'N05 prior evidence changed')
    prior=json.loads(Path(plan['prior_execution']).read_bytes())
    guard=json.loads(Path(plan['prior_guard']).read_bytes())
    counts=prior['budget']['counts']
    require(prior['status']=='FAILED' and prior['cleanup_failure'] is None and
        prior['package']==plan['package'] and guard['status']=='COMMAND_FAILED' and
        'Node bridge failed before response' in prior['failure'] and
        (counts['starts'],counts['chats'],counts['probes'])==(1,5,0) and
        plan['prior_counts']==counts and plan['prior_seconds']>=prior['seconds'] and
        len(prior['models'])==1,'N05 failure cannot be reused as prefix completion')
    stopped=json.loads(Path(plan['prior_stopped']).read_bytes())
    require(stopped['desired']=='STOPPED' and not any(stopped['active'].values()),
            'N05 previous service not stopped')


common.load_modules, common.verify = load_modules, verify


def read_response(reader):
    """Require an actually terminated bounded HTTP frame, including chunk zero."""
    wire = bytearray()
    def line():
        value = reader.readline(8193)
        require(len(value) <= 8192 and value.endswith(b'\r\n'), 'incomplete HTTP line')
        wire.extend(value)
        require(len(wire) <= BODY_LIMIT+65536, 'response wire bound')
        return value
    status_line = line().decode('ascii').strip().split(' ', 2)
    require(len(status_line) >= 2 and status_line[0] in ('HTTP/1.0','HTTP/1.1'), 'status line')
    status = int(status_line[1]);headers = {}
    while (raw := line()) != b'\r\n':
        require(len(wire) <= 65536, 'header bound')
        key,value = raw.decode('ascii').rstrip('\r\n').split(':',1)
        key = key.lower()
        require(key not in headers, 'duplicate response header')
        headers[key] = value.strip()
    body = bytearray()
    if status >= 200 and status != 204:
        require(not ('content-length' in headers and 'transfer-encoding' in headers), 'mixed framing')
        chunked = headers.get('transfer-encoding') == 'chunked'
        require(chunked or 'content-length' in headers, 'bounded response framing required')
        while True:
            size = int(line().strip(),16) if chunked else int(headers['content-length'])
            require(0 <= size <= BODY_LIMIT-len(body), 'response body bound')
            value = reader.read(size);require(len(value) == size, 'truncated response body')
            body.extend(value);wire.extend(value)
            if chunked:require(line() == b'\r\n', 'bad chunk delimiter')
            if not chunked or size == 0:break
    return status, headers, bytes(body), bytes(wire)


def request_head(path, fields, *, chunked=False, length=0, method='POST', close=False):
    text = f'{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n'
    text += ''.join(f'{k}: {v}\r\n' for k,v in fields.items())
    text += ('Transfer-Encoding: chunked\r\nExpect: 100-continue\r\n' if chunked
             else f'Content-Length: {length}\r\n')
    if close:text += 'Connection: close\r\n'
    return (text+'\r\n').encode()


def exchange(sock, reader, path, body, fields, *, chunked=False):
    raw = json.dumps(body).encode() if body is not None else b''
    sock.sendall(request_head(path,fields,chunked=chunked,length=len(raw),
                             method='POST' if body is not None else 'GET'))
    if chunked:
        interim = read_response(reader)
        require(interim[0] == 100 and not interim[2], 'missing authenticated 100')
        for at in range(0,len(raw),31):
            part=raw[at:at+31];sock.sendall(f'{len(part):x}\r\n'.encode()+part+b'\r\n')
        sock.sendall(b'0\r\n\r\n')
    elif raw:sock.sendall(raw)
    return read_response(reader)


def answer(label, *, stream=False):
    from tools.examples import chat_client
    value = chat_client.make_request([dict(role='user',content=
        'Compute 17 + 25. Reply with the number only.')],[],stream=stream,
        max_tokens=128,timeout_ms=60000)
    value['request_id'] = 'n05-'+label
    return value


class Stage(common.Stage):
    def __init__(self, plan, plan_path):
        super().__init__(plan,plan_path)
        self.context=ssl.create_default_context(cafile=plan['tls_inputs']['ca']['path'])
        self.prefix=self.policy['http']['path_prefix']

    def public_fields(self, client='alpha'):
        return {'Authorization':'Bearer '+self.keys[client],'Content-Type':'application/json',
                'Origin':'https://client.example'}

    def record(self, label, result, *, expected=200):
        status, headers, raw, wire = result
        (self.output/(label+'-wire.bin')).write_bytes(wire)
        save(self.output/(label+'-http.json'),dict(status=status,headers=headers,bytes=len(raw)))
        require(status == expected, 'HTTP status '+label+': '+str(status))
        return raw

    def chat_result(self, label, body, result, *, arithmetic=True):
        from tools.examples import chat_client
        raw=self.record(label,result)
        value=(chat_client.read_sse if body['stream'] else chat_client.read_json)(io.BytesIO(raw))
        save(self.output/(label+'-response.json'),asdict(value))
        require(value.id==body['request_id'],'completion request identity')
        if arithmetic:require(value.finish_reason=='stop' and value.message['content'].strip()=='42',
                              'task quality '+label)
        return value

    @contextmanager
    def peer(self, *, core=False):
        with socket.create_connection(('127.0.0.1',self.policy['upstream_port'] if core else self.port),65) as raw:
            if core:
                with raw.makefile('rb') as reader:yield raw,reader
            else:
                with self.context.wrap_socket(raw,server_hostname='127.0.0.1') as sock:
                    with sock.makefile('rb') as reader:yield sock,reader

    def generate(self, label, body, sock, reader, *, path=None, fields=None, chunked=False):
        self.budget.reserve('chats',label);save(self.output/(label+'-request.json'),body)
        return exchange(sock,reader,path or self.prefix+'/v1/chat/completions',body,
                        fields or self.public_fields(),chunked=chunked)

    def sdk(self,label,body):
        from tools.serve_protocol.js_client import JsSdkClient
        verify_sdk(self.plan['sdk'],dispatch=True)
        self.budget.reserve('chats',label);save(self.output/(label+'-request.json'),body)
        client=JsSdkClient(f'https://127.0.0.1:{self.port}{self.prefix}/v1',
            self.plan['sdk']['sdk_directory'],protocol_sha256=self.plan['protocol_sha256'],
            timeout=65,api_key=self.keys['alpha'],ca_file=self.plan['tls_inputs']['ca']['path'])
        try:
            with (self.output/(label+'-wire.bin')).open('xb') as wire:value=client.complete(body,sink=wire.write)
        except Exception as error:
            save(self.output/(label+'-client-error.json'),getattr(error,'details',dict(message=str(error))))
            raise
        save(self.output/(label+'-response.json'),asdict(value))
        return value

    def optional_proxy(self):
        stage=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_args):pass
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']))
                conn=http.client.HTTPSConnection('127.0.0.1',stage.port,context=stage.context,timeout=65)
                try:
                    conn.request('POST',self.path,body,{
                        **stage.public_fields(),'Forwarded':'for=192.0.2.9;host=127.0.0.1;proto=http',
                        'X-Forwarded-Prefix':stage.prefix})
                    result=conn.getresponse();raw=result.read(BODY_LIMIT+1)
                    require(len(raw)<=BODY_LIMIT,'proxy response bound')
                    self.send_response(result.status);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                finally:conn.close()
        proxy=ThreadingHTTPServer(('127.0.0.1',0),Handler);proxy.daemon_threads=True
        thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start()
        conn=http.client.HTTPConnection('127.0.0.1',proxy.server_port,timeout=65)
        try:
            body=answer('optional-proxy');self.budget.reserve('chats','optional-proxy')
            save(self.output/'optional-proxy-request.json',body)
            conn.request('POST','/v1/chat/completions',json.dumps(body),{'Content-Type':'application/json'})
            result=conn.getresponse();raw=result.read(BODY_LIMIT+1)
            self.chat_result('optional-proxy',body,(result.status,dict(result.getheaders()),raw,raw))
        finally:
            conn.close();proxy.shutdown();proxy.server_close();thread.join(timeout=3)
            require(not thread.is_alive(),'proxy cleanup')

    def exercise(self):
        first=self.ready(1)
        from tools.serve_ops import transport
        from tools.examples import chat_client
        from tools.examples.project_records import ProjectRecords,VALIDATORS
        from jsonschema import Draft202012Validator
        with self.peer() as (sock,reader):
            caps=json.loads(self.record('capabilities',exchange(sock,reader,self.prefix+'/v1/capabilities',None,self.public_fields())))
            require(caps['protocol_source_sha256']==self.plan['protocol_sha256'] and
                    caps['http']['connection_reuse'] and caps['http']['chunked_requests'],'actual HTTP capability')
            save(self.output/'capabilities.json',caps)
        observed=transport.get(self.policy['upstream_operations_port'],'/v1/config',timeout=3)
        require(observed['http_status']==200,'config observation')
        config=json.loads(observed['body']);save(self.output/'configuration.json',config)
        Draft202012Validator(self.engine.read(ROOT/'docs/serve-config-v1.schema.json')).validate(config)
        if not self.plan.get('resume_after_prefix'):
            policies=list((self.output/'state').glob('**/controller-*/model-1/client-policy.json'))
            require(len(policies)==1,'one actual model credential binding')
            core_policy=self.engine.read(policies[0])
            core_fields={'X-Q4T-Client-Id':'alpha','X-Q4T-Gateway-Token':core_policy['gateway_token'],
                         'Content-Type':'application/json'}
            with self.peer(core=True) as (sock,reader):
                body=answer('core-chunked')
                self.chat_result('core-chunked',body,self.generate('core-chunked',body,sock,reader,
                    path='/v1/chat/completions',fields=core_fields,chunked=True))
                self.record('core-reuse',exchange(sock,reader,'/v1/models',None,core_fields))
            with self.peer() as (sock,reader):
                for label,stream,chunked in [('native-json',False,True),('native-sse',True,False)]:
                    body=answer(label,stream=stream)
                    result=self.generate(label,body,sock,reader,chunked=chunked)
                    if stream:require(result[1].get('transfer-encoding')=='chunked','SSE transport not chunked')
                    require(result[1].get('access-control-allow-origin')=='https://client.example','CORS missing')
                    self.chat_result(label,body,result)
                self.record('native-reuse',exchange(sock,reader,self.prefix+'/v1/models',None,self.public_fields('beta')))
                self.record('native-reauth',exchange(sock,reader,self.prefix+'/v1/models',None,
                    dict(self.public_fields(),Authorization='Bearer invalid')),expected=401)
        value=self.sdk('official-sdk',answer('official-sdk',stream=True))
        require(value.finish_reason=='stop' and value.message['content'].strip()=='42','SDK SSE quality')
        tool=copy.deepcopy(self.plan['requests']['tool']);value=self.sdk('tool',tool)
        calls=chat_client.prepare_tool_calls(value,VALIDATORS)
        require(len(calls)==1 and calls[0].arguments==dict(path='docs/MODEL.md',start_line=1,line_count=3),'tool contract')
        actual=ProjectRecords(ROOT).execute(calls[0]);save(self.output/'tool-execution.json',dict(executions=1,result=actual))
        require(actual['ok'] and actual['source']['sha256']==self.plan['sources']['docs/MODEL.md'],'real document source')
        follow=copy.deepcopy(self.plan['requests']['follow'])
        follow['messages']=chat_client.append_tool_results(tool['messages'],value,
            {calls[0].call_id:json.dumps(actual,ensure_ascii=False)})
        value=self.sdk('tool-final',follow)
        require(value.finish_reason=='stop' and actual['source']['citation'] in value.message['content'],'tool closure')
        with self.peer() as (sock,reader):
            body=dict(model='qwen3.8-flash-next',input='Compute 17 + 25. Reply with the number only.',
                      temperature=0,max_output_tokens=128,reasoning=dict(effort='none'),store=False)
            result=self.generate('responses',body,sock,reader,path=self.prefix+'/v1/responses')
            value=json.loads(self.record('responses',result));save(self.output/'responses-response.json',value)
            text=''.join(p['text'] for item in value['output'] if item['type']=='message'
                         for p in item['content'] if p['type']=='output_text')
            require(value['status']=='completed' and text.strip()=='42','Responses quality')
            prompt=('<|im_start|>user\nCompute 17 + 25. Reply with the number only.<|im_end|>\n'
                    '<|im_start|>assistant\n<think>\n\n</think>\n\n')
            body=dict(model='qwen3.8-flash-next',prompt=prompt,temperature=0,max_tokens=64)
            result=self.generate('completions',body,sock,reader,path=self.prefix+'/v1/completions')
            value=json.loads(self.record('completions',result));save(self.output/'completions-response.json',value)
            require(value['choices'][0]['finish_reason']=='stop' and value['choices'][0]['text'].strip()=='42','Completions quality')
        self.optional_proxy()
        after=self.sample();require((first['model_pid'],first['model_start_ticks'],first['model_generation'])==
            (after['model_pid'],after['model_start_ticks'],after['model_generation']),'unexpected model restart')
        self.metrics('terminal-drain',self.engine.read(self.output/'counts.json')['counts']['chats']-
                     self.plan.get('prior_counts',{}).get('chats',0))
        save(self.output/'business-pass.json',dict(status='PASS_NATIVE_HTTP_TRANSPORT',
            actual_proxy=True,direct_core=True,native_tls=True,official_sdk=True,
            tools_executed=1,model_generations=1))


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('run','launcher','controller'))
    parser.add_argument('--plan',type=Path,required=True);parser.add_argument('--config',type=Path)
    args=parser.parse_args();plan=json.loads(args.plan.read_bytes())
    if args.mode=='launcher':return common.launcher(plan,args.plan)
    if args.mode=='controller':common.controller_actor(plan,args.config);return 0
    from tools.release.release import child_subreaper
    with child_subreaper():return Stage(plan,args.plan).run()


if __name__=='__main__':raise SystemExit(main())
