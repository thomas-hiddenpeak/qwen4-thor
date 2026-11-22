"""Bounded native Live recording, SDK download, scoped privacy and real voice."""
import argparse
from array import array
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import wave

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import client_acceptance as voice, fixture_checks
from tools.serve_live import responses_acceptance as live
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service.package import read, require, save, sha

SCHEMA = 'q4t-live-recording-business-v1'
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=1, fixture_plays=1,
              audio_inputs=1, responses=2, chats=1, downloads=3, controls=2000,
              seconds=480)


def prepare(out, qualification):
    live.prepare(out, qualification)
    plan = read(out/'plan.json')
    record = plan['record']
    version = re.search(r'默认\s*rev(\d+)', (ROOT/record['path']).read_text())
    require(version and sha(ROOT/record['path']) == record['sha256'], 'current version record')
    record['version'] = 'rev' + version[1]
    plan.update(schema=SCHEMA, limits=LIMITS, port=18701, operations_port=18702,
                public_port=18703, verify_fork_state=True, retained_query=voice.retained_query())
    save(out/'plan.json', plan)


def track(samples, source):
    """Check every stereo sample, including intervening silence and the tail."""
    cursor = 0
    spans = []
    for segment in source:
        require(segment and len(segment)%2 == 0 and any(segment), 'nonempty real PCM segment')
        offset = samples.find(segment, cursor)
        require(offset >= cursor and not offset%2 and not any(samples[cursor:offset]),
                'recorded track differs from observed PCM')
        spans.append(dict(start_frame=offset//2, frames=len(segment)//2,
                          sha256=hashlib.sha256(segment).hexdigest()))
        cursor = offset + len(segment)
    require(not any(samples[cursor:]), 'unexpected audio after final observed segment')
    return spans


def inspect_fork_state(out, receipt):
    session = receipt['recording']['id']
    require(re.fullmatch(r'live_[0-9a-f]{32}', session), 'recorded source identity')
    owner = hashlib.sha256(b'client:alpha').hexdigest()
    objects = out/'objects'

    def record(kind, identity):
        path = objects/f'{owner}.{kind}.{identity}.json'
        require(0 < path.stat().st_size <= 8 << 20, 'bounded fixture record')
        raw = path.read_bytes()
        envelope = json.loads(raw)
        prefix = b'{"record":'
        suffix = b',"sha256":"' + envelope['sha256'].encode('ascii') + b'"}'
        require(raw.startswith(prefix) and raw.endswith(suffix) and
                hashlib.sha256(raw[len(prefix):-len(suffix)]).hexdigest() == envelope['sha256'],
                'object store content digest')
        row = envelope['record']
        require((row['owner'],row['kind'],row['id'])==(owner,kind,identity),
                'scope and identity of durable record')
        return row

    wave = record('live_recording',session)
    key = 'fork_'+hashlib.sha256(session.encode()).hexdigest()
    require(wave['data']['fork_state_id']==key and 'fork_state' not in wave['data'],
            'recording links independent state')
    saved = record('live_fork_state',key)
    state = saved['data']
    require(saved['expires_at']==wave['expires_at'] and state['source_id']==session
            and state['format']=='q4t.live_fork.v1', 'joint source and retention')
    return verify_conversation(state, receipt)


def verify_conversation(state, receipt):
    start = receipt['events'][0]['session']
    config = state['configuration']
    require(all(config[k] == start[k] for k in ('instructions','input','model','audio','store'))
            and not state['requires_tool_credentials'], 'source configuration')
    history = state['frontend_history']
    frames, background, answers = [], [], []
    heading = '\n\n[Audio input transcript: recognized speech from the supplied recording]\n'
    end = '\n[End audio input transcript]\n\n'
    prefix = 'Application background context, not a new user request: '
    for item in history:
        for index, part in enumerate(item.get('content', [])):
            text = part.get('text', '')
            if item.get('role') == 'user' and part.get('type') == 'input_text':
                if text.startswith(heading):
                    require(text.endswith(end), 'complete native ASR frame')
                    value = json.loads(text[len(heading):-len(end)])
                    expected = dict(source=f"conversation.item[{item['id']}].content[{index}]",
                                    transcript=receipt['expected_input'])
                    require(value == expected, 'source-bound ASR frame')
                    frames.append(value)
                if text.startswith(prefix):
                    background.append(json.loads(text[len(prefix):]))
            if item.get('role') == 'assistant' and part.get('type') == 'output_audio':
                answers.append(part['transcript'])
    require(len(frames) == 1 and background == [receipt['record']],
            'one real audio input and exact project record')
    require(answers == [receipt['record']['version']], 'spoken answer in history')
    calls = [v['call_id'] for v in history if v.get('type') == 'function_call']
    results = [v['call_id'] for v in history if v.get('type') == 'function_call_output']
    require(len(calls) == 1 and calls == results and state['responses'] is None,
            'one correlated frontend handoff and independent delegate mode')
    decisions = state['decision_history']
    require(state['frontend_instructions'].startswith(config['instructions'])
            and decisions[-2:] == [dict(role='user',content=frames[0]['transcript']),
                                    dict(role='assistant',content=answers[0])],
            'inherited instructions and independent transcript ledger')
    return dict(frontend_items=len(history),handoff_pairs=len(calls),scoped=True,
                input=frames[0]['transcript'],answer=answers[0],
                source_and_fork_expire_together=True)


