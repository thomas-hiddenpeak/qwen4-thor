"""Finish cancellation/quality after a gateway-vs-runner validator mistake."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_operations_listener import cpu,stage as s,native_fix as previous

FAILED=previous.OUTPUT
ORIGINAL_PACKAGE=previous.PACKAGE
PACKAGE,OUTPUT=s.BASE/'package-business-fix',s.BASE/'execution-04'
SOURCES=('tools/serve_operations_listener/business_fix.py','tools/serve_operations_listener/test_business_fix.py')
PLANNED=dict(s.PLANNED,native_processes=0,negative_probes=2,reused_negative_probes=4,
             prior_models=1,prior_generation_attempts=1)


def gateway_error(value,receipt,status,code):
    cpu.require(receipt['status']==status and value==dict(error=dict(message='Gateway rejected request',
        type='gateway_error',param=None,code=code)),'gateway error contract')
    headers={k.lower():v for k,v in receipt['headers'].items()}
    cpu.require(headers.get('content-type','').startswith('application/json') and
        headers.get('cache-control')=='no-store' and headers.get('x-q4t-gateway-request-id'),
        'gateway error framing')
    if status==401:cpu.require(headers.get('www-authenticate')=='Bearer realm="q4t"','gateway auth challenge')


def prior_gate():
    value=cpu.read(FAILED/'business/result.json')
    cpu.require(value['status']=='FAILED' and value['counts']==dict(starts=1,generation_attempts=1,controls=18) and
        'ValidationError' in value['error'] and 'gateway_unauthorized' in value['error'] and
        cpu.read(FAILED/'lease/exit.json')['status']=='COMMAND_FAILED','exact retained validator failure required')
    cpu.require('33 tests, 33 passed, 0 failed, 0 skipped' in
        (FAILED/'business/native/stdout.log').read_text(),'native evidence missing')
    s.saturation_evidence(cpu.read(FAILED/'business/saturated-metrics.json'))
    diagnostic=cpu.read(FAILED/'business/saturated-diagnostic.json')
    cpu.require(diagnostic['state']=='HEALTHY' and diagnostic['identity']['state']=='verified','diagnostic reuse invalid')
    cpu.require(cpu.read(FAILED/'business/saturated-supervisor.json')['ready'],'supervisor reuse invalid')
    for name,status in (('public-overflow',503),('anonymous-discovery',401),('identity-local-admin',403),
                        ('saturated-models',200),('saturated-capabilities',200)):
        cpu.require(cpu.read(FAILED/'business'/(name+'-http.json'))['status']==status,'reused control status')
    gateway_error(cpu.read(FAILED/'business/bad-key-wire.bin'),cpu.read(FAILED/'business/bad-key-http.json'),
                  401,'gateway_unauthorized')
    for name in ('native','scoped-model','nginx'):
        owner=cpu.read(FAILED/'business'/name/'identity.json')
        cpu.require(not Path(f"/proc/{owner['pid']}").exists(),'prior owned process still exists')
    cpu.require(not (FAILED/'release').exists(),'failed run must not install')


def prepare():
    cpu.require(not PACKAGE.exists() and not OUTPUT.exists(),'no automatic retry')
    cpu.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit driver repair first')
    prior_gate();s.h.verify_seal(ORIGINAL_PACKAGE)
    sources=cpu.read(ORIGINAL_PACKAGE/'sources.json')
    cpu.require(all(cpu.sha(ROOT/n)==d for n,d in sources.items()),'production/fixture source changed')
    tested=cpu.read(s.BASE/'driver-business-fix/result.json');cpu.require(tested['passed'],'repair CPU gate')
    for name,digest in tested['sources'].items():cpu.require(cpu.sha(ROOT/name)==digest,'repair source drift')
    s.h.seal(FAILED);PACKAGE.mkdir()
    for path in sorted(ORIGINAL_PACKAGE.rglob('*')):
        rel=path.relative_to(ORIGINAL_PACKAGE)
        if path.is_file() and str(rel) not in ('sources.json','plan.json','sealed-files.json'):
            target=PACKAGE/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
    sources.update({n:cpu.sha(ROOT/n) for n in SOURCES});cpu.save(PACKAGE/'sources.json',sources)
    plan=cpu.read(ORIGINAL_PACKAGE/'plan.json')
    plan.update(candidate=str(PACKAGE/'candidate/q4t'),binary=str(PACKAGE/'candidate/q4t'),planned=PLANNED,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        business_repair=dict(original_failure=s.h.identity(FAILED/'sealed-files.json'),
            native_and_observation_reused=True,prior_models=1,prior_generation_attempts=1,
            remaining_successes=5,remaining_explicit_cancel=1,new_models=1,new_attempts=6,
            cumulative_models=2,cumulative_generation_attempts=7,production_changed=False))
    for path in (FAILED/'sealed-files.json',s.BASE/'driver-business-fix/result.json'):
        plan['local'][str(path)]=cpu.sha(path)
    cpu.save(PACKAGE/'plan.json',plan);s.h.seal(PACKAGE)
    return dict(prepared=True,limits=plan['limits'],planned=PLANNED,production_unchanged=True)


class Run(s.Run):
    def request(self,label,body=None,expected=200,**kwargs):
        if label!='gateway-local-admin':return super().request(label,body,expected,**kwargs)
        connection,response=self.begin(label,body,**kwargs)
        try:
            value=json.loads(self.record(label,response,response.read(262145),expected))
            gateway_error(value,cpu.read(s.s.OUT/(label+'-http.json')),404,'gateway_route_not_found')
            return value
        finally:connection.close()

    def saturated(self):
        body=s.s.a.ordinary(True);body['messages'][0]['content']='逐行列出从1到1000的数字，不要省略。'
        body['max_tokens']=512
        connection,response=self.begin('cancel-active',body);prefix=response.readline()
        cpu.require(response.status==200 and prefix.startswith(b'data: '),'active stream not started')
        for _ in range(PLANNED['public_slow_headers']):
            sock=socket.create_connection(('127.0.0.1',s.PORT),1);self.held.append(sock)
            sock.sendall(b'GET /metrics HTTP/1.1\r\nHost:')
        until=time.monotonic()+3
        while True:
            metrics,_=self.get_metrics('saturation-'+str(self.counts['controls']))
            if metrics['q4t_public_connections']==136:break
            cpu.require(time.monotonic()<until,'saturation unavailable');time.sleep(.01)
        s.saturation_evidence(metrics);cpu.save(s.s.OUT/'saturated-metrics.json',metrics)
        self.request('gateway-local-admin',path='/metrics',method='GET',expected=404);self.negative_probes+=1
        cancel=dict(request_id=body['request_id'],cancel_token=body['cancel_token'])
        self.request('cross-client-cancel',cancel,path='/v1/requests/cancel',client=1,expected=404);self.negative_probes+=1
        self.request('operations-cancel',cancel,path='/v1/requests/cancel',expected=202)
        raw=self.record('cancel-active',response,prefix+response.read(8*1024**2+1),200)
        cpu.save(s.s.OUT/'cancel-terminal.json',s.s.cancelled_wire(raw,body['request_id']))
        self.cancelled+=1;connection.close()
        for sock in self.held:sock.close()
        self.held.clear();self.idle('cancel-recovery')

    def execute(self):
        result=dict(status='FAILED',limits=s.LIMITS,counts=self.counts)
        try:
            prior_gate();s.s.a.require_idle(s.s.OUT/'initial-gpu.json')
            cpu.save(s.s.OUT/'reused-evidence.json',dict(native=s.h.identity(FAILED/'business/native/stdout.log'),
                diagnostic=s.h.identity(FAILED/'business/saturated-diagnostic.json'),
                supervisor=s.h.identity(FAILED/'business/saturated-supervisor.json'),
                original_failure=s.h.identity(FAILED/'business/result.json'),native_processes_started=0))
            self.launch();self.saturated()
            value=self.complete('ordinary-recovery',s.s.a.ordinary(True))
            cpu.require(value.finish_reason=='stop' and value.message['content'].strip()=='710003','recovery quality')
            for client,name in enumerate(('alpha','beta')):
                self.current_client=client;self.tools('project-'+name)
            metrics=self.idle('final-idle');cpu.save(s.s.OUT/'final-metrics.json',metrics)
            cpu.require(self.successes==metrics['q4t_requests_success_total']==5 and self.cancelled==1 and
                metrics['q4t_requests_aborted_total']==1 and self.negative_probes==2 and
                metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,
                'request/grammar accounting')
            self.stop(True);self.children.reap(s.s.OUT);s.s.a.require_idle(s.s.OUT/'final-gpu.json')
            result.update(status='PASS_BOUNDED_OPERATIONS_LISTENER',successes=self.successes,cancelled=1,
                negative_probes=2,reused_negative_probes=4,real_tools=2,native_reused=True)
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED',error=repr(error));raise
        finally:
            for sock in self.held:sock.close()
            for connection in self.sockets:connection.close()
            try:self.children.cleanup()
            except BaseException as error:
                result.update(status='FAILED',cleanup_error=repr(error));raise
            finally:
                result['seconds']=time.monotonic()-self.started;cpu.save(s.s.OUT/'result.json',result)


def bind():
    prior_gate();s.h.verify_seal(FAILED)
    s.PACKAGE,s.OUTPUT,s.PLANNED,s.Run=PACKAGE,OUTPUT,PLANNED,Run
    s.check()


def execute():bind();s.execute()

def finalize():bind();s.finalize()


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','execute','finalize'))
    print(globals()[p.parse_args().action]() or {'passed':True})
