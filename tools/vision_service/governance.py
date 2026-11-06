"""One-lease visual client quota, TLS and fair admission acceptance.

Only ephemeral loopback listeners. Uses real keys/certificate and the shared
stage custody, source freeze, sampler and final GPU barriers. No retries.
"""
import argparse
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.vision_service import stage as s
from tools.serve_gateway import gateway as g, client_policy as cp
from tools.serve_gateway.test_gateway import certificate, example, VENDOR
from tools.serve_gateway.transport_tests import TLSConnection
from tools.serve_gateway.acceptance import cancelled_wire

FOLDER=s.BASE/'phase-05'
TLS_PORT=18466


def policy():
    common=dict(max_control_inflight=2,max_prompt_tokens=8192,
                max_body_bytes=16*1024**2)
    return dict(version=2,fair_queue=True,gateway_token=secrets.token_hex(32),clients=[
        dict(id='alpha',**common,max_inflight=5,max_output_tokens=2048,requests_per_minute=6,tokens_per_minute=12000),
        dict(id='beta',**common,max_inflight=3,max_output_tokens=64,requests_per_minute=20,tokens_per_minute=1000)])


def prepare():
    previous=s.BASE/'phase-04'
    result=json.loads((previous/'execution/result.json').read_text())
    # Continue only the scoped image successes. Temporal quality remains
    # failed; this phase must never turn the previous whole stage into PASS.
    accepted=('natural-json','natural-sse','natural-spatial','document-png',
              'document-jpeg','chart','multiple-images','visual-tool-loop','cancelled-visual','recovery')
    observed={r['label']:r['passed'] for r in result['observations']}
    s.require(all(observed.get(n) for n in accepted) and not result['cleanup_errors'] and
              not result['monitor_failures'], 'scoped image/custody prerequisites')
    s.require(json.loads((previous/'lease/exit.json').read_text())['status']=='COMMAND_FAILED',
              'prior failed phase must remain failed')
    for label in ('small','rectangular'):
        s.require(json.loads((previous/('execution/hf-'+label+'/waitable.json')).read_text())['returncode']==0,
                  'official numerical reference prerequisite')
    FOLDER.mkdir(mode=0o700)
    private=FOLDER/'private';private.mkdir(mode=0o700)
    front=example(TLS_PORT,s.PORT)
    front.update(per_client_inflight=6,global_inflight=8,per_client_rps=100,burst=128)
    backend=policy();cp.validate(backend)
    s.require(sum(c['max_inflight'] for c in backend['clients'])<=8 and
              len(backend['clients'])<=6,'fairness startup capacity combination')
    s.save(private/'clients.json',backend);s.save(private/'frontend.json',front)
    cert,key=certificate(private)
    receipt=g.prepare(private/'frontend.json',json.loads(VENDOR.read_text())['binary'],
                      cert,key,private/'gateway',private/'clients.json')
    plan=json.loads((previous/'plan.json').read_text())
    plan.update(limits=dict(s.LIMITS,native=0),tower_repair=False,cpu_video=True,
                policy=str(private/'clients.json'),frontend=str(private/'frontend.json'),
                gateway=receipt,tls_port=TLS_PORT,
                source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                matrix=dict(governance_attempts=13,completed=6,cancelled=2,refused=5,
                            temporal_diagnostics=4,prior_temporal_quality='FAILED'))
    # Refresh tracked code identities after reviewed tool/document changes;
    # candidate and canonical model inputs must still match the accepted run.
    old=plan['files']
    for name in (str(s.BASE/'engine/q4t'),str(s.MODEL/'config.json')):
        s.require(s.sha(name)==old[name],'accepted runtime changed')
    paths=[ROOT/n for n in subprocess.check_output(['git','ls-files'],cwd=ROOT,text=True).splitlines()
           if n.startswith(('src/','include/','cmake/','tools/','docs/serve-')) or n=='CMakeLists.txt']
    paths += [Path(n) for n in old if Path(n).is_file()]
    paths += [p for p in private.rglob('*') if p.is_file() and 'runtime' not in p.parts]
    paths += [previous/'execution/result.json',previous/'lease/exit.json']
    plan['files']={str(p):s.sha(p) for p in paths}
    s.save(FOLDER/'plan.json',plan)
    return plan['matrix']


