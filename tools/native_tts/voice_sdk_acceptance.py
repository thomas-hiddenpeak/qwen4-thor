"""Pinned Voice SDK through native TLS, with bounded owned entrypoint restart.

This drives real registration, synthesized Speech/Chat, independent ASR and
revocation. A PID-bound test admission peer replaces only Guardian dispatch;
managed recovery qualification remains a separate task.
"""
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import threading
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_tts import voice_service_acceptance as direct
from tools.native_tts import voice_sdk
from tools.serve_gateway import gateway
from tools.serve_recovery import journal, native_ingress
from tools.serve_recovery.test_gateway import config as gateway_fixture

read, save, sha, require = direct.read, direct.save, direct.sha, direct.require
LIMITS = dict(direct.LIMITS, posts=15, seconds=1200, ingress_starts=2)
KEYS = {'audio':'A'*40, 'other':'B'*40, 'tiny':'C'*40}  # Owned test keys only.


class Edge:
    """Same native entry/admission protocol as Files' real TLS driver."""
    def __init__(self, stage):
        self.stage=stage;self.process=self.peer=self.thread=None
        self.stop=threading.Event();self.error=None
        stage.budget('ingress_starts');number=stage.counts['ingress_starts']
        self.out=stage.out/f'entry-{number}';self.out.mkdir(mode=0o700)
        source=gateway_fixture(self.out)['policy']
        source.update(version=5,listen_port=stage.phase['edge_port'],
            upstream_port=stage.plan['port'],upstream_operations_port=stage.plan['operations_port'],
            body_bytes=16<<20,file_body_bytes=(10<<20)+65536,
            per_client_rps=100,burst=100,global_inflight=4,per_client_inflight=1,
            clients=[dict(id=name,api_key=key) for name,key in KEYS.items()],
            http=dict(path_prefix='/voice-test'))
        address='q4t-voice-sdk-'+str(uuid.uuid4())
        tls=stage.phase['tls_inputs']
        settings=dict(version=2,parent_pid=os.getpid(),
            parent_start_ticks=journal.process_start(os.getpid()),address=address,
            policy=gateway.policy(source),tls=dict(certificate=tls['certificate']['path'],
            key=tls['key']['path'],ca='',require_client_certificate=False),
            tls_lease='q4t-voice-sdk-tls-'+str(uuid.uuid4()))
        save(self.out/'ingress.json',settings)
        def decide(lane):
            stage.same_model()
            return dict(port=stage.plan['operations_port' if lane=='control' else 'port'],
                        token=direct.SECRET)
        self.peer=native_ingress.AdmissionSocket(address,KEYS,decide,
                    lambda:self.process.pid if self.process is not None else None)
        try:
            with (self.out/'entry.log').open('xb') as log:
                self.process=subprocess.Popen([stage.phase['ingress']['path'],'--config',
                    str(self.out/'ingress.json')],stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                    pass_fds=(stage.lease,))
            stage.history[self.process.pid]=direct.lifecycle.guard.processes()[self.process.pid]
            def serve():
                try:
                    while not self.stop.is_set():self.peer.step(.02)
                except BaseException as error:self.error=error
            self.thread=threading.Thread(target=serve);self.thread.start()
            context=ssl.create_default_context(cafile=tls['ca']['path']);end=time.monotonic()+5
            while time.monotonic()<end:
                conn=http.client.HTTPSConnection('127.0.0.1',stage.phase['edge_port'],context=context,timeout=1)
                try:
                    stage.budget('controls')
                    conn.request('GET','/voice-test/v1/models',headers={'Authorization':'Bearer '+KEYS['audio']})
                    response=conn.getresponse();raw=response.read(1<<20)
                    if response.status==200:
                        save(self.out/'ready.json',dict(status=200,bytes=len(raw),pid=self.process.pid));break
                except (OSError,http.client.HTTPException):pass
                finally:conn.close()
                require(self.process.poll() is None and self.error is None,'native TLS entry failed')
                time.sleep(.02)
            else:raise TimeoutError('native TLS readiness')
        except BaseException:
            self.close();raise

    def close(self):
        if self.process is not None and self.process.poll() is None:
            direct.lifecycle.guard.signal_identity(self.process.pid,
                self.stage.history[self.process.pid],signal.SIGTERM)
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                direct.lifecycle.guard.signal_identity(self.process.pid,
                    self.stage.history[self.process.pid],signal.SIGKILL)
                self.process.wait(timeout=3)
        self.stop.set()
        if self.thread:self.thread.join(timeout=3)
        if self.peer:self.peer.close();self.peer=None
        save(self.out/'closed.json',dict(pid=None if self.process is None else self.process.pid,
            exit=None if self.process is None else self.process.poll(),
            thread_stopped=self.thread is None or not self.thread.is_alive(),
            error=None if self.error is None else str(self.error)))
        require(self.thread is None or not self.thread.is_alive(),'admission peer did not stop')


