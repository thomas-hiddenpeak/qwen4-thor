"""One scoped TLS Live session, two delegated Responses, one spoken result and a core Chat."""
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
from tools.serve_live import fixture_acceptance as fixture
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_recovery.bundle import assets
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=1, responses=2,
              spoken=1, chats=1, controls=2000, seconds=480)
SCHEMA = 'q4t-live-responses-business-v1'


def current_model_assets(model, previous):
    current = assets(model)
    require(current.keys() == previous.keys(), 'model asset inventory changed')
    for name, identity in current.items():
        for key in ('dev', 'ino', 'size', 'mtime_ns'):
            require(identity[key] == previous[name][key],
                    f'model asset {key} changed since prior qualification: {name}')
    return current


DEFAULT_QUALIFICATION = ROOT / 'docs/evidence/serve-live-responses-execution-cpu-20261123/result.json'


def prepare(out, qualification=DEFAULT_QUALIFICATION, *, protected=None):
    previous = fixture.WORK / 'fixture-business-01'
    require(read(previous / 'acceptance.json')['observation']['fixture']['source_equal'], 'prior native WSS qualification')
    plan = read(previous / 'plan.json')
    if protected is not None:
        require(set(protected) == set(plan['protected']), 'protected deployment file set')
        plan['protected'] = protected
    plan['model_assets'] = current_model_assets(plan['model'], plan['model_assets'])
    verified = read(qualification)
    require(sha(plan['binary']) == verified['engine_sha256'] and
            sha(plan['ingress']) == verified['ingress_sha256'], 'qualified native binaries required')
    out.mkdir(parents=True, mode=0o700)
    policy = read(previous / 'client-policy.json')
    policy['gateway_token'] = os.urandom(32).hex()
    save(out / 'client-policy.json', policy)
    (out / 'client-policy.json').chmod(0o600)
    tls = certificate(out / 'credentials')
    sources = {name: sha(ROOT / name) for name in plan['sources']}
    for name in subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard',
                                         'src/server','include/q4t/server','tools/serve_live'],cwd=ROOT,text=True).splitlines():
        sources[name] = sha(ROOT / name)
    plan.update(schema=SCHEMA, limits=LIMITS, output=str(out), port=18671, operations_port=18672, public_port=18673,
                binary_sha256=sha(plan['binary']), ingress_sha256=sha(plan['ingress']), sources=sources,
                source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                client_policy_sha256=sha(out/'client-policy.json'), tls=tls,
                tls_files={tls[n]:sha(tls[n]) for n in ('certificate','key')},
                record={'path':'docs/STATUS.md','sha256':sha(ROOT/'docs/STATUS.md')})
    for name, digest in plan['protected'].items():
        require(sha(name) == digest, 'protected default drift')
    save(out/'plan.json', plan)