def observe(out):
    plan, receipt = read(out/'plan.json'), read(out/'sdk.json')
    clip = plan['retained_query']
    rows = [json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
    observed = fixture_checks.observe(receipt, rows, read(out/'before.json'),
        read(out/'live-drained.json'), plan['record'], clip, store=True)
    require(all(row['status'] == 'PASS' for row in observed['quality']), 'real ASR and spoken project result')
    artifact = out/'recording.wav'
    envelope = receipt['recording']
    events = receipt['events']
    require(receipt['before_download'] == envelope['other_client'] == 404 and
            envelope['status'] == 200 and envelope['id'] == events[0]['session']['id'] and
            44 < artifact.stat().st_size == envelope['bytes'] <= 32 << 20 and
            sha(artifact) == envelope['sha256'], 'download and visibility receipts')
    with wave.open(str(artifact), 'rb') as content:
        frames = content.getnframes()
        require(content.getparams()[:3] == (2, 2, 24000) and
                44 + frames*4 == artifact.stat().st_size, 'complete stereo WAVE')
        stereo = array('H')
        stereo.frombytes(content.readframes(frames))
    # Slicing uint16 pairs preserves the original little-endian wire bytes
    # even on a big-endian evaluator: no numerical reinterpretation is needed.
    input_audio = Path(clip['path']).read_bytes()
    require(hashlib.sha256(input_audio).hexdigest() == clip['sha256'], 'frozen input')
    left = track(stereo[::2].tobytes(), [input_audio])
    parts, buffer = [], bytearray()
    for event in events:
        if event['type'] == 'session.output_audio.delta':
            buffer.extend(base64.b64decode(event['delta'], validate=True))
        if event['type'] in ('session.output_audio.fixture.played','session.output_transcript.delta'):
            parts.append(bytes(buffer))
            buffer.clear()
    require(not buffer and len(parts) >= 2 and parts[0] == input_audio, 'fixture and real speech')
    right = track(stereo[1::2].tobytes(), parts)
    return dict(wave=envelope, frames=frames, sample_rate=24000, left=left, right=right,
                before_publication=receipt['before_download'], business=observed,
                **({'fork_state':inspect_fork_state(out,receipt)} if plan.get('verify_fork_state') else {}))


class Stage(base.Stage):
    limits = LIMITS

    def extra_model_args(self):
        return live.Stage.extra_model_args(self) + ['--live-recordings']

    def business(self):
        plan, clip = self.plan, voice.retained_query()
        require(clip == plan['retained_query'], 'verified authorized query')
        require(all(sha(p)==h for p,h in plan['tts_assets'].items()), 'TTS identity')
        self.snapshot('before')
        for key in ('websocket_sessions','fixture_plays','audio_inputs','responses','downloads'):
            self.budget(key, self.limits[key])
        args = dict(base_url=f"https://127.0.0.1:{plan['public_port']}/v1",
            api_key=self.keys['alpha'], other_key=self.keys['beta'],
            ca_file=plan['tls']['certificate'], expected_input=voice.QUERY,
            clip=clip['path'], clip_sha256=clip['sha256'],
            record_file=str(ROOT/plan['record']['path']), record_sha256=plan['record']['sha256'],
            expected_version=plan['record']['version'], journal_file=str(self.out/'events.jsonl'),
            recording=str(self.out/'recording.wav'))
        env = {k:v for k,v in os.environ.items() if k not in
               ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD','NODE_TLS_REJECT_UNAUTHORIZED')}
        env.update(Q4T_NODE_SDK_DIR=plan['sdk_directory'], NODE_EXTRA_CA_CERTS=args['ca_file'])
        with (self.out/'sdk.json').open('xb') as stdout, (self.out/'sdk.stderr').open('xb') as stderr:
            actor = subprocess.run(['node',str(ROOT/'tools/serve_live/fixture_sdk.mjs')],
                input=json.dumps(args).encode(), stdout=stdout, stderr=stderr, env=env, timeout=320)
        self.same_model()
        self.snapshot('live-drained')
        self.core('core-after')
        self.snapshot('final-drained')
        require(actor.returncode==0, 'actual SDK actor failed; original evidence retained')
        save(self.out/'observation.json', observe(self.out))


def finalize(out):
    result, plan = read(out/'result.json'), read(out/'plan.json')
    require(result['status']=='PASS' and result['seconds'] <= LIMITS['seconds'] and
            all(result['counts'][k]==v for k,v in LIMITS.items() if k not in ('controls','seconds')),
            'complete bounded recording run')
    env = qualify.environment(read(out/'lease/exit.json'),
        [json.loads(line) for line in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
        [read(out/'cleanup.json')], expected_starts=1)
    verdict = observe(out)
    require(verdict == read(out/'observation.json'), 'independent recording replay')
    require(all(sha(ROOT/n)==h for n,h in plan['sources'].items()), 'qualified source identities')
    save(out/'acceptance.json', dict(environment=env, observation=verdict,
         seconds=result['seconds'], default_unchanged=result['default_unchanged'], core='42'))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run','finalize'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--qualification',type=Path)
    args=parser.parse_args()
    out=args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence path required')
    if args.action=='prepare':
        prepare(out,args.qualification)
    elif args.action=='run':
        plan=read(out/'plan.json')
        require(plan['schema']==SCHEMA,'recording stage identity')
        with base.lifecycle.child_subreaper():
            Stage(plan).run()
    else:
        finalize(out)
