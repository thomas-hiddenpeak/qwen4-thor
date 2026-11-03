"""Retain incomplete SSE capture; repeat that case, finish unexecuted cases only."""
import argparse
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_device_memory import stage as original, verify as v
from chat_client import read_sse, RemoteError

h,a=original.h,original.a
ORIGINAL_OUTPUT=original.OUTPUT
BASE=ROOT/'.q4t-work/serve-device-memory-correction-20261103'
PACKAGE,OUTPUT=BASE/'package',BASE/'execution'
ORIGINAL_CHECK,ORIGINAL_RELEASE=original.check,original.release
SPENT=dict(generations=8,negatives=1,starts=3)
LIMITS=dict(generations=9,negatives=7,starts=7,observations=128,seconds=1800)
CUMULATIVE=dict(generations=17,negatives=8,starts=10)


def capture_response(response, sink, *, maximum=8*1024*1024):
    # Read through HTTP EOF before applying the client's fail-fast SSE parser.
    # Sink every byte, including a tail after an error envelope, without replay.
    raw=bytearray()
    while True:
        part=response.read1(min(65536,maximum-len(raw)+1))
        if not part:return bytes(raw)
        sink(part);raw.extend(part)
        h.require(len(raw)<=maximum,'fault wire exceeds capture limit')


def audit_original():
    h.verify_seal(original.PACKAGE);h.verify_seal(ORIGINAL_OUTPUT)
    result=h.read(ORIGINAL_OUTPUT/'business/result.json')
    h.require(result['status']=='FAILED' and result['counts']==SPENT and
        result['error']=="RuntimeError('error stream needs one final DONE')",'unexpected first failure')
    guard=h.read(ORIGINAL_OUTPUT/'lease/exit.json')
    h.require(guard['status']=='COMMAND_FAILED' and guard['child_returncode']==1 and
        'ENV_ABORT' not in (ORIGINAL_OUTPUT/'lease/events.jsonl').read_text(),'original environment invalid')
    native=h.read(ORIGINAL_OUTPUT/'native/result.json')
    h.require(native['passed'] and native['required_tests']==25,'native proof absent')
    wires=[]
    for name,labels,metrics,success in (
        ('model',('ordinary-recovery','ordinary-sse','strict-json','project-read','project-answer'),
         'production-complete',5),('optional-oom',('fallback','recovered'),'recovered',2)):
        folder=ORIGINAL_OUTPUT/'business'/name
        h.require(h.read(folder/'result.json')['passed'],'completed scenario missing')
        rows=h.read(folder/(metrics+'-resources.json'))['metrics']
        h.require(rows['q4t_requests_total']==rows['q4t_requests_success_total']==success and
            rows['q4t_requests_error_total']==rows['q4t_requests_aborted_total']==0 and
            all(rows[k]==0 for k in v.base.EMPTY) and rows['q4t_seq_slots_free']==rows['q4t_seq_slots_total'],
            'original accounting/cleanup')
        for label in labels:
            request=h.read(folder/(label+'-request.json'));value=h.read(folder/(label+'-result.json'))
            wire=folder/(label+'-wire.bin');events=v.base.wire_values(wire.read_bytes(),request['stream'])
            h.require(events and all('error' not in item for item in events),'original success wire invalid')
            schema=v.base.Draft202012Validator(h.read(ROOT/'docs/serve-transport-v1.schema.json'))
            for item in events:schema.validate(item)
            h.require(value['finish_reason']==('tool_calls' if label=='project-read' else 'stop'),
                      'original incomplete turn')
            wires.append(dict(file=h.identity(wire),envelopes=len(events),success=True))
    for name in ('model','optional-oom','session-oom'):
        folder=ORIGINAL_OUTPUT/'business'/name
        h.require(h.read(folder/'gpu-after.json')['pids']==[] and
            h.read(folder/'scope/cleanup.json')['unit_removed'],'owned original scope not cleaned')
    partial=ORIGINAL_OUTPUT/'business/session-oom/fault-wire.bin'
    error=h.read(ORIGINAL_OUTPUT/'business/session-oom/fault-error.json')
    h.require(error['code']=='device_memory_exhausted' and error['status']==200 and
        b'data: [DONE]' not in partial.read_bytes(),'original capture boundary differs')
    return dict(passed=True,counts=SPENT,wires=wires,original_result=h.identity(ORIGINAL_OUTPUT/'business/result.json'),
        incomplete_error_wire=h.identity(partial),native=h.identity(ORIGINAL_OUTPUT/'native/result.json'),
        reused_successful_model_turns=7,repeat_only_failed_capture=True,runtime_changed=False,
        original_failed_plan_not_reclassified=True)


