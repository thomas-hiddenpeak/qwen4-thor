"""Fork a real saved voice lookup over both official SDK transports on loopback TLS."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import media_create_acceptance as media
from tools.serve_live import recording_acceptance as recording
from tools.serve_live import responses_acceptance as live
from tools.serve_live import webrtc_media_acceptance as rtc
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=2, sdk_posts=2,
              recalls=1, chats=1, controls=2000, seconds=300)
SCHEMA = 'q4t-live-fork-business-v1'


def object_hashes(directory):
    files = list(directory.iterdir())
    require(0 < len(files) <= 32 and all(
        p.is_file() and not p.is_symlink() and p.stat().st_size <= 40 << 20
        for p in files), 'bounded closed test store')
    return {p.name:sha(p) for p in files}


def media_preflight():
    rtc.Gst.init(None)
    plugins = {}
    for factory in ('webrtcbin','nicesrc','nicesink','srtpenc','srtpdec','opusenc','opusdec'):
        entry = rtc.Gst.ElementFactory.find(factory)
        require(entry, 'media peer unavailable: '+factory)
        filename = entry.get_plugin().get_filename()
        plugins[filename] = sha(filename)
    with rtc.peer_offer('OPUS') as (_, _, _, _, _, offer, _):
        require('m=audio ' in offer and 'm=application ' in offer, 'local peer SDP coverage')
    return plugins


def observe(out):
    plan = read(out/'plan.json')
    rows = [json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
    sent = [r['event'] for r in rows if r['direction']=='client']
    events = [r['event'] for r in rows if r['direction']=='server']
    require([e['type'] for e in sent]==['session.start','session.commentary.append','session.close']
            and sent[0]['session']=={} and plan['answer'].lower() not in
            json.dumps(sent,ensure_ascii=False).lower(), 'no answer or original tool data in new request')
    started, ended = events[0]['session'], events[-1]['session']
    sdk = read(out/'sdk.json')
    require(events[0]['type']=='session.started' and events[-1]['type']=='session.closed'
            and sdk['opens']==1 and sdk['close_code']==1000 and
            plan['source_id']!=started['id']==ended['id']==sdk['id'], 'new independent child')
    for k,v in plan['inherited'].items():
        require(started[k]==v, 'inherited source field '+k)
    require(started['store'] and started['audio']['format']==dict(type='audio/pcm',rate=24000),
            'socket transport settings')
    # The child stored its actual model-visible ledger at drain. Only native
    # item IDs are rebound; ASR origin, tool associations and content must match.
    def checkpoint(identity):
        owner = hashlib.sha256(b'client:alpha').hexdigest()
        key = hashlib.sha256(identity.encode()).hexdigest()
        row = read(out/'objects'/f'{owner}.live_fork_state.fork_{key}.json')['record']
        require(row['owner']==owner and row['data']['source_id']==identity,'scoped checkpoint')
        return row['data']
    parent, child = checkpoint(plan['source_id']), checkpoint(sdk['id'])
    prior, received = parent['frontend_history'], child['frontend_history']
    def content(item):
        return {k:v for k,v in item.items() if k!='id'}
    require(len(received)==len(prior)+2 and
            [content(i) for i in received[:len(prior)]]==[content(i) for i in prior] and
            child['frontend_instructions']==parent['frontend_instructions'] and
            child['decision_items']==parent['decision_items'], 'complete restored model context')
    spoken = ''.join(e['delta'] for e in events if e['type']=='session.output_transcript.delta')
    audio = b''.join(base64.b64decode(e['delta'],validate=True) for e in events
                    if e['type']=='session.output_audio.delta')
    require(re.sub(r'[\s。，,.!！?？]', '', spoken).lower()==plan['answer'].lower() and
            len(audio)>0 and not len(audio)%2 and any(audio), 'real spoken version from source only')
    require(not any(e['type'] in ('error','session.delegation.created','response.event') for e in events),
            'old work was not replayed')
    media_receipt = read(out/'media.json')
    require(media_receipt['id'] not in (sdk['id'],plan['source_id']) and
            media_receipt['status']==201 and media_receipt['close'] and
            media_receipt['denied']=='live_client_event_forbidden', 'independent restricted media child')
    before, after = read(out/'before.json'), read(out/'live-drained.json')
    counter = 'q4t_client_rate_admitted_total{client="alpha"}'
    require(after[counter]-before[counter]==1 and after['q4t_realtime_host_reserved_bytes']==0,
            'one explicit generation; ownership drained')
    source = Path(plan['source_directory'])
    require(object_hashes(source/'objects')==plan['source_hashes'], 'original test store drift')
    return dict(ws=sdk,media=media_receipt,foreign=read(out/'foreign.json'),source=plan['source_id'],
                spoken=spoken,pcm_bytes=len(audio),pcm_sha256=hashlib.sha256(audio).hexdigest(),
                source_lookups_replayed=0,explicit_generations=1,restored_items=len(prior),
                stored_child_items=len(received))


class Stage(recording.Stage):
    limits = LIMITS
    create = media.Stage.create

    def business(self):
        import websocket
        self.snapshot('before')
        plan, source = self.plan, self.plan['source_id']
        url = f'https://127.0.0.1:{plan["public_port"]}/v1'
        self.budget('websocket_sessions')
        try:
            sock = websocket.create_connection(url.replace('https:', 'wss:')+'/live/sessions/'+source+'/fork',
                timeout=8,suppress_origin=True,header={'Authorization':'Bearer '+self.keys['beta']},
                sslopt={'context':ssl.create_default_context(cafile=plan['tls']['certificate'])})
        except websocket.WebSocketBadStatusException as exc:
            require(exc.status_code==404 and json.loads(exc.resp_body)['error']['code']=='object_not_found',
                    'foreign socket owner')
        else:
            sock.close()
            raise ValueError('foreign socket accepted')
        # Exercise WebRTC before recall so model quality cannot mask transport errors.
        rtc.Gst.init(None)
        with rtc.peer_offer('OPUS') as (peer, _, _, channel, messages, offer, _):
            config = dict(store=False,client=dict(data_channel=dict(
                allowed_client_events=['session.close'],allowed_server_events=[
                    dict(type=k) for k in ('session.started','error','session.closed')])))
            rejected = self.create(offer, config, source=source, client='beta')
            require(rejected==dict(status=404,code='object_not_found'), 'foreign WebRTC owner')
            save(self.out/'foreign.json',dict(websocket=404,webrtc=rejected))
            created = self.create(offer, config, source=source)
            require(created['status']==201, 'SDK fork status '+str(created.get('code')))
            identity = created['data']['session']['id']
            rtc.accept_answer(peer, created['data']['transport']['sdp'])
            def wait(kind):
                return rtc.wait_for(lambda:next((e for e in map(json.loads,list(messages))
                                               if e['type']==kind),None),kind,12)
            active = wait('session.started')['session']
            require(active['id']==identity and active['client']==config['client'] and not active['store']
                    and 'format' not in active['audio'], 'negotiated media and permissions')
            channel.send_string(json.dumps(dict(type='session.input_audio.mute',event_id='forbidden')))
            denied = wait('error')
            require(denied['client_event_id']=='forbidden', 'correlated permission denial')
            channel.send_string(json.dumps(dict(type='session.close',event_id='end')))
            end = wait('session.closed')
            save(self.out/'media.json',dict(id=identity,status=created['status'],
                 close=end['session']['id']==identity,denied=denied['error']['code']))
        self.snapshot('media-drained')
        self.budget('websocket_sessions')
        self.budget('recalls')
        args = dict(base_url=url, source=source,key=self.keys['alpha'], ca_file=plan['tls']['certificate'],
                    journal=str(self.out/'events.jsonl'))
        with (self.out/'sdk.json').open('xb') as output, (self.out/'sdk.stderr').open('xb') as errors:
            result = subprocess.run(['node',ROOT/'tools/serve_live/fork_sdk.mjs'],input=json.dumps(args).encode(),
                stdout=output,stderr=errors,timeout=155,env=dict(os.environ,Q4T_NODE_SDK_DIR=plan['sdk_directory']))
        require(result.returncode==0, 'SDK fork failed; see retained journal')
        self.snapshot('live-drained')
        self.core('core-after')
        self.snapshot('final-drained')
        save(self.out/'observation.json', observe(self.out))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','run','finalize'])
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--source',type=Path)
    p.add_argument('--qualification',type=Path)
    args=p.parse_args()
    out=args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'private stage')
    if args.action=='prepare':
        source=args.source.resolve(strict=True)
        require(source.is_relative_to(ROOT/'.q4t-work') and not out.is_relative_to(source) and
                not source.is_relative_to(out), 'separate closed fixture and private destination')
        sdk=read(source/'sdk.json')
        reviewed=recording.inspect_fork_state(source,sdk)
        plugins=media_preflight()
        live.prepare(out,args.qualification)
        plan=read(out/'plan.json')
        worker=Path(plan['binary']).with_name('q4t-live-webrtc')
        require(sha(worker)==read(args.qualification)['worker_sha256'],'qualified media worker')
        original=object_hashes(source/'objects')
        shutil.copytree(source/'objects',out/'objects')
        require(object_hashes(out/'objects')==original,'byte-identical copied test store')
        config=sdk['events'][0]['session']
        plan.update(schema=SCHEMA,limits=LIMITS,source_id=sdk['recording']['id'],source_directory=str(source),
                    source_hashes=original,answer=reviewed['answer'],worker=str(worker),worker_sha256=sha(worker),
                    inherited={k:config[k] for k in ('model','instructions','input','triggers')},
                    media_plugins=plugins,port=18741,operations_port=18742,public_port=18743)
        save(out/'plan.json',plan)
    elif args.action=='run':
        plan=read(out/'plan.json')
        require(plan['schema']==SCHEMA and sha(plan['worker'])==plan['worker_sha256'],'stage identity')
        require(media_preflight()==plan['media_plugins'], 'qualified media peer')
        with base.lifecycle.child_subreaper():
            Stage(plan).run()
    else:
        plan,result=read(out/'plan.json'),read(out/'result.json')
        require(result['status']=='PASS' and result['seconds']<=LIMITS['seconds'] and result['default_unchanged']
                and all(result['counts'][k]==v for k,v in LIMITS.items() if k not in ('seconds','controls')),
                'bounded business and cleanup')
        env=qualify.environment(read(out/'lease/exit.json'),
            [json.loads(line) for line in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
            [read(out/'cleanup.json')],expected_starts=1)
        verdict=observe(out)
        require(verdict==read(out/'observation.json') and all(sha(ROOT/n)==h for n,h in plan['sources'].items()),
                'independent journal review and source identities')
        save(out/'acceptance.json',dict(environment=env,seconds=result['seconds'],observation=verdict,
                                       default_unchanged=True,core='42'))


if __name__=='__main__':
    main()
