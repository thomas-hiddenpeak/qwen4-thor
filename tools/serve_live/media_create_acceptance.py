"""One TLS model start, scoped SDK WebRTC creation, project query and core Chat."""
import argparse
import array
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import responses_acceptance as live
from tools.serve_live import webrtc_media_acceptance as rtc
from tools.serve_live.client_acceptance import retained_query
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, ingress_starts=1, sdk_posts=3, voice_inputs=1,
              client_results=1, chats=1, controls=2000, seconds=320)
SCHEMA = 'q4t-live-media-create-business-v1'


class Stage(live.Stage):
    limits = LIMITS

    def create(self, sdp, session):
        self.budget('sdk_posts')
        args = dict(key=self.keys['alpha'], base_url=f'https://127.0.0.1:{self.plan["public_port"]}/v1',
                    request=dict(transport=dict(type='webrtc', sdp=sdp), session=session))
        env = dict(os.environ, Q4T_NODE_SDK_DIR=self.plan['sdk_directory'],
                   NODE_EXTRA_CA_CERTS=self.plan['tls']['certificate'])
        child = subprocess.run(['node', ROOT/'tools/serve_live/media_create_sdk.mjs'],
            input=json.dumps(args), text=True, capture_output=True, timeout=25, env=env, check=False)
        require(child.returncode == 0 and len(child.stdout) < 131072,
                'SDK did not return a bounded response: '+child.stderr[-1200:])
        return json.loads(child.stdout)

    def business(self):
        self.snapshot('before')
        rtc.Gst.init(None)
        config = dict(model='qwen3.8-flash-next', instructions=(
            '这是项目资料查询服务。用户询问项目资料时，先使用 q4t_live_delegate 委派客户端读取真实项目记录。'
            '收到客户端结果前不要回答或编造版本号。收到应用提供的真实结果后，只说版本号一次，不要解释，不要再次委派。'),
            input=[dict(role='developer',content=[dict(text='保持项目查询任务和真实结果的关联。')]),
                   dict(role='user',content=[dict(text='接下来的录音包含我的问题，请先使用客户端查询。')]),
                   dict(role='assistant',content=[dict(text='明白。')])],
            audio=dict(output=dict(voice='vivian')),
            delegation=dict(type='client', acknowledgement_delay_ms=600000),
            client=dict(data_channel=dict(allowed_client_events=[
                'session.close','session.thinking.append','session.commentary.append'])))
        bad = self.create('v=0\r\n', config)
        require(bad == dict(status=400, code='live_invalid_sdp'), 'invalid SDP: '+repr(bad))
        with rtc.peer_offer('OPUS') as (peer, source, sink, channel, messages, offer, _):
            response = self.create(offer, config)
            require(response['status'] == 201, 'SDK create: '+repr(response.get('code')))
            session_id = response['data']['session']['id']
            answer = response['data']['transport']
            require(answer['type'] == 'webrtc' and session_id.startswith('live_'), 'media identity')
            rtc.accept_answer(peer, answer['sdp'])
            def find(kind):
                for wire in list(messages):
                    event = json.loads(wire)
                    if event['type'] == kind:
                        return event
                return None
            def wait(kind, seconds=15):
                return rtc.wait_for(lambda: find(kind), kind, seconds)
            def send(kind, **fields):
                channel.send_string(json.dumps(dict(type=kind, **fields)))
            try:
                require(wait('session.started')['session']['id'] == session_id, 'active owner identity')
                busy = self.create(offer, config)
                require(busy == dict(status=429, code='client_capacity_exhausted'),
                        'continuous client ownership: '+repr(busy))
                send('session.input_audio.mute', event_id='denied')
                denied = wait('error')
                require(denied.get('client_event_id') == 'denied' and
                        denied['error']['code'] == 'live_client_event_forbidden', 'frontend authority')
                clip = retained_query()
                raw = Path(clip['path']).read_bytes()
                self.budget('voice_inputs')
                audio = bytes(14400) + raw + bytes(57600)
                for i in range(0, len(audio), 960):
                    rtc.push(source, audio[i:i+960])
                    time.sleep(.020)
                delegated = wait('session.delegation.created', 45)['delegation']
                require(delegated['target'] == 'client', 'project query delegated')
                record = self.plan['record']
                path = ROOT/record['path']
                require(sha(path) == record['sha256'], 'live project record')
                version = re.search(r'默认\s*(rev\d+)', path.read_text()).group(1)
                self.budget('client_results')
                send('session.thinking.append',event_id='lookup',delegation_id=delegated['id'],
                     content=json.dumps(dict(**record,version=version), ensure_ascii=False))
                wait('session.thinking.appended')
                send('session.commentary.append',event_id='answer', delegation_id=delegated['id'],
                     content='项目资料已读取。只说刚才真实记录中的版本号一次。')
                wait('session.commentary.appended')
                wait('session.output_transcript.delta', 60)
                pcm = array.array('h')
                def speech():
                    while (sample := sink.emit('try-pull-sample',0)) is not None:
                        buf = sample.get_buffer()
                        pcm.frombytes(buf.extract_dup(0,buf.get_size()))
                        require(len(pcm) <= 24000*40, 'audio observation bound')
                    return len(pcm) >= 9600 and max(map(abs,pcm),default=0) > 500
                rtc.wait_for(speech, 'actual decoded model audio', 15)
                send('session.close',event_id='finish')
                require(wait('session.closed', 50)['session']['id'] == session_id, 'terminal identity')
                events = [json.loads(m) for m in messages]
                transcript = lambda kind: ''.join(e['delta'] for e in events if e['type'] == kind)
                heard = transcript('session.input_transcript.delta')
                spoken = transcript('session.output_transcript.delta')
                save(self.out/'observation.json',dict(sdk_status=response['status'],invalid=bad,capacity=busy,
                     denied=denied['error']['code'], session=session_id, codec='opus', input=heard,
                     output=spoken, version=version, decoded_samples=len(pcm),peak=max(map(abs,pcm)),
                     audio_base64_events=sum(e['type']=='session.output_audio.delta' for e in events)))
                require(heard == clip['expected_transcript'] and
                        re.sub(r'[^a-z0-9]','',spoken.lower()) == version and
                        sum(e['type']=='error' for e in events)==1 and
                        not any(e['type']=='session.output_audio.delta' for e in events),
                        'voice quality and media separation')
            finally:
                save(self.out/'events.json',[json.loads(m) for m in messages])
        self.same_model()
        self.snapshot('live-drained')
        self.core('core-after')
        self.snapshot('final-drained')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','run','finalize'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--qualification',type=Path)
    args=parser.parse_args()
    out=args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence path')
    if args.action=='prepare':
        live.prepare(out,args.qualification)
        plan=read(out/'plan.json')
        worker=Path(plan['binary']).with_name('q4t-live-webrtc')
        require(sha(worker)==read(args.qualification)['worker_sha256'], 'qualified media worker')
        plan.update(schema=SCHEMA,limits=LIMITS,worker=str(worker),worker_sha256=sha(worker),
                    port=18721,operations_port=18722,public_port=18723)
        save(out/'plan.json',plan)
    elif args.action=='run':
        plan=read(out/'plan.json')
        require(plan['schema']==SCHEMA and sha(plan['worker'])==plan['worker_sha256'],'stage identity')
        with base.lifecycle.child_subreaper():
            Stage(plan).run()
    else:
        result,plan=read(out/'result.json'),read(out/'plan.json')
        require(result['status']=='PASS' and result['default_unchanged'] and
                result['seconds'] <= LIMITS['seconds'] and
                all(result['counts'][k]==v for k,v in LIMITS.items() if k not in ('seconds','controls')),
                'complete bounded run')
        env=qualify.environment(read(out/'lease/exit.json'),
            [json.loads(line) for line in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
            [read(out/'cleanup.json')],expected_starts=1)
        require(all(sha(ROOT/n)==h for n,h in plan['sources'].items()),'qualified sources')
        save(out/'acceptance.json',dict(environment=env,observation=read(out/'observation.json'),
             seconds=result['seconds'],default_unchanged=True,core='42'))


if __name__=='__main__':
    main()
