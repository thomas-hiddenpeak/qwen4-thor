"""One-lease native speech output, official WSS, playback and quota business."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_tts.voice_service_acceptance import multipart
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_realtime import speech_checks as checks
from tools.serve_realtime.history_acceptance import settlement
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_service.package import require, save, sha

WORK = ROOT / '.q4t-work/serve-realtime-audio-20261120'
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=2,
              response_creates=9, chats=2, transcriptions=3, controls=2000,
              seconds=1200)
CONTRACT = dict(actual_speech=True, waveform_retrieval=True,
    all_three_formats=True, fractional_speed=.75, playback_cancel=True,
    single_rpm=True, no_model_restart=True, default_unchanged=True,
    guardian_recovery=False, master_complete=False)


def check_plan(plan):
    require(plan['schema'] == 'q4t-realtime-speech-business-v1' and
            plan['limits'] == LIMITS and plan['contract'] == CONTRACT,
            'frozen speech scope drift')
    require(plan['record']['path'] == str(ROOT / 'docs/STATUS.md') and
            re.fullmatch(r'rev[0-9]+', plan['record']['version']),
            'actual project source required')


def tts_health(config, resident):
    health = config['native_tts_health']
    require(health['state'] == ('ready' if resident else 'metadata_ready') and
            health['compute_failures'] == 0 and health['reason'] == 'none' and
            health['core_readiness_dependency'] is False and
            (health['device_bytes'] > 0 if resident else health['device_bytes'] == 0),
            'TTS health must match actual auxiliary family residency')


def check_receipt(value, mode, record):
    labels = (['tool', 'tool_audio', 'pcm', 'mulaw', 'alaw', 'cancel',
               'after_cancel'] if mode == 'main' else
              ['quota_audio', 'quota_refused'])
    states = (['completed'] * 5 + ['cancelled', 'completed'] if mode == 'main'
              else ['completed', 'failed'])
    require(value['status'] == 'PASS' and value['mode'] == mode and
            value['transport'] == 'official-openai-wss' and
            value['connections'] == 1 and value['close_code'] == 1000 and
            value['automatic_replay'] is False and
            value['response_creates'] == len(labels), 'actual SDK boundary')
    rows = value['responses']
    require([v['label'] for v in rows] == labels and
            [v['response']['status'] for v in rows] == states,
            'speech response coverage')
    responses = [v['response'] for v in rows]
    events = value['events']
    require([e['response'] for e in events if e['type'] == 'response.done'] ==
            responses and len({r['id'] for r in responses}) == len(responses),
            'missing/duplicate/changed terminal')
    require(len({e['event_id'] for e in events}) == len(events),
            'replayed event identity')
    retrieved = {v['label']: v['item'] for v in value['retrievals']}
    require(len(retrieved) == len(value['retrievals']), 'repeated retrieval label')
    audio = {}
    total = 0
    for label, response in zip(labels, responses):
        if response['status'] == 'failed':
            require(response['status_details']['error']['code'] ==
                    'client_rpm_exhausted', 'actual RPM refusal')
            continue
        total += checks.usage(response)
        if response['status'] == 'completed' and label not in ('tool', 'after_cancel'):
            raw, evidence = checks.audio(events, response, retrieved[label])
            audio[label] = (raw, evidence)
    if mode == 'main':
        require(value['tool_record'] == dict(record_key='release',
            version=record['version'], source='docs/STATUS.md',
            source_sha256=record['sha256']), 'real tool provenance')
        calls = responses[0]['output']
        require(len(calls) == 1 and calls[0]['type'] == 'function_call' and
                calls[0]['name'] == 'lookup_project_record' and
                json.loads(calls[0]['arguments']) == {'record_key': 'release'},
                'actual allowlisted tool call')
        for label, fmt in [('pcm', 'audio/pcm'), ('mulaw', 'audio/pcmu'),
                           ('alaw', 'audio/pcma')]:
            require(audio[label][1]['format'] == fmt, 'requested wire format')
        checks.retained_prefix(retrieved['pcm_truncated'], audio['pcm'][0],
                               'audio/pcm', 100)
        actions = value['checks']
        locked = [v for v in actions if v['type'] == 'locked_voice']
        deleted = [v for v in actions if v['type'] == 'deleted']
        active = [v for v in actions if v['type'] == 'active_truncate']
        require(len(locked) == len(deleted) == len(active) == 1 and
                locked[0]['error']['code'] == 'realtime_voice_locked' and
                deleted[0]['error']['code'] == 'realtime_item_not_found' and
                deleted[0]['item_id'] == retrieved['pcm']['id'],
                'locked voice/deleted history protection')
        require([v['speed'] for v in actions if v['type'] == 'speed'] ==
                [1, 1, .75], 'fractional effective speed')
        cancelled = responses[5]
        action = active[0]
        require(action['response_id'] == cancelled['id'] and
                action['milliseconds'] == 20 and
                retrieved['cancel']['id'] == action['item_id'],
                'active cancellation identity')
        at = next(i for i, e in enumerate(events) if
                  e['event_id'] == action['event_id'])
        chunks = [e for e in events if e['type'] == 'response.output_audio.delta'
                  and e['response_id'] == cancelled['id']]
        require(chunks and not any(e['type'] == 'response.output_audio.delta' and
                e['response_id'] == cancelled['id'] for e in events[at + 1:]),
                'audio emitted after truncation acknowledgement')
        checks.retained_prefix(retrieved['cancel'],
            b''.join(checks.decode64(e['delta']) for e in chunks), 'audio/pcm', 20)
        require(cancelled['output'][0]['content'][0]['transcript'] == '',
                'cancelled final transcript revived')
        tool_deltas = [e for e in events if e['type'] ==
                       'response.function_call_arguments.delta' and
                       e['response_id'] == responses[0]['id']]
        require(tool_deltas and all(e['item_id'] == calls[0]['id'] for e in tool_deltas)
                and ''.join(e['delta'] for e in tool_deltas) == calls[0]['arguments'],
                'tool stream does not match actual terminal')
        expected_quality = [(label, audio[label][1]['transcript'].strip(),
                             record['version'] if label == 'tool_audio' else checks.PHRASE)
                            for label in ('tool_audio', 'pcm', 'mulaw', 'alaw')]
        expected_quality.append(('after_cancel', ''.join(p['text'] for item in
            responses[6]['output'] for p in item.get('content', []) if
            p['type'] == 'output_text').strip(), '42'))
    else:
        expected_quality = [('quota_audio', audio['quota_audio'][1]['transcript'].strip(),
                             checks.PHRASE)]
    require(value['quality'] == [dict(label=label, actual=actual, expected=expected,
        status='PASS' if actual == expected else 'FAIL')
        for label, actual, expected in expected_quality],
        'quality must be derived from original answers, including failures')
    return total, audio


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed source before freezing')
    build = WORK / 'runtime-build-01'
    built = json.loads((build / 'result.json').read_text())
    require(built['status'] == 'BUILD_PASS' and built['warnings'] == 0,
            'zero-warning runtime build required')
    for name, digest in json.loads((build / 'plan.json').read_text())['sources'].items():
        require(sha(ROOT / name) == digest, 'built source drift')
    binary = args.binary.resolve(strict=True)
    require(sha(binary) == built['binary_sha256'], 'built binary drift')
    cpu = WORK / 'business-cpu-02'
    require(json.loads((cpu / 'result.json').read_text())['status'] == 'PASS',
            'business checks CPU not qualified')
    previous = base.WORK / 'transcription-business-04/run'
    require(json.loads((previous / 'result.json').read_text())['status'] == 'PASS',
            'reused assets not qualified')
    plan = json.loads((previous / 'plan.json').read_text())
    out = args.output.resolve()
    require(out.is_relative_to(WORK), 'private phase output required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    policy = json.loads((previous / 'client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    for client in policy['clients']:
        client.update(requests_per_minute=1 if client['id'] == 'beta' else 30,
                      tokens_per_minute=60000, max_output_tokens=3457)
    save(out / 'client-policy.json', policy)
    (out / 'client-policy.json').chmod(0o600)
    tls = certificate(out / 'credentials')
    manifest = ROOT / '.q4t-work/serve-refusal-interference-20261120/service-package-01/bundle/engine/manifest.json'
    tts = json.loads(manifest.read_text())['settings']['native_tts']
    sdk = base.WORK / 'profile-sdk-01'
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include',
        'cmake', 'CMakeLists.txt', 'tools/serve_realtime', 'tools/native_tts',
        'tools/native_asr', 'tools/serve_grammar_reap', 'tools/serve_recovery',
        'tools/serve_service', 'tools/evalscope', 'tools/release'],
        cwd=ROOT, text=True).splitlines()
    record = ROOT / 'docs/STATUS.md'
    version = re.search(r'默认\s*rev([0-9]+)', record.read_text())
    require(version is not None, 'current default record')
    protected = [ROOT / n for n in ('build/q4t', 'build/q4t.release.json',
        'build/service/manifest.json', 'build/service.release.json',
        'build/service-native-state/ledger.json')]
    plan.update(schema='q4t-realtime-speech-business-v1', limits=LIMITS,
        contract=CONTRACT, output=str(out), port=args.port,
        operations_port=args.port + 1, public_port=args.port + 2,
        binary=str(binary), binary_sha256=sha(binary), native_tts=tts,
        tts_assets={str(p): sha(p) for p in sorted(Path(tts['directory']).rglob('*'))
                    if p.is_file()}, sdk_directory=str(sdk),
        sdk_files={str(p): sha(p) for p in sorted((sdk / 'node_modules').rglob('*'))
                   if p.is_file()}, sources={n: sha(ROOT / n) for n in sources},
        protected={str(p): sha(p) for p in protected},
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(), client_policy_sha256=sha(out / 'client-policy.json'),
        tls=tls, tls_files={tls[n]: sha(tls[n]) for n in ('certificate', 'key')},
        reused_core_before={}, qualified_files={str(p): sha(p) for directory in
            (build, cpu) for p in directory.iterdir() if p.is_file()},
        record=dict(path=str(record), sha256=sha(record), version='rev' + version.group(1)),
        scope=__doc__)
    check_plan(plan)
    save(out / 'plan.json', plan)
    print(json.dumps(dict(plan=str(out / 'plan.json'), limits=LIMITS)))


class Stage(base.Stage):
    limits = LIMITS

    def __init__(self, plan):
        check_plan(plan)
        super().__init__(plan)
        for name, digest in {**plan['qualified_files'], **plan['tts_assets']}.items():
            require(sha(name) == digest, 'qualification or TTS asset drift')
        require(sha(plan['record']['path']) == plan['record']['sha256'],
                'actual release record drift')

    def extra_model_args(self):
        tts = self.plan['native_tts']
        args = ['--tts-model-dir', tts['directory']]
        for key, value in tts['limits'].items():
            args.extend(['--tts-' + key.replace('_', '-'), str(value)])
        return args

    def budget(self, kind, amount=1):
        require(shutil.disk_usage(self.out).free >= 128 << 20, 'disk headroom')
        require(sum(p.stat().st_size for p in self.out.rglob('*') if p.is_file())
                <= 48 << 20, 'artifact bound')
        super().budget(kind, amount)

    def speech(self, mode, client):
        self.budget('websocket_sessions')
        self.budget('response_creates', 7 if mode == 'main' else 2)
        record = self.plan['record']
        args = dict(base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",
            api_key=self.keys[client], ca_file=self.plan['tls']['certificate'],
            mode=mode, record_file=record['path'], record_sha256=record['sha256'],
            expected_version=record['version'])
        env = {k: v for k, v in os.environ.items() if k not in
               ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR'] = self.plan['sdk_directory']
        run = subprocess.run(['node', str(ROOT / 'tools/serve_realtime/speech_sdk.mjs')],
            input=json.dumps(args), text=True, capture_output=True, env=env, timeout=370)
        (self.out / (mode + '.sdk.json')).write_text(run.stdout)
        (self.out / (mode + '.stderr')).write_text(run.stderr)
        require(run.returncode == 0 and len(run.stdout.encode()) <= 16 << 20,
                'actual speech SDK failed: ' + mode)
        value = json.loads(run.stdout)
        tokens, audio = check_receipt(value, mode, record)
        self.same_model()
        return tokens, audio, value['quality']

    def transcribe(self, label, raw, evidence):
        self.budget('transcriptions')
        wav, physical = checks.waveform(raw, evidence['format'])
        (self.out / (label + '.wav')).write_bytes(wav)
        # No expected sentence, prompt or language is given to ASR.
        body, mime = multipart(dict(model='qwen3-asr-0.6b', response_format='json'),
                               'file', wav)
        conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=120)
        try:
            conn.request('POST', '/v1/audio/transcriptions', body=body,
                headers={'Content-Type': mime, 'X-Q4T-Client-Id': 'gamma',
                         'X-Q4T-Gateway-Token': self.token})
            reply = conn.getresponse()
            wire = reply.read((1 << 20) + 1)
            (self.out / (label + '.asr.wire')).write_bytes(wire)
            require(reply.status == 200 and len(wire) <= 1 << 20,
                    'independent ASR HTTP failed')
        finally:
            conn.close()
        value = json.loads(wire)
        normalize = lambda s: re.sub(r'[^a-z0-9]', '', s.lower())
        quality = dict(label=label, expected=checks.PHRASE, actual=value['text'],
            status='PASS' if normalize(value['text']) == normalize(checks.PHRASE) else 'FAIL')
        save(self.out / (label + '.evidence.json'),
             dict(wire=evidence, physical=physical, quality=quality))
        self.same_model()
        return quality

    def business(self):
        self.snapshot('before')
        self.core('core-before')
        tokens, audio, quality = self.speech('main', 'alpha')
        settlement(self.snapshot('main-drained'), 'alpha', tokens, 7)
        tokens, _, second_quality = self.speech('quota', 'beta')
        quality.extend(second_quality)
        state = self.snapshot('quota-drained')
        settlement(state, 'beta', tokens, 1)
        require(state['q4t_client_rate_rejected_total{client="beta",kind="rpm"}']
                == 1, 'actual quota metric')
        status, raw = self.http('/v1/config', label='speech-config')
        require(status == 200, 'speech config')
        tts_health(json.loads(raw), True)
        for label in ('pcm', 'mulaw', 'alaw'):
            quality.append(self.transcribe(label, *audio[label]))
        self.core('core-after')
        self.snapshot('final-drained')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200, 'final config')
        tts_health(json.loads(raw), False)
        save(self.out / 'quality.json', quality)
        require(all(v['status'] == 'PASS' for v in quality),
                'quality failure retained separately from protocol evidence')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--binary', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--port', type=int, default=18458)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Stage(json.loads(args.plan.read_text())).run()