class Run(v.Run):
    def budget(self,kind=None):
        h.require(time.time()<self.deadline,'original wall deadline')
        if kind:
            h.require(self.counts[kind]<LIMITS[kind],'targeted correction attempts: '+kind)
            self.counts[kind]+=1
            h.save(self.out/f'attempt-{kind}-{self.counts[kind]:02d}.json',
                dict(kind=kind,counts=self.counts.copy(),cumulative={k:SPENT[k]+self.counts[k] for k in SPENT},time=time.time()))

    def http(self,method,path,body=None,*,timeout=5):
        h.require(self.controls<LIMITS['observations'],'correction controls')
        return super().http(method,path,body,timeout=timeout)

    def failure(self,body,code):
        self.schemas['chat'].validate(body)
        self.budget('negatives');self.budget('generations')
        h.save(self.folder/'fault-request.json',body)
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=180)
        path=self.folder/'fault-wire.bin'
        receipt=dict(eof=False)
        try:
            conn.request('POST','/v1/chat/completions',json.dumps(body),{'Content-Type':'application/json'})
            response=conn.getresponse()
            receipt.update(status=response.status,headers=dict(response.getheaders()))
            with path.open('xb',buffering=0) as output:
                def sink(part):output.write(part);os.fsync(output.fileno())
                raw=capture_response(response,sink)
            receipt['eof']=True
        except BaseException as error:
            receipt['read_error']=repr(error)
            raise
        finally:
            conn.close();h.save(self.folder/'fault-http.json',receipt)
        h.require(receipt['status']==(200 if body['stream'] else 503),'raw error HTTP status')
        if body['stream']:
            h.require(receipt['headers'].get('Content-Type','').startswith('text/event-stream'),'error SSE MIME')
        values=v.error_terminal(raw,body['stream'],code)
        for value in values:self.transport.validate(value)
        error=None
        if body['stream']:
            try:read_sse(io.BytesIO(raw))
            except RemoteError as caught:error=caught
        else:error=RemoteError(receipt['status'],raw)
        h.require(error is not None and error.code==code and not error.automatic_retry_safe,
                  'same-wire client did not reject unsafe continuation')
        h.save(self.folder/'fault-client.json',dict(code=error.code,status=error.status,
            complete_assistant_turn=False,automatic_retry=False,offline_parse_of_same_actual_wire=True))
        self.wires.append(dict(file=h.identity(path),envelopes=len(values),success=False))
        return values


def prepare():
    h.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit correction before freeze')
    plan=ORIGINAL_CHECK();audit=audit_original()
    h.require(time.time()<plan['started']+1800-400,'original time reserve insufficient')
    h.require(h.read(BASE/'cpu/result.json')['passed'],'capture CPU failure')
    PACKAGE.mkdir();shutil.copytree(BASE/'cpu',PACKAGE/'cpu')
    h.save(PACKAGE/'reused-evidence.json',audit)
    paths=['tools/serve_device_memory/correction.py','tools/serve_device_memory/test_correction.py',
           'docs/SERVE_DEVICE_MEMORY_CORRECTION_2026-11-03.md']
    h.save(PACKAGE/'sources.json',{p:h.sha(ROOT/p) for p in paths})
    h.save(PACKAGE/'plan.json',dict(source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        original_package=h.identity(original.PACKAGE/'sealed-files.json'),
        original_execution=h.identity(ORIGINAL_OUTPUT/'sealed-files.json'),started=plan['started'],
        limits=LIMITS,spent=SPENT,cumulative=CUMULATIVE,
        original_limits_unchanged=original.LIMITS,original_plan_remains_failed=True,
        added_attempts=dict(generations=1,negatives=1,starts=1)))
    h.seal(PACKAGE)


