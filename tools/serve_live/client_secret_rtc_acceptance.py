"""Native TLS credential redemption on two actual encrypted WebRTC calls."""
import json
import os
import subprocess
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import media_create_acceptance as media
from tools.serve_live import webrtc_media_acceptance as rtc
from tools.serve_live.client_acceptance import phone_query_matches, retained_query
from tools.serve_service.package import require, save, sha


class Stage(media.Stage):
    limits = dict(starts=1, ingress_starts=1, sdk_posts=5, issuances=3, voice_inputs=1,
                  responses=1, chats=1, controls=2000, seconds=240)

    @staticmethod
    def prepare_fixture(out, plan):
        # This host's qualified NICE plugin was unpacked for WebRTC validation.
        # Verify the actual packaged/system plugin before allocating the GPU.
        retained = ROOT/'.q4t-work/serve-live-20261123/webrtc-media/deps/root/usr/lib/aarch64-linux-gnu/gstreamer-1.0'
        env = dict(os.environ, GST_REGISTRY_1_0=str(out/'fixture-registry.bin'))
        if not env.get('GST_PLUGIN_PATH_1_0') and (retained/'libgstnice.so').is_file():
            env['GST_PLUGIN_PATH_1_0'] = str(retained)
        inspect = subprocess.run(['gst-inspect-1.0','nicesrc'],env=env,text=True,
                                 capture_output=True,timeout=15,check=True)
        path = Path(re.search(r'^  Filename +(.+)$', inspect.stdout, re.M)[1])
        plan['ice_fixture'] = dict(plugin=str(path),sha256=sha(path),
                                   search=env.get('GST_PLUGIN_PATH_1_0',''),
                                   registry=env['GST_REGISTRY_1_0'])

    def start(self):
        ice = self.plan['ice_fixture']
        require(sha(ice['plugin']) == ice['sha256'],'ICE fixture identity')
        os.environ.update(GST_PLUGIN_PATH_1_0=ice['search'],GST_REGISTRY_1_0=ice['registry'])
        super().start()

    def business(self):
        self.snapshot('before')
        rtc.Gst.init(None)
        summary = {}
        for kind in ('realtime','transcription'):
            grant = dict(type=kind)
            if kind == 'realtime':
                grant.update(instructions='Inherited audit instruction.', output_modalities=['text'],
                             audio=dict(input=dict(turn_detection=None)))
            else:
                grant['audio'] = dict(input=dict(turn_detection=None,
                    transcription=dict(model='qwen3-asr-0.6b',language='zh')))
            with rtc.peer_offer('OPUS') as (peer, source, _, channel, messages, offer, _):
                def create(**params):
                    self.budget('issuances')
                    return self.sdk_request('alpha', action='credential-call', credential=grant,
                                            request=dict(sdp=offer, **params))
                if kind == 'transcription':
                    invalid = create(session={'type':'realtime'})
                    require(invalid == dict(status=400,code='realtime_session_type_invalid'), 'credential type switch')
                    summary['wrong_type'] = invalid
                    reply = create()
                else:
                    reply = create(session=dict(type='realtime',instructions='Return the requested number only.'))
                require(reply.get('status') == 201 and reply.get('mime') == 'application/sdp',
                        'expected SDP answer: '+str((reply.get('status'),reply.get('code'))))
                match = re.fullmatch(r'/v1/realtime/calls/(rtc_[A-Za-z0-9_-]+)',reply['location'])
                require(match, 'same-origin call Location')
                call = match[1]
                rtc.accept_answer(peer,reply['sdp'])
                def wait(kind):
                    def poll():
                        require(len(messages) < 2048,'bounded SCTP event count')
                        for wire in list(messages):
                            event = json.loads(wire)
                            require(event['type'] != 'error','Realtime event: '+str(event.get('error')))
                            if event['type'] == kind:
                                return event
                    return rtc.wait_for(poll,kind,45)
                def send(kind, **fields):
                    channel.send_string(json.dumps(dict(type=kind,**fields),ensure_ascii=False))
                session = wait('session.created')['session']
                require(session['type'] == kind,'credential-selected first event')
                if kind == 'realtime':
                    require(session['instructions'] == 'Return the requested number only.' and
                            session['output_modalities'] == ['text'] and
                            session['audio']['input']['turn_detection'] is None,'merged call settings')
                    send('conversation.item.create', item=dict(type='message',role='user',content=[
                        dict(type='input_text',text='What is 6 times 7? Reply with only the number.')]))
                    self.budget('responses')
                    send('response.create', response=dict(max_output_tokens=32))
                    result = wait('response.done')['response']
                    text = ''.join(p['text'] for item in result['output'] if item['type']=='message'
                                   for p in item['content'] if p['type']=='output_text')
                    require(result['status']=='completed' and text.strip()=='42','conversation answer')
                    summary['conversation'] = dict(answer=text,modalities=session['output_modalities'],status=201)
                else:
                    require(session['audio']['input']['transcription']['language']=='zh','inherited ASR settings')
                    clip=retained_query()
                    audio=bytes(14400)+Path(clip['path']).read_bytes()+bytes(28800)
                    self.budget('voice_inputs')
                    for start in range(0,len(audio),960):
                        rtc.push(source,audio[start:start+960])
                        time.sleep(.02)
                    time.sleep(.20)
                    send('input_audio_buffer.commit')
                    heard=wait('conversation.item.input_audio_transcription.completed')['transcript']
                    require(phone_query_matches(heard,clip['expected_transcript']), 'spoken project query: '+heard)
                    summary['transcription'] = dict(transcript=heard,language='zh',codec='opus',status=201)
                hangup = self.sdk_request('alpha',action='hangup',id=call)
                require(hangup==dict(status=200,body=''),'confirmed call cleanup')
            self.snapshot(kind+'-drained')
        save(self.out/'observation.json',summary)
        self.core('core-after')
        self.snapshot('final-drained')


if __name__ == '__main__':
    media.main(Stage, 'q4t-realtime-client-secret-webrtc-v1')