def rate(raw,client,field):
    key='q4t_client_rate_'+field+'{client="'+client+'"}'
    rows=[float(line.split()[-1]) for line in raw.splitlines() if line.startswith(key+' ')]
    s.require(len(rows)==1,'missing/duplicate client metric: '+key)
    return rows[0]


class Run(s.Run):
    success_status = "PASS_BOUNDED_MEDIA_GOVERNANCE_DIAGNOSTICS_RECORDED"
    def __init__(self):
        super().__init__(FOLDER)
        self.front=json.loads(Path(self.plan['frontend']).read_text())
        self.context=ssl.create_default_context(cafile=Path(self.plan['gateway']['package'])/'certificate.pem')
        self.pending=[]
        self.nginx=None

    def begin(self,label,path,body=None,client_index=0):
        self.budget('generations' if path=='/v1/chat/completions' else 'controls')
        remaining=self.plan['limits']['seconds']-(time.monotonic()-self.started)-45
        conn=TLSConnection(self.front['server_name'],TLS_PORT,context=self.context,timeout=min(130,remaining))
        fields={'Content-Type':'application/json'}
        if client_index is not None:
            fields['Authorization']='Bearer '+self.front['clients'][client_index]['api_key']
        s.save(self.output/(label+'-request.json'),dict(path=path,body=body,client_index=client_index))
        try:
            conn.request('POST' if body is not None else 'GET',path,
                json.dumps(body,ensure_ascii=False).encode() if body is not None else None,fields)
            response=conn.getresponse()
            s.save(self.output/(label+'-transport.json'),dict(status=response.status,headers=dict(response.getheaders())))
            self.pending.append(conn)
            return conn,response
        except BaseException:
            conn.close();raise

    def finish(self,label,conn,response,expected=200):
        try:
            raw=response.read(32*1024**2+1)
            (self.output/(label+'-wire.bin')).write_bytes(raw)
            s.require(len(raw)<=32*1024**2 and response.status==expected,'HTTP status/bound: '+label)
            return raw
        finally:
            conn.close()
            if conn in self.pending:self.pending.remove(conn)

    def http(self,label,path,body=None,expected=200,client_index=0):
        if path in ('/metrics','/v1/config'):
            return super().http(label,path,body,expected)
        conn,response=self.begin(label,path,body,client_index)
        return self.finish(label,conn,response,expected)

    def complete(self,label,body,client_index=0):
        raw=self.http(label,'/v1/chat/completions',body,client_index=client_index)
        value=(s.client.read_sse if body.get('stream') else s.client.read_json)(io.BytesIO(raw))
        s.require(value.id==body['request_id'] and value.model==body['model'],'completion identity')
        s.save(self.output/(label+'-completion.json'),asdict(value))
        return value

    def metric(self,label):
        return self.http(label,'/metrics').decode()

    def queued(self,label,count):
        end=time.monotonic()+8
        while True:
            raw=self.metric(label+'-'+str(self.counts['controls']))
            if s.parse_metrics(raw)['q4t_queued_requests']==count:return raw
            s.require(time.monotonic()<end,'queue did not reach expected size')
            time.sleep(.1)

    def preflight(self,label,body,client_index=0):
        value=json.loads(self.http(label,'/v1/chat/preflight',dict(request=body),client_index=client_index))
        s.Draft202012Validator(json.loads((ROOT/'docs/serve-preflight-v1.schema.json').read_text())['$defs']['response']).validate(value)
        s.require(value['client_limits']['rate_quota_reserved'] is False,'preflight charged rate quota')
        return value

    def visual(self,stream=True):
        return s.request('What is the Passed count for Vision in the table? Reply with the integer only.',
                         [s.image_part(s.BASE/'fixtures/document.png')],stream=stream,max_tokens=32)

    def launch(self):
        self.budget('starts')
        argv=[self.plan['binary'],'serve','--model-dir',self.plan['model'],
              '--host','127.0.0.1','--port',str(s.PORT),'--allow-media','--no-mtp',
              '--max-seq','1','--max-prefill','8192','--max-len','16384',
              '--max-inflight-requests','8','--max-queued-requests','6',
              '--client-policy-file',self.plan['policy']]
        env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k not in ('LD_PRELOAD','LD_LIBRARY_PATH')}
        self.model=self.children.launch(self.output/'model',argv,env)
        end=time.monotonic()+180
        while 'serving on port' not in (self.output/'model/stderr.log').read_text():
            s.require(self.children.running(self.model) and time.monotonic()<end,'startup failed')
            time.sleep(.1)
        config=json.loads(self.http('config','/v1/config'))
        cp.observed_matches(config['client_policy'],cp.binding(Path(self.plan['policy']).read_bytes()))
        s.require(config['client_governance']['fair_queue'] and config['runtime']['media_enabled'],
                  'media/governance profile')

    def cancel_request(self,label,body,client_index=0,expected=202):
        return self.http(label,'/v1/requests/cancel',
            {k:body[k] for k in ('request_id','cancel_token')},expected,client_index)

    def tests(self):
        # Trust boundary: neither raw loopback bypass nor missing TLS key.
        bad=json.loads(super().http('direct-no-token','/v1/chat/completions',self.visual(),401))
        s.require(bad['error']['code']=='client_authentication_failed','backend bypass')
        self.http('tls-no-key','/v1/chat/completions',self.visual(),401,client_index=None)
        large=s.request('What color is the mug? One word only.',
            [s.image_part(s.BASE/'fixtures/natural-scene.png')],max_tokens=32)
        before=self.metric('quota-before')
        pre=self.preflight('expanded-preflight',large,1)
        full=pre['prompt_tokens']+pre['output']['available_tokens']
        placeholders=pre['prompt_tokens']-pre['media']['visual_tokens']+pre['media']['items']
        s.require(placeholders+32<1000<full,'test does not isolate visual expansion')
        denial=json.loads(self.http('expanded-tpm-denied','/v1/chat/completions',large,429,1))
        s.require(denial['error']['code']=='client_tpm_exhausted','expanded TPM not enforced')
        invalid=s.request('Describe.',[dict(type='image_url',image_url='data:image/png;base64,AAAA')])
        self.http('invalid-media','/v1/chat/completions',invalid,400)
        after=self.metric('quota-after')
        for who in ('alpha','beta'):
            for field in ('requests','reserved_tokens','settled_tokens_total'):
                s.require(rate(before,who,field)==rate(after,who,field),'preflight/rejection charged quota')
        self.observe('expanded-tpm',True,expanded_cost=full,placeholder_cost=placeholders+32)
        for label,stream in (('tls-json',False),('tls-sse',True)):
            body=self.visual(stream);value=self.complete(label,body)
            s.require(value.finish_reason=='stop' and value.message['content'].strip()=='12','TLS visual quality')
        self.idle('seeds-idle')
        active=self.visual();active['messages'][0]['content'][-1]['text']='List every visible detail, then count from 1 to 1000 without omitting any number.'
        active['max_tokens']=2048
        active_prompt=self.preflight('active-preflight',active)['prompt_tokens']
        conn,resp=self.begin('active','/v1/chat/completions',active)
        # Registered SSE plus actual slot admission, not just socket presence.
        end=time.monotonic()+8
        while f'id={active["request_id"]} event=admitted' not in (self.output/'model/stderr.log').read_text():
            s.require(time.monotonic()<end,'active request not admitted');time.sleep(.1)
        waiting=[]
        for label,who in (('a1',0),('a2',0),('a3',0),('b1',1)):
            body=self.visual();pre=self.preflight(label+'-preflight',body,who)
            c,r=self.begin(label,'/v1/chat/completions',body,who)
            waiting.append((label,body,c,r,pre['prompt_tokens']+pre['output']['available_tokens']))
            self.queued('queued-'+label,len(waiting))
        self.cancel_request('cross-client-cancel',active,1,404)
        before=self.metric('before-cancel')
        label,body,c,r,cost=waiting[2]
        self.cancel_request('cancel-a3',body)
        cancelled_wire(self.finish(label,c,r),body['request_id'])
        after=self.queued('after-cancel',3)
        s.require(rate(before,'alpha','reserved_tokens')-rate(after,'alpha','reserved_tokens')==cost,
                  'queued media reservation not refunded')
        s.require(rate(before,'alpha','settled_tokens_total')==rate(after,'alpha','settled_tokens_total'),
                  'queued media charged executed tokens')
        denied=json.loads(self.http('rpm-denied','/v1/chat/completions',self.visual(),429))
        s.require(denied['error']['code']=='client_rpm_exhausted','RPM not enforced')
        self.cancel_request('cancel-active',active)
        active_wire=self.finish('active',conn,resp)
        cancelled_wire(active_wire,active['request_id'])
        s.require(b'cancelled during media processing' in active_wire,'active cancellation phase changed')
        for i in (3,0,1):
            label,body,c,r,_=waiting[i]
            value=s.client.read_sse(io.BytesIO(self.finish(label,c,r)))
            s.require(value.id==body['request_id'] and value.finish_reason=='stop' and
                value.message['content'].strip()=='12','queued visual response')
            s.save(self.output/(label+'-completion.json'),asdict(value))
        self.idle('fair-idle')
        order=re.findall(r'id=(\S+) event=admitted', (self.output/'model/stderr.log').read_text())
        expected=[active['request_id'],*(waiting[i][1]['request_id'] for i in (3,0,1))]
        s.require(order[-4:]==expected and waiting[2][1]['request_id'] not in order,'client fair order')
        value=self.complete('beta-recovery',self.visual(),1)
        s.require(value.message['content'].strip()=='12' and value.finish_reason=='stop','isolated client recovery')
        self.idle('final-idle')
        raw=self.metric('final-ledger');values=s.parse_metrics(raw)
        s.require(all(rate(raw,c,'reserved_tokens')==0 for c in ('alpha','beta')),'reservation retained')
        # Admission bills the full logical prompt even if media preparation
        # is cancelled. Generation-path metrics do not include that request.
        s.require(sum(rate(raw,c,'settled_tokens_total') for c in ('alpha','beta'))==
            values['q4t_prompt_tokens_total']+values['q4t_generation_tokens_total']+active_prompt,
            'admitted logical usage mismatch')
        self.observe('media-client-governance',True,expected_order=expected,observed_order=order,
                     queued_refund=cost,tls=True,long_stability=False)

    def diagnose_temporal(self):
        # Client alpha exhausted its minute quota above. Wait for the real
        # fixed-window boundary once; no extra model, no retries of answers.
        raw=self.metric('diagnostic-window')
        remaining=max(rate(raw,c,'window_remaining_ms') for c in ('alpha','beta'))
        if remaining>0:time.sleep(min(remaining/1000+.2,60))
        original=json.loads((s.BASE/'phase-04/requests.json').read_text())
        forward=next(r['request']['messages'][0]['content'][0] for r in original if r['label']=='motion-forward')
        probes=[('first-frame',[s.image_part(s.BASE/'fixtures/motion-00.png')],
                 'Is the red circle on the left or right side of this image? Reply with one word only.'),
                ('last-frame',[s.image_part(s.BASE/'fixtures/motion-07.png')],
                 'Is the red circle on the left or right side of this image? Reply with one word only.'),
                ('video-plain',[forward],
                 'Describe where the red circle starts and ends, and which direction it moves. Answer briefly in English.'),
                ('endpoint-images',[s.image_part(s.BASE/'fixtures/motion-00.png'),s.image_part(s.BASE/'fixtures/motion-07.png')],
                 'These are the first and last frames of a video in time order. Describe where the red circle starts and ends, and which direction it moves. Answer briefly in English.')]
        for label,parts,prompt in probes:
            body=s.request(prompt,parts,max_tokens=128)
            value=self.complete('diagnose-'+label,body)
            self.observe('diagnose-'+label,True,diagnostic_only=True,
                         content=value.message.get('content'),finish_reason=value.finish_reason)
        self.idle('diagnostic-idle')

    def business(self):
        folder=Path(self.plan['gateway']['package'])
        manifest=g.check(folder,self.plan['gateway']['manifest_sha256'])
        try:
            self.nginx=self.children.launch(self.output/'nginx',[manifest['nginx']['path'],
                '-e','stderr','-p',str(folder)+'/', '-c',str(folder/'nginx.conf')])
            end=time.monotonic()+10
            while True:
                s.require(self.children.running(self.nginx) and time.monotonic()<end,'nginx startup')
                try:
                    with socket.create_connection(('127.0.0.1',TLS_PORT),.1):break
                except ConnectionRefusedError:time.sleep(.1)
            self.tests()
            self.diagnose_temporal()
        finally:
            for conn in self.pending:conn.close()
            self.pending.clear()
            if self.nginx and self.children.running(self.nginx):self.children.signal(self.nginx,signal.SIGQUIT)
            if self.nginx:self.children.wait(self.nginx,self.output/'nginx',35)


if __name__=='__main__':
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','run'))
    p.add_argument('--phase',choices=('phase-05','phase-05b'),default='phase-05b')
    args=p.parse_args()
    FOLDER=s.BASE/args.phase
    print(json.dumps(prepare() if args.action=='prepare' else Run().run()))
