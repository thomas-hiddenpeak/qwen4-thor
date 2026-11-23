"""Official Realtime SDK SDP, scoped sideband, duplex project lookup and hangup."""
import array
import json
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
    limits = dict(starts=1, ingress_starts=1, sdk_posts=7, sidebands=2,
                  voice_inputs=1, client_results=1, chats=1, controls=2000, seconds=320)

    def create_call(self, sdp, **configuration):
        reply = self.sdk_request('alpha', action='create', request=dict(sdp=sdp, **configuration))
        require(reply['status'] == 201 and reply.get('mime') == 'application/sdp' and
                reply['sdp'].startswith('v=0') and 'a=fingerprint:' in reply['sdp'],
                'expected real SDP 201: '+repr((reply['status'],reply.get('code'))))
        match = re.fullmatch(r'/v1/realtime/calls/(rtc_[A-Za-z0-9_-]+)', reply['location'])
        require(match, 'same-origin call Location')
        return match[1], reply['sdp']

    def business(self):
        self.snapshot('before')
        rtc.Gst.init(None)
        def hangup(call, client='alpha'):
            return self.sdk_request(client, action='hangup', id=call)
        with rtc.peer_offer('OPUS') as (_, _, _, _, _, offer, _):
            raw, _ = self.create_call(offer)
            require(hangup(raw) == dict(status=200, body=''), 'release pending raw SDP call')
        instructions = '根据用户的录音，调用工具查询实际项目记录。'
        config = dict(type='realtime', model='qwen3.8-flash-next', instructions=instructions,
            output_modalities=['audio'], audio=dict(input=dict(format=dict(type='audio/pcm',rate=24000),
            turn_detection=None,transcription=dict(model='qwen3-asr-0.6b')),output=dict(voice='vivian')))
        with rtc.peer_offer('OPUS') as (peer, source, sink, channel, messages, offer, _):
            call, answer = self.create_call(offer, session=config)
            require(call != raw, 'independent call IDs')
            rtc.accept_answer(peer, answer)
            def wait(kind, begin=0, seconds=45):
                def poll():
                    require(len(messages)<4096, 'bounded inbound events')
                    for wire in list(messages)[begin:]:
                        event = json.loads(wire)
                        require(event['type'] != 'error', 'Realtime error: '+str(event.get('error')))
                        if event['type'] == kind:
                            return event
                return rtc.wait_for(poll, kind, seconds)
            def send(kind, **fields):
                channel.send_string(json.dumps(dict(type=kind, **fields), ensure_ascii=False))
            try:
                session = wait('session.created')['session']
                require(session['instructions'] == instructions and session['id'].startswith('sess_'),
                        'configured primary session')
                wait('conversation.created')
                foreign = hangup(call, 'beta')
                require(foreign['status']==404, 'foreign hangup')
                denied = self.sdk_request('beta', action='attach', id=call)
                require(denied == dict(status=404), 'foreign sideband')
                attached = self.sdk_request('alpha', action='attach', id=call)
                require(attached == dict(status=101, session=session['id'], instructions=instructions),
                        'same live session on sideband')
                busy = self.sdk_request('alpha', action='create', request=dict(sdp=offer))
                require(busy == dict(status=429,code='client_capacity_exhausted'), 'active call admission')
                clip = retained_query()
                audio = bytes(14400) + Path(clip['path']).read_bytes() + bytes(28800)
                self.budget('voice_inputs')
                for i in range(0, len(audio), 960):
                    rtc.push(source, audio[i:i+960])
                    time.sleep(.020)
                time.sleep(.20)
                send('input_audio_buffer.commit',event_id='commit_query')
                heard = wait('conversation.item.input_audio_transcription.completed')['transcript']
                require(phone_query_matches(heard, clip['expected_transcript']), 'spoken query: '+heard)
                tool = dict(type='function',name='lookup_project_record',parameters=dict(type='object',
                    properties=dict(record_key=dict(type='string',enum=['release'])),required=['record_key'],
                    additionalProperties=False))
                send('response.create',response=dict(output_modalities=['text'],max_output_tokens=128,
                    instructions='Use lookup_project_record with record_key release for the spoken project version question.',
                    tools=[tool],tool_choice=dict(type='function',name=tool['name'])))
                first = wait('response.done')['response']
                save(self.out/'tool-terminal.json', first)
                calls = [item for item in first['output'] if item['type']=='function_call']
                require(first['status']=='completed' and len(calls)==1 and calls[0]['status']=='completed' and
                        calls[0]['name']==tool['name'] and calls[0]['call_id'] and
                        json.loads(calls[0]['arguments'])==dict(record_key='release'), 'completed exact call')
                self.budget('client_results')
                record = self.plan['record']
                path = ROOT/record['path']
                require(sha(path)==record['sha256'], 'frozen real project record')
                version = re.search(r'默认\s*(rev\d+)', path.read_text()).group(1)
                at = len(messages)
                send('conversation.item.create',item=dict(type='function_call_output',id='release_result',
                    call_id=calls[0]['call_id'],output=json.dumps(dict(record_key='release',version=version,
                    source=record['path'],sha256=record['sha256']))))
                require(wait('conversation.item.done',at)['item']['id']=='release_result','result correlated')
                at = len(messages)
                send('response.create',response=dict(output_modalities=['audio'],tools=[],tool_choice='none',
                    max_output_tokens=3457,instructions='只朗读刚收到的真实工具结果中的版本号一次，保留 rev 前缀，不要添加其他内容。'))
                second = wait('response.done',at,60)['response']
                spoken = wait('response.output_audio_transcript.done',at)['transcript']
                require(second['status']=='completed' and re.sub(r'[^a-z0-9]','',spoken.lower())==version,
                        'spoken project version: '+spoken)
                for terminal in ('started','stopped'):
                    require(wait('output_audio_buffer.'+terminal,at)['response_id']==second['id'],
                            'actual playout '+terminal)
                pcm = array.array('h')
                while (sample := sink.emit('try-pull-sample',0)) is not None:
                    buf = sample.get_buffer()
                    pcm.frombytes(buf.extract_dup(0,buf.get_size()))
                    require(len(pcm)<=24000*40,'bounded decoded observation')
                require(len(pcm)>=9600 and max(map(abs,pcm),default=0)>500, 'actual decoded SRTP speech')
                require(not any(json.loads(w)['type']=='response.output_audio.delta' for w in messages),
                        'audio travels on the media track')
                require(hangup(call)==dict(status=200,body=''), 'worker acknowledged hangup')
                require(hangup(call)['status']==404, 'released call identity')
                save(self.out/'observation.json',dict(raw_sdp_status=201, multipart_status=201,
                    raw_pending_hangup=200, scope=foreign['status'], foreign_sideband=denied['status'],
                    owning_sideband=attached['status'], capacity=busy['status'], call=call, codec='opus',
                    input=heard, tool=tool['name'], arguments=json.loads(calls[0]['arguments']),
                    output=spoken, record=record, decoded_samples=len(pcm),peak=max(map(abs,pcm)),
                    audio_base64_events=0, hangup=200, repeated_hangup=404))
            finally:
                save(self.out/'events.json',[json.loads(w) for w in messages])
        self.same_model()
        self.snapshot('live-drained')
        self.core('core-after')
        self.snapshot('final-drained')


if __name__=='__main__':
    media.main(Stage, 'q4t-realtime-webrtc-create-business-v1')
