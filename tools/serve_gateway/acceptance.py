"""One real managed model, two TLS clients, strict tools and cancellation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_gateway import gateway as g, cpu
from tools.serve_gateway.test_gateway import certificate, example, VENDOR
from tools.serve_gateway.transport_tests import TLSConnection
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from jsonschema import Draft202012Validator
from chat_client import RemoteError

BASE=cpu.BASE
PLAN=BASE/'model-plan.json'
OUT=BASE/'model'
LIMITS=dict(starts=1,generations=8,negatives=2,observations=96,seconds=720)
GPU_WINDOW=600
SUPERVISOR_BOUND=492
EXPECTED=dict(successful_requests=7,real_tool_executions=1,rejections=2)
SOURCES=sorted(set((*a.SOURCES,*cpu.SOURCES,'tools/serve_gateway/acceptance.py',
    'tools/serve_gateway/exec_gateway.py','tools/serve_gateway/transport_tests.py',
    'tools/serve_service/controller_exec.py','docs/serve-transport-v1.schema.json')))


def configure():
    a.BASE,a.PLAN,a.OUT=BASE,PLAN,OUT
    a.LIMITS,a.CONTROL_LIMIT,a.GPU_WINDOW=LIMITS,LIMITS['observations'],GPU_WINDOW


def prepare():
    g.require(not PLAN.exists() and not OUT.exists(),'preserve prior model phase')
    # Concurrent research may edit unrelated documents in this shared worktree.
    # Freeze every actual dependency against HEAD without staging that work.
    for name in SOURCES:
        committed=subprocess.check_output(['git','show','HEAD:'+name])
        g.require(committed==(ROOT/name).read_bytes(),'commit reviewed dependency first: '+name)
    tested=pkg.read(BASE/'cpu-complete/result.json')
    g.require(tested['passed'] and tested['effective_tests']==9,'CPU acceptance missing')
    for name,digest in tested['sources'].items():g.require(g.sha(ROOT/name)==digest,'CPU source changed')
    transport=pkg.read(BASE/'transport/result.json')
    g.require(transport['status']=='PASS_BOUNDED_TLS_TRANSPORT' and
        pkg.read(BASE/'transport-lease/exit.json')['status']=='COMPLETED','TLS transport missing')
    for name,digest in pkg.read(BASE/'transport-plan.json')['sources'].items():
        g.require(g.sha(ROOT/name)==digest,'transport source changed: '+name)
    accepted=pkg.read(ROOT/'.q4t-work/serve-inspection-20261103/accepted-package.json')['package']
    manifest=pkg.verify(accepted['package'],accepted['manifest_sha256'])
    g.require(manifest['version']==3 and manifest['settings']['restart_limit']==0,'backend package policy')
    release=pkg.read(ROOT/'build/q4t.release.json')
    for name,digest in pkg.read(release['tested_source_manifest']).items():
        g.require(pkg.sha(ROOT/name)==digest,'runtime source changed')
    private=BASE/'model-private';private.mkdir(mode=0o700)
    cert,key=certificate(private)
    p=example(upstream=manifest['settings']['port'])
    p.update(per_client_inflight=1,global_inflight=4,per_client_rps=100,burst=128)
    policy=private/'policy-private.json';g.save(policy,json.dumps(p).encode())
    gateway=g.prepare(policy,pkg.read(VENDOR)['binary'],cert,key,private/'package')
    pkg.save(PLAN,dict(version=1,limits=LIMITS,package=accepted,gateway=gateway,
        private_policy_sha256=g.sha(policy),cpu_receipt_sha256=g.sha(BASE/'cpu-complete/result.json'),
        transport_result_sha256=g.sha(BASE/'transport/result.json'),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        source_files={name:g.sha(ROOT/name) for name in SOURCES},
        default_release_sha256=g.sha(ROOT/'build/q4t.release.json'),
        unit='no-installed-gateway.service',token='explicit-tls-short',gpu_window_seconds=600,
        supervisor_http_upper_bound=492,performance_acceptance=False))
    return dict(prepared=True,model_or_listener_started=False)


def check():
    p=pkg.read(PLAN)
    g.require(p['limits']==LIMITS and p['gpu_window_seconds']==600,'budget changed')
    for name,digest in p['source_files'].items():g.require(g.sha(ROOT/name)==digest,'source changed: '+name)
    g.require(g.sha(BASE/'cpu-complete/result.json')==p['cpu_receipt_sha256'] and
        g.sha(BASE/'transport/result.json')==p['transport_result_sha256'],'receipt changed')
    pkg.verify(p['package']['package'],p['package']['manifest_sha256'])
    g.check(p['gateway']['package'],p['gateway']['manifest_sha256'])
    g.require(g.sha(BASE/'model-private/policy-private.json')==p['private_policy_sha256'],'keys changed')
    g.require(g.sha(ROOT/'build/q4t')==p['package']['binary_sha256'] and
        g.sha(ROOT/'build/q4t.release.json')==p['default_release_sha256'],'default pair changed')
    return p


def cancelled_wire(raw, request_id):
    validator=Draft202012Validator(pkg.read(ROOT/'docs/serve-transport-v1.schema.json'))
    values=[]
    for line in raw.splitlines(keepends=True):
        if line.startswith(b'data: {') and line.endswith(b'\n'):
            value=json.loads(line[6:]);validator.validate(value);values.append(value)
    errors=[v for v in values if 'error' in v]
    g.require(len(errors)==1 and values[-1]==errors[0] and
        errors[0]['error']['code']=='explicit_cancel' and
        raw.count(b'data: [DONE]')==1 and raw.rstrip().endswith(b'data: [DONE]'),
        'incomplete cancellation terminal')
    chunks=[v for v in values if 'choices' in v]
    g.require(chunks and all(v['id']==request_id for v in chunks) and
        all(c.get('finish_reason') is None for v in chunks for c in v['choices']) and
        all(v.get('usage') is None for v in chunks),'cancelled stream claimed success or changed identity')
    try:a.read_sse(io.BytesIO(raw))
    except RemoteError as error:g.require(error.code=='explicit_cancel','client error classification')
    else:raise ValueError('client accepted interrupted response')
    return dict(passed=True,error_code='explicit_cancel',complete_terminal=True,success=False)


class Run(a.Run):
    def __init__(self,plan):
        super().__init__(plan)
        self.process=self.gateway=self.log=self.gateway_log=None
        self.mutex=threading.RLock()
        self.keys=g.parse(g.read_file(BASE/'model-private/policy-private.json',private=True))
        self.context=ssl.create_default_context(cafile=Path(plan['gateway']['package'])/'certificate.pem')
        self.request_ids=set()

    def budget(self,kind=None):
        with self.mutex:super().budget(kind)

    def launch(self):
        self.log=(OUT/'controller.log').open('xb')
        cmd=[self.manifest['python']['path'],'-I','-B',ROOT/'tools/serve_service/controller_exec.py',
            '--plan',PLAN,'--output',self.run_dir,'--parent',str(os.getpid())]
        self.gpu_started=time.monotonic();signal.setitimer(signal.ITIMER_REAL,GPU_WINDOW)
        self.process=subprocess.Popen(list(map(str,cmd)),stdin=subprocess.DEVNULL,
            stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        state=self.wait_state('READY',1)
        self.controller=pkg.read(self.run_dir/'controller.json')
        self.owners=[self.controller['owner'],state['owner'],state['backend']]
        g.require(self.controller['owner']==sup.process_identity(self.process.pid) and
                  not a.lock_available(a.guard.common_lock()),'controller/lease mismatch')
        pkg.save(OUT/'owners.json',self.owners)
        verified=g.check_backend(self.run_dir,g.check(self.plan['gateway']['package'],
                                                    self.plan['gateway']['manifest_sha256']))
        pkg.save(OUT/'backend-bound.json',verified)
        self.gateway_log=(OUT/'gateway-private.log').open('xb')
        cmd=[self.manifest['python']['path'],'-I','-B',ROOT/'tools/serve_gateway/exec_gateway.py',
            '--parent',str(os.getpid()),'--package',self.plan['gateway']['package'],
            '--manifest-sha256',self.plan['gateway']['manifest_sha256'],'--backend-run',self.run_dir]
        self.gateway=subprocess.Popen(list(map(str,cmd)),stdin=subprocess.DEVNULL,
            stdout=self.gateway_log,stderr=subprocess.STDOUT,start_new_session=True,umask=0o077)
        until=time.monotonic()+10
        while time.monotonic()<until:
            self.budget();g.require(self.gateway.poll() is None,'production gateway exited')
            try:
                with socket.create_connection(('127.0.0.1',self.keys['listen_port']),.1):break
            except ConnectionRefusedError:time.sleep(.02)
        else:raise RuntimeError('gateway startup timeout')
        pkg.save(OUT/'gateway-owner.json',sup.process_identity(self.gateway.pid))

    def wire(self,label,body,*,client=0,expected=200,kind='generations',
             path='/v1/chat/completions',content_event=None):
        self.budget(kind)
        if kind=='generations':
            with self.mutex:
                g.require(body['request_id'] not in self.request_ids,'request id reused')
                self.request_ids.add(body['request_id'])
        pkg.save(OUT/(label+'-request.json'),body)
        conn=TLSConnection(self.keys['server_name'],self.keys['listen_port'],
                           context=self.context,timeout=180)
        headers={'Content-Type':'application/json','X-Request-Id':body['request_id']}
        if client is not None:headers['Authorization']='Bearer '+self.keys['clients'][client]['api_key']
        receipt=dict(eof=False,client=None if client is None else self.keys['clients'][client]['id'])
        raw=bytearray();started=time.monotonic()
        try:
            conn.request('POST',path,json.dumps(body),headers)
            response=conn.getresponse()
            receipt.update(status=response.status,headers=dict(response.getheaders()))
            with (OUT/(label+'-wire.bin')).open('xb',buffering=0) as wire:
                while line:=response.readline(65537):
                    raw.extend(line);wire.write(line)
                    g.require(len(raw)<=8*1024**2 and len(line)<=65536,'wire limit')
                    if content_event and line.startswith(b'data: {'):
                        value=json.loads(line[6:])
                        if any(c.get('delta',{}).get('content') for c in value.get('choices',[])):
                            content_event.set()
            receipt['eof']=True
            g.require(response.status==expected and response.getheader('X-Q4T-Gateway-Request-Id'),
                      'unexpected gateway status/identity: '+label)
            if kind=='generations':
                g.require(response.getheader('X-Request-Id')==body['request_id'],'backend correlation changed')
            return bytes(raw)
        except BaseException as error:receipt['error']=repr(error);raise
        finally:
            receipt['seconds']=time.monotonic()-started
            pkg.save(OUT/(label+'-http.json'),receipt);conn.close()

    def complete(self,label,body,client=0):
        raw=self.wire(label,body,client=client)
        value=(a.read_sse if body['stream'] else a.read_json)(io.BytesIO(raw))
        g.require(value.id==body['request_id'] and value.model==body['model'],'response identity')
        pkg.save(OUT/(label+'-response.json'),asdict(value))
        return value

    def metrics(self,label):
        self.budget('controls')
        response=sup.transport.get(self.port,'/metrics')
        g.require(response['http_status']==200,'metrics unavailable')
        metrics,_=a.contracts.parse_metrics(response['body'],version=4)
        pkg.save(OUT/(label+'.json'),dict(response=response,metrics=metrics))
        return metrics

    def business(self):
        self.wire('unauthorized',a.ordinary(),client=None,expected=401,kind='negatives')
        g.require(self.metrics('unauthorized-metrics')['q4t_requests_total']==0,'unauthorized reached model')
        self.plain('ordinary-alpha')
        result=self.complete('ordinary-beta',a.ordinary(True),client=1)
        g.require(result.finish_reason=='stop' and result.message['content'].strip()=='710003','beta quality')
        result=self.complete('strict',a.strict_json())
        g.require(result.finish_reason=='stop' and json.loads(result.message['content'])=={'ok':True},'strict quality')
        self.tools('project')
        a.healthy_idle(self.metrics('before-cancel'),5)
        self.cancel_business(prior_success=5,grammar_workers=3)

    @contextmanager
    def concurrent_requests(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                yield pool
            except BaseException:
                # Stop the owned backend before executor.__exit__ joins active
                # requests. Otherwise a failed control request waits for the
                # whole long generation instead of draining immediately.
                self.cleaning=True
                signal.setitimer(signal.ITIMER_REAL,0)
                try:self.stop_backend()
                except BaseException as error:
                    pkg.save(OUT/'failure-stop-error.json',dict(error=repr(error)))
                raise

    def cancel_business(self,prior_success,grammar_workers):
        active=a.make_request([dict(role='user',content='逐行列出从1到10000的所有整数，每行一个数字，不要省略，不要解释。')],
            [],stream=True,max_tokens=4096,timeout_ms=180000)
        entered=threading.Event()
        with self.concurrent_requests() as pool:
            running=pool.submit(self.wire,'active-cancel',active,content_event=entered)
            g.require(entered.wait(15),'active model did not produce content')
            queued=pool.submit(self.complete,'queued-beta',a.ordinary(),1)
            until=time.monotonic()+10
            for n in range(40):
                metrics=self.metrics(f'queue-observation-{n:02d}')
                if metrics['q4t_queued_requests']==1 and metrics['q4t_active_chats']==2:break
                g.require(time.monotonic()<until and not running.done(),'queue state not reached')
                time.sleep(.1)
            else:raise RuntimeError('queue observation budget')
            rejected=self.wire('alpha-quota',a.ordinary(),expected=429,kind='negatives')
            g.require(json.loads(rejected)['error']['code']=='gateway_rate_limit','quota error code')
            cancel=dict(request_id=active['request_id'],cancel_token=active['cancel_token'])
            raw=self.wire('explicit-cancel',cancel,expected=202,kind='controls',
                          path='/v1/requests/cancel')
            g.require(json.loads(raw)=={'cancel_requested':True},'cancel acknowledgment')
            pkg.save(OUT/'cancel-verified.json',cancelled_wire(running.result(20),active['request_id']))
            result=queued.result(20)
            g.require(result.finish_reason=='stop' and result.message['content'].strip()=='710003','queued beta quality')
        self.plain('after-cancel-recovery')
        metrics=self.metrics('final-metrics')
        g.require(all(metrics[n]==0 for n in a.EMPTY) and metrics['q4t_seq_slots_free']==1 and
            metrics['q4t_requests_total']==prior_success+3 and
            metrics['q4t_requests_success_total']==prior_success+2 and
            metrics['q4t_requests_aborted_total']==1 and metrics['q4t_requests_error_total']==0 and
            metrics['q4t_structured_workers_started_total']==
            metrics['q4t_structured_workers_reaped_total']==grammar_workers,
            'final request/resource accounting mismatch')

    def stop_backend(self):
        if self.process is not None and self.process.poll() is None:
            if (self.run_dir/'controller.json').exists():self.service_cli('explicit-stop','stop')
            else:self.process.terminate()
            g.require(self.process.wait(50)==0,'controller stop failed')
        if self.process is not None and self.process.poll() is not None and self.gpu_finished is None:
            self.gpu_finished=time.monotonic();signal.setitimer(signal.ITIMER_REAL,0)

    def stop_owned(self):
        # Drain backend first, including when a setup or concurrent check fails.
        self.stop_backend()
        if self.gateway is not None and self.gateway.poll() is None:
            self.gateway.send_signal(signal.SIGQUIT)
            try:self.gateway.wait(35)
            except subprocess.TimeoutExpired:self.gateway.terminate();self.gateway.wait(10)
            g.require(self.gateway.returncode==0,'gateway stop failed')

    def execute(self):
        OUT.mkdir(mode=0o700)
        pkg.save(OUT/'begin.json',dict(monotonic=self.started,unix_seconds=time.time()))
        result=dict(status='FAILED',performance_acceptance=False,long_test_started=False)
        def interrupted(*_):raise RuntimeError('bounded TLS model driver interrupted')
        previous={s:signal.signal(s,interrupted) for s in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT)}
        try:
            a.require_idle(OUT/'gpu-before.json')
            self.launch();self.business();self.stop_owned()
            terminal=pkg.read(self.run_dir/'terminal.json')
            g.require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and
                terminal['guard']['status']=='COMPLETED' and terminal['worker']['attempts']==1 and
                terminal['worker']['restarts']==0,'managed cleanup incomplete')
            time.sleep(10)
            g.require(self.gateway.poll()==0 and self.process.poll()==0 and
                len(list(self.run_dir.glob('attempt-*.json')))==1,'unexpected restart')
            for path in (OUT/'gateway-private.log',self.run_dir/'backend.log'):
                raw=path.read_bytes()
                for c in self.keys['clients']:g.require(c['api_key'].encode() not in raw,'key leaked to logs')
            result.update(status='PASS_TLS_MODEL_TOOL_CANCEL',starts=1,
                          cancelled_requests=1,**EXPECTED)
        except BaseException as error:result['error']=repr(error);raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0);self.cleaning=True
            try:
                self.stop_owned();a.require_idle(OUT/'gpu-final.json')
                g.require(a.lock_available(a.guard.common_lock()),'GPU lease retained')
                g.require(time.monotonic()-self.started<LIMITS['seconds'] and
                    (self.gpu_finished is None or self.gpu_finished-self.gpu_started<GPU_WINDOW),'time budget')
                result['cleanup_confirmed']=True
            except BaseException as error:
                result.update(status='FAILED',cleanup_error=repr(error));raise
            finally:
                for s,h in previous.items():signal.signal(s,h)
                for stream in (self.log,self.gateway_log):
                    if stream:stream.close()
                result.update(counts=self.counts,elapsed_seconds=time.monotonic()-self.started,
                    gpu_window_elapsed=None if self.gpu_finished is None else self.gpu_finished-self.gpu_started,
                    supervisor_http_upper_bound=SUPERVISOR_BOUND)
                pkg.save(OUT/'result.json',result)
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','check','run'))
    args=parser.parse_args();configure()
    value=prepare() if args.action=='prepare' else check()
    if args.action=='run':
        with sup.processes.child_subreaper():value=Run(value).execute()
    print(json.dumps(value))