class Stage(direct.Stage):
    def __init__(self, phase):
        self.phase=phase;self.edge=None
        require(phase['bounds']==LIMITS,'frozen SDK/TLS limits differ')
        super().__init__(phase['http']);self.counts['ingress_starts']=0
        for path,digest in phase['sources'].items():
            require(sha(ROOT/path)==digest,'SDK stage source drift: '+path)
        for row in [phase['ingress'],*phase['tls_inputs'].values()]:
            require(sha(row['path'])==row['sha256'],'SDK stage binary/TLS drift')
        for row in phase['clips'].values():
            require(sha(row['path'])==row['sha256'],'fixed voice audio drift')
        for path,digest in phase['sdk']['files'].items():
            require(sha(path)==digest,'SDK dependency drift: '+path)

    def budget(self,kind):
        require(time.monotonic()-self.started<LIMITS['seconds']-40,'SDK stage deadline')
        require(self.counts[kind]<LIMITS[kind],'SDK stage attempt bound: '+kind)
        self.artifacts();self.counts[kind]+=1;save(self.out/'counts.json',self.counts)

    def sdk(self,label,action,body,*,clip=None,client='audio',status=200,code=None,control=False):
        self.budget('controls' if control else 'posts')
        output=self.out/(label+'.wire')
        config=dict(action=action,request=body,api_key=KEYS[client],
            base_url=f'https://127.0.0.1:{self.phase["edge_port"]}/voice-test/v1',
            ca=self.phase['tls_inputs']['ca']['path'],output=str(output))
        if clip is not None:config['audio']=clip
        save(self.out/(label+'.request.json'),dict(action=action,request=body,client=client,clip=clip))
        env={k:v for k,v in os.environ.items() if k not in
            ('PYTHONPATH','PYTHONHOME','LD_PRELOAD','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY',
             'http_proxy','https_proxy','all_proxy')}
        env['PYTHONDONTWRITEBYTECODE']='1'
        run=subprocess.run([self.phase['sdk']['python'],'-I','-B',
            str(ROOT/'tools/native_tts/voice_sdk.py')],input=json.dumps(config),capture_output=True,
            text=True,env=env,timeout=min(190,max(1,LIMITS['seconds']-40-(time.monotonic()-self.started))))
        (self.out/(label+'.sdk.log')).write_text(run.stderr)
        require(run.stdout,'SDK returned no receipt: '+label)
        receipt=json.loads(run.stdout);save(self.out/(label+'.sdk.json'),receipt)
        raw=output.read_bytes();exchanges=receipt.get('exchanges',[])
        require(receipt['version']==voice_sdk.VERSION and len(exchanges)==1 and
            exchanges[0]['url'].startswith(config['base_url']) and
            exchanges[0]['status']==status and receipt['wire_bytes']==len(raw) and
            receipt['wire_sha256']==sha(output),'SDK/wire/status mismatch: '+label)
        if status==200:require(run.returncode==0 and receipt['ok'],'SDK method failed: '+label)
        else:
            require(run.returncode==1 and not receipt['ok'],'negative SDK action succeeded: '+label)
            direct.checked_error(status,raw,status,code)
        self.observe(label)
        print(json.dumps(dict(step=label,status=status,counts=self.counts)),flush=True)
        return raw,receipt

    def business(self):
        self.edge=Edge(self);self.core('core-before')
        clips=self.phase['clips'];request=self.plan['requests']
        self.sdk('negated','consent_create',request['consent'],clip=clips['negated'],
                 status=400,code='voice_consent_mismatch')
        raw,_=self.sdk('consent','consent_create',request['consent'],clip=clips['consent'])
        consent=direct.checked_metadata(200,raw,'voice_consent')['id']
        raw,_=self.sdk('rename','consent_update',dict(consent_id=consent,**request['rename']),control=True)
        require(direct.checked_metadata(200,raw,'voice_consent')['name']=='local-consent-renamed','SDK rename differs')
        self.sdk('consent-other','consent_retrieve',dict(consent_id=consent),client='other',status=404,control=True)
        raw,_=self.sdk('consent-list','consent_list',dict(limit=1),control=True)
        require(json.loads(raw)['data'][0]['id']==consent,'SDK list omitted actual consent')
        fields=dict(request['voice'],consent=consent)
        self.sdk('voice-other','voice_create',fields,clip=clips['sample'],client='other',status=404)
        self.sdk('voice-mismatch','voice_create',fields,clip=clips['different'],status=400,code='voice_speaker_mismatch')
        raw,_=self.sdk('voice','voice_create',fields,clip=clips['sample'])
        voice=direct.checked_metadata(200,raw,'voice')['id']
        self.sdk('quota','consent_create',request['consent'],clip=clips['consent'],client='tiny',
                 status=429,code='client_tpm_exhausted')
        old=self.edge.process.pid;self.edge.close();self.edge=None
        self.edge=Edge(self);require(self.edge.process.pid!=old,'entry was not restarted');self.same_model()
        raw,_=self.sdk('consent-after-entry-restart','consent_retrieve',dict(consent_id=consent),control=True)
        require(json.loads(raw)['id']==consent,'state lost across native entry restart')
        speech=dict(model=direct.BASE,input=direct.TARGETS[0],voice=dict(id=voice),response_format='wav',stream_format='sse')
        self.sdk('speech-other','speech',speech,client='other',status=404)
        raw,_=self.sdk('speech','speech',speech)
        audio,usage=direct.speech.sse(raw);direct.check_wave(audio)
        (self.out/'speech.wav').write_bytes(audio);save(self.out/'speech.usage.json',usage)
        self.transcribe('speech-asr',audio,direct.TARGETS[0])
        body=direct.chat.request('Repeat exactly without adding any words: '+direct.TARGETS[1],
                                 'wav',stream=True);body['audio']['voice']=dict(id=voice);body['store']=True
        raw,receipt=self.sdk('chat','chat',body)
        states,usage,events=direct.chat.parse_stream(raw,1);direct.chat.check_usage(usage,1)
        require(direct.normalize(states[0]['transcript'])==direct.normalize(direct.TARGETS[1]),'SDK Chat transcript differs')
        require(len(receipt['result']['events'])==len(events),'SDK omitted stream event')
        for a,b in zip(receipt['result']['events'],events,strict=True):
            require(a['choices']==b['choices'],'SDK changed streamed choices')
        audio=base64.b64decode(states[0]['data'],validate=True);direct.check_wave(audio)
        (self.out/'chat.wav').write_bytes(audio)
        self.transcribe('chat-asr',audio,direct.TARGETS[1])
        completion=events[0]['id']
        raw,_=self.sdk('chat-history','chat_retrieve',dict(completion_id=completion),control=True)
        require(json.loads(raw)['choices'][0]['message']['audio']['id']==states[0]['id'],'SDK history audio differs')
        self.snapshot(consent,voice,completion)
        raw,_=self.sdk('revoke','consent_delete',dict(consent_id=consent),control=True)
        require(json.loads(raw).get('deleted') is True,'SDK revoke failed')
        self.sdk('revoked-speech','speech',speech,status=404)
        body['stream']=False;body.pop('stream_options')
        self.sdk('revoked-chat','chat',body,status=404)
        raw,_=self.sdk('delete-voice','voice_delete',dict(voice_id=voice),control=True)
        require(json.loads(raw).get('deleted') is True,'SDK voice delete failed')
        self.core('core-after');require(self.counts['posts']==LIMITS['posts'],'SDK business checklist incomplete')

    def run(self):
        result=dict(status='FAILED');old=signal.signal(signal.SIGALRM,
            lambda *_:(_ for _ in ()).throw(TimeoutError('SDK stage deadline')))
        signal.alarm(LIMITS['seconds']-40)
        try:
            direct.chat.probe_ports((self.phase['edge_port'],));self.start();self.business()
            result['status']='PASS_OFFICIAL_VOICE_SDK_NATIVE_TLS_ENTRY_RESTART'
        except BaseException:
            result['error']=traceback.format_exc();raise
        finally:
            signal.alarm(0);signal.signal(signal.SIGALRM,old)
            try:
                if self.edge:self.edge.close()
            except BaseException:
                result['status']='FAILED'
                result['entry_cleanup_error']=traceback.format_exc()
                raise
            finally:
                try:
                    self.cleanup();direct.chat.probe_ports((self.phase['edge_port'],))
                    require(direct.defaults()==self.plan['defaults'],'default drift')
                except BaseException:
                    result['status']='FAILED';result['cleanup_error']=traceback.format_exc();raise
                finally:
                    result.update(counts=self.counts,seconds=time.monotonic()-self.started,
                        default_replaced=False,guardian_recovery_retested=False,
                        listening='PENDING',performance='NOT_CLAIMED')
                    save(self.out/'result.json',result)


def main():
    plan=read(Path(sys.argv[1]));Path(plan['http']['output']).mkdir(exist_ok=False)
    with direct.lifecycle.child_subreaper():Stage(plan).run()


if __name__=='__main__':main()