def observe(out):
    plan=read(out/'plan.json')
    receipt=read(out/'sdk.json')
    rows=[json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
    events=[r['event'] for r in rows if r['direction']=='server']
    sent=[r['event'] for r in rows if r['direction']=='client']
    require(receipt['status']=='PASS' and receipt['opens']==1 and receipt['close_code']==1000 and
            len(sent)==receipt['sent']==7 and events[-1]['type']=='session.closed', 'single original session and drain')
    require(not any(e['type']=='error' for e in events),'successful backend protocol')
    delegates=[e for e in events if e['type']=='session.delegation.created']
    require(len(delegates)==2 and [e.get('client_event_id') for e in delegates]==['lookup','answer'] and
            len({e['delegation']['id'] for e in delegates})==len({e['delegation']['response_id'] for e in delegates})==2,
            'two distinct real delegated owners')
    observed=[]
    for d in delegates:
        chain=[e['event'] for e in events if e['type']=='response.event' and e['delegation_id']==d['delegation']['id']]
        require(d['delegation']['target']=='responses' and chain[0]['type']=='response.created' and
                chain[-1]['type']=='response.completed' and [e['sequence_number'] for e in chain]==list(range(len(chain))),
                'actual complete sequence')
        for e in chain:
            if 'response' in e:
                snap=e['response']
                require(snap['id']==d['delegation']['response_id'] and snap['instructions'] is None and
                        snap['output']==snap['tools']==[] and 'input' not in snap,'bounded public lifecycle projection')
        observed.append([e['item'] for e in chain if e['type']=='response.output_item.done'])
    calls=[i for i in observed[0] if i['type']=='function_call']
    results=[e['item'] for e in sent if e['type']=='response.item.create' and e['item'].get('type')=='function_call_output']
    require(len(calls)==len(results)==1 and calls[0]['name']=='read_project_version' and
            calls[0]['call_id']==results[0]['call_id'] and json.loads(calls[0]['arguments'])=={},
            'matching complete tool call and application result')
    record=receipt['record']
    require(json.loads(results[0]['output'])==record and record['sha256']==plan['record']['sha256'] and
            record['path']==plan['record']['path'],'actual project data backfill')
    text=''.join(p['text'] for item in observed[1] if item['type']=='message' for p in item['content'] if p['type']=='output_text')
    spoken=''.join(e['delta'] for e in events if e['type']=='session.output_transcript.delta')
    audio=b''.join(base64.b64decode(e['delta'],validate=True) for e in events if e['type']=='session.output_audio.delta')
    before,after=read(out/'before.json'),read(out/'live-drained.json')
    def delta(key):
        name=key+'{client="alpha"}'
        return after[name]-before[name]
    require(delta('q4t_client_rate_admitted_total')==3 and delta('q4t_client_rate_settled_tokens_total')>0 and
            after['q4t_client_rate_reserved_tokens{client="alpha"}']==0,'two backend responses and one frontend with actual settlement')
    require(audio and len(audio)%2==0 and any(audio),'real PCM')
    normalize=lambda s: re.sub(r'[\s。，,.!！?？]', '', s).lower()
    return dict(protocol='PASS', version=record['version'], backend=text, spoken=spoken,
                quality=all(normalize(s)==record['version'].lower() for s in (text,spoken)),
                pcm_bytes=len(audio), pcm_sha256=hashlib.sha256(audio).hexdigest(),
                settled_tokens=delta('q4t_client_rate_settled_tokens_total'),requests=3,listener_heard=False)


class Stage(base.Stage):
    limits=LIMITS

    def extra_model_args(self):
        return fixture.Stage.extra_model_args(self)+['--object-store-dir',str(self.out/'objects')]

    def business(self):
        record=self.plan['record']
        require(sha(ROOT/record['path'])==record['sha256'],'project record version frozen at startup')
        self.snapshot('before')
        self.budget('websocket_sessions');self.budget('responses',2);self.budget('spoken')
        args=dict(base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",api_key=self.keys['alpha'],
                  ca_file=self.plan['tls']['certificate'],record_file=str(ROOT/record['path']),
                  record_sha256=record['sha256'],journal_file=str(self.out/'events.jsonl'))
        with (self.out/'sdk.json').open('xb') as output,(self.out/'sdk.stderr').open('xb') as errors:
            actor=subprocess.run(['node',str(ROOT/'tools/serve_live/responses_sdk.mjs')],input=json.dumps(args).encode(),
                                 stdout=output,stderr=errors,timeout=225,
                                 env=dict(os.environ,Q4T_NODE_SDK_DIR=self.plan['sdk_directory']))
        self.same_model();self.snapshot('live-drained')
        self.core('core-after');self.snapshot('final-drained')
        require(actor.returncode==0,'original SDK interaction failed; inspect private journal')
        verdict=observe(self.out)
        save(self.out/'observation.json',verdict)
        require(verdict['quality'],'actual spoken and backend version must match project record')


def finalize(out):
    result=read(out/'result.json')
    require(result['status']=='PASS' and result['seconds']<=LIMITS['seconds'] and
            all(result['counts'][k]==v for k,v in LIMITS.items() if k not in ('seconds','controls')), 'bounded actual work')
    env=qualify.environment(read(out/'lease/exit.json'),
                            [json.loads(row) for row in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
                            [read(out/'cleanup.json')],expected_starts=1)
    verified=observe(out)
    require(verified==read(out/'observation.json') and verified['quality'],'independent observations agree')
    save(out/'acceptance.json',dict(environment=env, observation=verified, seconds=result['seconds'],
                                    default_unchanged=result['default_unchanged'], real_core_reply='42'))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','run','finalize'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--qualification', type=Path, default=DEFAULT_QUALIFICATION)
    args=parser.parse_args()
    out=args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'),'private work directory required')
    if args.action=='prepare':prepare(out,args.qualification)
    elif args.action=='run':
        plan=read(out/'plan.json')
        require(plan['schema']==SCHEMA and sha(plan['ingress'])==plan['ingress_sha256'],'native build identity')
        for name,digest in plan['tts_assets'].items():require(sha(name)==digest,'TTS asset drift')
        with base.lifecycle.child_subreaper():Stage(plan).run()
    else:finalize(out)
