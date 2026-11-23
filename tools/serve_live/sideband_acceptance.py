"""Exercise trusted Live attachment, replay and media isolation on a real TLS call."""
import argparse
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_live import media_create_acceptance as media
from tools.serve_live import responses_acceptance as live
from tools.serve_live import webrtc_media_acceptance as rtc
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service.package import read,require,save,sha

LIMITS=dict(starts=1,ingress_starts=1,sdk_posts=1,observer_handshakes=4,
            fixture_plays=1,chats=1,controls=2000,seconds=300)
SCHEMA='q4t-live-sideband-business-v1'


class Stage(media.Stage):
    limits=LIMITS

    def missing(self, key, session):
        import websocket
        target=f'wss://127.0.0.1:{self.plan["public_port"]}/v1/live/sessions/{session}/attach'
        self.budget('observer_handshakes')
        try:
            ws=websocket.create_connection(target,timeout=5,suppress_origin=True,
                header={'Authorization':'Bearer '+self.keys[key]},
                sslopt={'context':ssl.create_default_context(cafile=self.plan['tls']['certificate'])})
        except websocket.WebSocketBadStatusException as error:
            require(error.status_code==404 and json.loads(error.resp_body)['error']['code']=='object_not_found',
                    'scoped absence response')
            return
        ws.close()
        raise ValueError('unowned attachment accepted')

    def business(self):
        self.snapshot('before')
        rtc.Gst.init(None)
        with rtc.peer_offer('OPUS') as (peer,source,sink,channel,messages,offer,_):
            created=self.create(offer,dict(model='qwen3.8-flash-next',client=dict(data_channel=dict(
                allowed_client_events=['session.input_audio.unmute'],
                allowed_server_events=[dict(type=kind) for kind in
                    ['session.started','error','session.input_audio.unmuted','session.closed']]))))
            require(created['status']==201,'media creation')
            session=created['data']['session']['id']
            rtc.accept_answer(peer,created['data']['transport']['sdp'])
            def received(kind):
                return any(json.loads(m)['type']==kind for m in messages)
            rtc.wait_for(lambda:received('session.started'),'primary started')
            self.missing('beta',session)
            self.missing('alpha','live_unknown')
            self.budget('observer_handshakes',2)
            self.budget('fixture_plays')
            args=dict(base_url=f'https://127.0.0.1:{self.plan["public_port"]}/v1',
                      api_key=self.keys['alpha'],session=session,ca_file=self.plan['tls']['certificate'],
                      directory=str(self.out))
            env=dict(os.environ,Q4T_NODE_SDK_DIR=self.plan['sdk_directory'])
            with (self.out/'sdk.json').open('xb') as stdout,(self.out/'sdk.stderr').open('xb') as stderr:
                actor=subprocess.Popen(['node',ROOT/'tools/serve_live/sideband_sdk.mjs'],stdin=subprocess.PIPE,
                    stdout=stdout,stderr=stderr,env=env)
                try:
                    actor.stdin.write(json.dumps(args).encode())
                    actor.stdin.close()
                    until=time.monotonic()+40
                    sent_primary=False
                    inputs=0
                    next_input=0
                    decoded=bytearray()
                    while time.monotonic()<until and actor.poll() is None:
                        if (self.out/'detached').exists() and not sent_primary:
                            channel.send_string(json.dumps(dict(type='session.input_audio.unmute',
                                                               event_id='primary_gap')))
                            sent_primary=True
                        if sent_primary and received('session.input_audio.unmuted'):
                            (self.out/'primary-continued').touch(exist_ok=True)
                        if (self.out/'muted').exists() and inputs<24 and time.monotonic()>=next_input:
                            rtc.push(source,rtc.tone(400))
                            inputs+=1
                            next_input=time.monotonic()+.020
                        if inputs==24 and time.monotonic()>next_input+.15:
                            (self.out/'input-sent').touch(exist_ok=True)
                        while (sample:=sink.emit('try-pull-sample',0)) is not None:
                            buf=sample.get_buffer()
                            decoded.extend(buf.extract_dup(0,buf.get_size()))
                            require(len(decoded)<24000*2*15,'bounded decoded fixture')
                        if len(decoded)>=4800:
                            if rtc.amplitude(decoded[-4800:],850)>2500:
                                (self.out/'rtp-received').touch(exist_ok=True)
                        time.sleep(.005)
                    require(actor.poll()==0,'SDK attachment failed; see sdk.stderr')
                finally:
                    if actor.poll() is None:
                        actor.terminate()
                        try: actor.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            actor.kill()
                            actor.wait()
            sdk=read(self.out/'sdk.json')
            raw=(self.out/'input.pcm').read_bytes()
            require(len(raw)>=9600,'substantial pre-mute input at observer')
            center=len(raw)//4*2
            strength=rtc.amplitude(raw[center-2400:center+2400],400)
            require(strength>2500 and sdk['replay_occurrences']==1 and
                    sdk['ended_session']==session and (self.out/'rtp-received').is_file(),
                    'sideband reflected audio, original replay and terminal identity')
            rtc.wait_for(lambda: received('session.closed'),'primary terminal',3)
            require(not received('session.input_audio.muted') and not received('session.output_audio.delta')
                    and not received('session.input_transcript.delta'),
                    'separate frontend permissions and graceful end')
            save(self.out/'observation.json',dict(sdk=sdk,create_http_status=created['status'],foreign_status=404,unknown_status=404,
                 primary_during_detach=True,reflected_input_400hz_amplitude=round(strength),
                 decoded_opus_samples=len(decoded)//2))
        self.same_model()
        after=self.snapshot('live-drained')
        require(after['q4t_live_sideband_connections']==0 and
                after['q4t_client_requests_active{client="alpha",lane="live_sideband"}']==0 and
                after['q4t_client_rate_admitted_total{client="alpha"}']==0,
                'no leaked observer resources or inference from muted input')
        self.core('core-after')
        self.snapshot('final-drained')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','run','finalize'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--qualification',type=Path)
    args=parser.parse_args()
    out=args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'),'private stage')
    if args.action=='prepare':
        live.prepare(out,args.qualification)
        plan=read(out/'plan.json')
        worker=Path(plan['binary']).with_name('q4t-live-webrtc')
        require(sha(worker)==read(args.qualification)['worker_sha256'],'qualified media worker')
        plan.update(schema=SCHEMA,limits=LIMITS,worker=str(worker),worker_sha256=sha(worker),
                    port=18731,operations_port=18732,public_port=18733)
        save(out/'plan.json',plan)
    elif args.action=='run':
        plan=read(out/'plan.json')
        require(plan['schema']==SCHEMA and sha(plan['worker'])==plan['worker_sha256'],'stage identity')
        with base.lifecycle.child_subreaper():
            Stage(plan).run()
    else:
        plan,result=read(out/'plan.json'),read(out/'result.json')
        require(result['status']=='PASS' and result['default_unchanged'] and result['seconds']<=LIMITS['seconds']
                and all(result['counts'][k]==v for k,v in LIMITS.items() if k not in ('seconds','controls')),
                'complete bounded run')
        env=qualify.environment(read(out/'lease/exit.json'),
            [json.loads(row) for row in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
            [read(out/'cleanup.json')],expected_starts=1)
        require(all(sha(ROOT/n)==h for n,h in plan['sources'].items()),'qualified sources')
        save(out/'acceptance.json',dict(environment=env,observation=read(out/'observation.json'),
            seconds=result['seconds'],default_unchanged=True,core='42'))


if __name__=='__main__':
    main()
