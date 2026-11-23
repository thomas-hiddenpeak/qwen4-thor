"""One SDK Live session checks manual and voice Decisions plus both transcript triggers."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import client_acceptance as voice, responses_acceptance as live
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service.package import read, require, save, sha

SCHEMA = 'q4t-live-decisions-business-v1'
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=1, audio_inputs=1,
              chats=1, controls=2000, seconds=420)
SOURCE = 'docs/evidence/serve-live-responses-http-20261123/result.json'


def prepare(out, qualification):
    live.prepare(out, qualification)
    plan = read(out/'plan.json')
    policy = read(out/'client-policy.json')
    alpha = next(c for c in policy['clients'] if c['id']=='alpha')
    require(alpha['max_inflight']==1 and alpha['requests_per_minute']==3,'single active data permit')
    # ASR, two frontend turns, two delegations, and transcript evaluations.
    alpha['requests_per_minute']=8
    save(out/'client-policy.json',policy)
    plan.update(schema=SCHEMA,limits=LIMITS,port=18691,operations_port=18692,public_port=18693,
                retained_query=voice.retained_query(),record=dict(path=SOURCE,sha256=sha(ROOT/SOURCE)),
                client_policy_sha256=sha(out/'client-policy.json'))
    save(out/'plan.json',plan)


def observe(out):
    receipt,plan=read(out/'sdk.json'),read(out/'plan.json')
    rows=[json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
    events=[row['event'] for row in rows if row['direction']=='server']
    sent=[row['event'] for row in rows if row['direction']=='client']
    require(receipt['status']=='PASS' and receipt['opens']==1 and receipt['close_code']==1000 and
            len(sent)==receipt['sent'] and events[0]['type']=='session.started' and
            events[-1]['type']=='session.closed' and events[-1]['reason']=='close_requested' and
            events[0]['session']['id']==events[-1]['session']['id'],'single normal close')
    require(not any(e['type']=='error' for e in events),'unexpected Live error')
    def of(kind):return [e for e in events if e['type']==kind]
    ids=[e['event_id'] for e in events if e['type']!='session.output_audio.delta']
    require(all(ids) and len(set(ids))==len(ids),'unique source identities')
    commands=of('session.delegation.created')
    require(len(commands)==2 and commands[0].get('client_event_id')=='manual' and
            not commands[1].get('client_event_id') and all(set(e['delegation'])=={'type','id','target'}
            and e['delegation']['target']=='decisions' for e in commands),'manual and native voice handoff')
    version=read(ROOT/plan['record']['path'])['observation']['version']
    added=[e for e in sent if e['type']=='decision.item.create']
    require(len(added)==1 and len(of('decision.item.created'))==1 and
            added[0]['item']['content'][0]['data']==dict(source='project acceptance record',default_version=version,
            document_sha256=plan['record']['sha256']),'actual project document')
    outputs=of('decision.event')
    by_id={e['event_id']:e for e in events if 'event_id' in e}
    source_ids=set()
    triggers=set()
    choices={}
    scores={}
    decision_tokens=0
    for outer in outputs:
        require(set(outer)=={'type','event_id','source_event_id','source_event_type','delegation_id',
                             'trigger_id','event'} and outer['event']['type']=='decision.completed','strict wrapper')
        decision=outer['event']['decision']
        answer,=decision['answers']
        usage=decision['usage']
        require(decision['model']=='qwen3.8-flash-next' and usage['input_tokens']>0 and usage['output_tokens']==1
                and usage['total_tokens']==usage['input_tokens']+1,'one actual model question')
        decision_tokens+=usage['total_tokens']
        if outer['delegation_id']:
            require(all(outer[k] is None for k in ('trigger_id','source_event_id','source_event_type')) and
                    answer['type']=='choice' and answer['name']=='version' and
                    outer['delegation_id'] not in choices,'distinct delegated answer')
            choices[outer['delegation_id']]=answer['choice']
        else:
            source=by_id[outer['source_event_id']]
            require(source['type']==outer['source_event_type'] and events.index(source)<events.index(outer) and
                    outer['trigger_id'] and outer['trigger_id'] not in triggers and source['event_id'] not in source_ids
                    and answer['type']=='predicate' and answer['name']==('asked' if source['type']=='session.input_transcript.delta'
                    else 'spoken') and 0<=answer['probability']<=1,'actual trigger source and direction')
            triggers.add(outer['trigger_id']);source_ids.add(source['event_id']);scores[source['event_id']]=answer['probability']
    require(set(choices)=={d['delegation']['id'] for d in commands},'both delegated results')
    inputs=of('session.input_transcript.delta')
    spoken=[e for e in of('session.output_transcript.delta') if e.get('client_event_id')=='tell']
    require(inputs and len(spoken)==1 and inputs[-1]['event_id'] in source_ids and spoken[0]['event_id'] in source_ids,
            'final input and linked spoken answer evaluated')
    feedback=[e for e in sent if e['type']=='session.commentary.append']
    require(len(feedback)==1 and feedback[0]['delegation_id']==commands[1]['delegation']['id'] and
            choices[feedback[0]['delegation_id']] in feedback[0]['content'],'actual decision used for commentary')
    pcm=b''.join(base64.b64decode(e['delta'],validate=True) for e in of('session.output_audio.delta'))
    require(len(pcm)>0 and len(pcm)%2==0 and any(pcm),'actual PCM')
    key=lambda name:name+'{client="alpha"}'
    before,after=read(out/'before.json'),read(out/'live-drained.json')
    admitted=after[key('q4t_client_rate_admitted_total')]-before[key('q4t_client_rate_admitted_total')]
    settled=after[key('q4t_client_rate_settled_tokens_total')]-before[key('q4t_client_rate_settled_tokens_total')]
    require(admitted==3+len(outputs) and admitted<=8 and settled>decision_tokens and
            after[key('q4t_client_rate_reserved_tokens')]==0,'ASR, voice and Decisions fully settled')
    normalize=lambda s:re.sub(r'[\s。，,.!！?？]','',s).lower()
    transcript=''.join(e['delta'] for e in inputs)
    return dict(manual=choices[commands[0]['delegation']['id']],automatic=choices[commands[1]['delegation']['id']],
                input=transcript,spoken=spoken[0]['delta'],expected=version,input_probability=scores[inputs[-1]['event_id']],
                output_probability=scores[spoken[0]['event_id']],input_deltas=len(inputs),triggers=len(triggers),
                evaluations=len(outputs),decision_tokens=decision_tokens,client_requests=admitted,settled_tokens=settled,
                pcm_bytes=len(pcm),pcm_sha256=hashlib.sha256(pcm).hexdigest(),listener_heard=False,
                quality=all(c==version for c in choices.values()) and normalize(transcript)==normalize(voice.QUERY) and
                        normalize(spoken[0]['delta'])==normalize(version) and scores[inputs[-1]['event_id']]>.5 and
                        scores[spoken[0]['event_id']]>.5)


class Stage(base.Stage):
    limits=LIMITS
    extra_model_args=live.Stage.extra_model_args

    def business(self):
        plan=self.plan
        clip=voice.retained_query()
        require(clip==plan['retained_query'] and sha(ROOT/SOURCE)==plan['record']['sha256'],'business inputs frozen')
        for name,digest in plan['tts_assets'].items():require(sha(name)==digest,'speech source drift')
        self.snapshot('before')
        self.budget('websocket_sessions');self.budget('audio_inputs')
        args=dict(base_url=f"https://127.0.0.1:{plan['public_port']}/v1",api_key=self.keys['alpha'],
                  ca_file=plan['tls']['certificate'],clip=clip['path'],clip_sha256=clip['sha256'],
                  record_file=str(ROOT/SOURCE),record_sha256=plan['record']['sha256'],journal_file=str(self.out/'events.jsonl'))
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD','NODE_EXTRA_CA_CERTS')}
        env['Q4T_NODE_SDK_DIR']=plan['sdk_directory']
        with (self.out/'sdk.json').open('xb') as out,(self.out/'sdk.stderr').open('xb') as err:
            actor=subprocess.run(['node',str(ROOT/'tools/serve_live/decision_session_sdk.mjs')],input=json.dumps(args).encode(),
                                 stdout=out,stderr=err,timeout=200,env=env)
        self.same_model();self.snapshot('live-drained');self.core('core-after');self.snapshot('final-drained')
        require(actor.returncode==0,'SDK actor failed; original journal retained')
        verdict=observe(self.out)
        save(self.out/'observation.json',verdict)
        require(verdict['quality'],'actual ASR, project answers and two transcript directions')


def finalize(out):
    result=read(out/'result.json')
    require(result['status']=='PASS' and result['seconds']<=LIMITS['seconds'] and
            all(result['counts'][k]==v for k,v in LIMITS.items() if k not in ('seconds','controls')),'single bounded run')
    env=qualify.environment(read(out/'lease/exit.json'),
                            [json.loads(line) for line in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
                            [read(out/'cleanup.json')],expected_starts=1)
    verified=observe(out)
    require(verified==read(out/'observation.json') and verified['quality'],'original evidence verdict')
    save(out/'acceptance.json',dict(environment=env,observation=verified,seconds=result['seconds'],
                                    default_unchanged=result['default_unchanged'],core='42'))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run','finalize'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--qualification',type=Path)
    args=parser.parse_args()
    out=args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'),'private work directory')
    if args.action=='prepare':prepare(out,args.qualification)
    elif args.action=='run':
        plan=read(out/'plan.json')
        require(plan['schema']==SCHEMA,'Live Decisions stage identity')
        with base.lifecycle.child_subreaper():Stage(plan).run()
    else:finalize(out)