def check():
    h.verify_seal(PACKAGE);saved=h.read(PACKAGE/'plan.json')
    h.check_identity(saved['original_package']);h.check_identity(saved['original_execution'])
    a.verify_hashes(ROOT,h.read(PACKAGE/'sources.json'))
    h.require(saved['limits']==LIMITS and saved['spent']==SPENT and saved['cumulative']==CUMULATIVE,'correction scope changed')
    plan=ORIGINAL_CHECK()
    h.require(audit_original()==h.read(PACKAGE/'reused-evidence.json'),'reused proof changed')
    plan.update(source_commit=saved['source_commit'],started=saved['started'],original_started=saved['started'])
    return plan


def verifier(plan,installed=False):
    if installed:
        from tools.serve_device_memory.installed_health import run
        run(plan,OUTPUT);return
    audit=audit_original();worker=Run(plan,OUTPUT/'business')
    for name in ('model','optional-oom'):
        shutil.copytree(ORIGINAL_OUTPUT/'business'/name,worker.out/name)
    result=dict(status='FAILED')
    try:
        for row in v.SCENARIOS[2:]:worker.scenario(*row)
        h.require(worker.counts=={k:LIMITS[k] for k in SPENT},'correction attempt count')
        h.save(worker.out/'wire-schema-result.json',dict(passed=True,records=audit['wires']+worker.wires,
            original_incomplete_error_wire=audit['incomplete_error_wire']))
        result.update(status='PASS_BOUNDED_DEVICE_MEMORY',reused=audit,physical_oom_proved=False,
            full_allocation_coverage=False,performance_acceptance=False,automatic_retry=False,
            original_failed_plan_retained=True,one_explicit_targeted_capture_repeat=True)
    except BaseException as error:
        result.update(status='ENV_ABORT' if isinstance(error,(v.observe.ObservationAbort,a.EnvironmentAbort)) else 'FAILED',error=repr(error))
        if isinstance(error,v.observe.ObservationAbort):raise a.EnvironmentAbort(str(error)) from error
        raise
    finally:
        result.update(new_counts=worker.counts,counts={k:SPENT[k]+worker.counts[k] for k in SPENT},
            new_control_requests=worker.controls,control_requests=25+worker.controls,
            cumulative_elapsed_seconds=time.time()-plan['started'])
        h.save(worker.out/'result.json',result)


def bind():
    original.OUTPUT=OUTPUT
    a.BASE,a.PACKAGE,a.OUTPUT=BASE,original.PACKAGE,OUTPUT
    a.MARKER=ROOT/'build/serve-device-memory-correction-recovery.json'
    a.check,a.verifier,a.release=check,verifier,ORIGINAL_RELEASE


def execute():
    h.require_guard(OUTPUT);plan=check()
    h.save(OUTPUT/'begin.json',dict(started=plan['started'],limits=LIMITS,original_wall_deadline_preserved=True))
    remaining=plan['started']+1800-90-time.time();h.require(remaining>0,'deadline')
    def expired(signum,frame):raise RuntimeError('original deadline; cleanup reserve only')
    signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,remaining)
    try:bind();a.execute()
    finally:signal.setitimer(signal.ITIMER_REAL,0)


def finalize():
    plan=check();h.require(time.time()-plan['started']<=1800,'original deadline')
    bind();a.finalize();h.seal(OUTPUT)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','launch','execute','finalize'))
    args=parser.parse_args()
    if args.action=='launch':
        check();OUTPUT.mkdir(mode=0o700)
        raise SystemExit(subprocess.call(['/usr/bin/python3','-B',str(ROOT/'tools/evalscope/gpu_exclusive.py'),
            '--output',str(OUTPUT/'lease'),'--','/usr/bin/python3','-B',str(Path(__file__)), 'execute'],cwd=ROOT))
    globals()[args.action]()


if __name__=='__main__':main()
