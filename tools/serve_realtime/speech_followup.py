"""Missing speech content only, plus fixed-seed observations of original prompts.

Original failed outputs are immutable. Successful waveform/cancel/quota checks
are reused; this stage neither repeats them nor changes API sampling defaults.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_realtime import speech_acceptance as first
from tools.serve_realtime import speech_checks as checks
from tools.serve_realtime.history_acceptance import settlement
from tools.serve_service.package import require, save, sha

WORK = first.WORK
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=1,
              response_creates=5, chats=2, transcriptions=3, controls=2000,
              seconds=600)
TASK = 'Repeat exactly without adding words: ' + checks.PHRASE
TOOL_TASK = 'What is the version in the preceding tool result? Reply only with the version.'


def comparison_requests(observed):
    selected = {v['label']: v for v in observed}
    require(set(selected) == {a + '-' + b for a in ('tool_audio', 'pcm')
                             for b in ('audio', 'text', 'new_user')},
            'exact original prompt reconstructions required')
    rows = {}
    for label in ('tool_audio', 'pcm'):
        a, b = selected[label + '-audio'], selected[label + '-text']
        require(a['native_prompt'] == a['measured_prompt'] == a['executed_prompt']
                == b['native_prompt'] == b['measured_prompt'] == b['executed_prompt']
                and a['generation_request'] == b['generation_request'],
                'audio changed the actual original text prompt')
        rows[label] = dict(model='qwen3.8-flash-next', prompt=a['native_prompt'],
                           max_tokens=64, temperature=0, seed=0)
    return rows


def check_receipt(value, record):
    labels = ['tool', 'tool_audio', 'pcm', 'mulaw', 'alaw']
    require(value['status'] == 'PASS' and value['mode'] == 'content' and
            value['transport'] == 'official-openai-wss' and value['connections'] == 1
            and value['close_code'] == 1000 and value['response_creates'] == 5 and
            value['automatic_replay'] is False, 'bounded content SDK')
    require([v['label'] for v in value['responses']] == labels and
            all(v['response']['status'] == 'completed' for v in value['responses']),
            'content response outcomes')
    rows = [v['response'] for v in value['responses']]
    events = value['events']
    require([e['response'] for e in events if e['type'] == 'response.done'] == rows
            and len({e['event_id'] for e in events}) == len(events) and
            len({r['id'] for r in rows}) == len(rows), 'replayed/missing terminal')
    require(value['tool_record'] == dict(record_key='release',
            version=record['version'], source='docs/STATUS.md',
            source_sha256=record['sha256']), 'actual tool provenance')
    call = rows[0]['output']
    require(len(call) == 1 and call[0]['type'] == 'function_call' and
            call[0]['name'] == 'lookup_project_record' and
            json.loads(call[0]['arguments']) == {'record_key': 'release'},
            'actual allowlisted tool call')
    deltas = [e for e in events if e['type'] == 'response.function_call_arguments.delta'
              and e['response_id'] == rows[0]['id']]
    require(deltas and all(e['item_id'] == call[0]['id'] for e in deltas) and
            ''.join(e['delta'] for e in deltas) == call[0]['arguments'], 'actual tool stream')
    retrieved = {v['label']: v['item'] for v in value['retrievals']}
    require(set(retrieved) == set(labels[1:]) and len(value['retrievals']) == 4,
            'exact audio retrievals')
    audio, expected_quality = {}, []
    last_end = -1
    for label, row in zip(labels[1:], rows[1:]):
        created = next(i for i, e in enumerate(events) if e['type'] ==
                       'response.created' and e['response']['id'] == row['id'])
        user_id = 'user_tool_answer' if label == 'tool_audio' else 'user_' + label
        user = [e['item'] for e in events[last_end + 1:created] if
                e['type'] == 'conversation.item.done' and e['item'].get('id') == user_id]
        require(len(user) == 1 and user[0]['role'] == 'user' and
                user[0]['content'] == [dict(type='input_text',
                    text=TOOL_TASK if label == 'tool_audio' else TASK)],
                'explicit new user turn must precede this response')
        raw, evidence = checks.audio(events, row, retrieved[label])
        require(evidence['format'] == {'tool_audio': 'audio/pcm', 'pcm': 'audio/pcm',
                'mulaw': 'audio/pcmu', 'alaw': 'audio/pcma'}[label], 'real requested format')
        audio[label] = raw, evidence
        expected = record['version'] if label == 'tool_audio' else checks.PHRASE
        actual = evidence['transcript'].strip()
        expected_quality.append(dict(label=label, actual=actual, expected=expected,
            status='PASS' if actual == expected else 'FAIL'))
        last_end = next(i for i, e in enumerate(events) if e['type'] ==
                        'response.done' and e['response']['id'] == row['id'])
    require(value['quality'] == expected_quality and
            [v['speed'] for v in value['checks'] if v['type'] == 'speed'] == [1, 1, .75],
            'quality or actual fractional setting differs')
    return sum(checks.usage(r) for r in rows), audio


def check_plan(plan):
    require(plan['schema'] == 'q4t-realtime-speech-content-v1' and plan['limits'] == LIMITS
            and plan['no_replay_of_passed_protocol'] is True and
            plan['sampling_defaults_changed'] is False,
            'missing-content scope drift')
    require(plan['comparisons'] == comparison_requests(plan['input_review']),
            'original fixed-seed comparison changed')


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed follow-up first')
    previous = WORK / 'business-01/run'
    plan = json.loads((previous / 'plan.json').read_text())
    require(json.loads((previous / 'result.json').read_text())['status'] == 'FAIL',
            'original quality failure must be retained')
    review = WORK / 'input-review-01'
    cpu = WORK / 'content-cpu-01'
    require(json.loads((review / 'result.json').read_text())['status'] ==
            'PASS_OFFLINE_INPUT_IDENTITY' and
            json.loads((cpu / 'result.json').read_text())['status'] == 'PASS',
            'offline review and CPU required')
    out = args.output.resolve()
    require(out.is_relative_to(WORK), 'private output required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    policy = json.loads((previous / 'client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    save(out / 'client-policy.json', policy)
    (out / 'client-policy.json').chmod(0o600)
    tls = first.certificate(out / 'credentials')
    observed = json.loads((review / 'observed.json').read_text())
    record = ROOT / 'docs/STATUS.md'
    version = re.search(r'默认\s*rev([0-9]+)', record.read_text()).group(1)
    plan.update(schema='q4t-realtime-speech-content-v1', output=str(out), limits=LIMITS,
        no_replay_of_passed_protocol=True, sampling_defaults_changed=False,
        input_review=observed, comparisons=comparison_requests(observed),
        client_policy_sha256=sha(out / 'client-policy.json'), tls=tls,
        tls_files={tls[n]: sha(tls[n]) for n in ('certificate', 'key')},
        record=dict(path=str(record), sha256=sha(record), version='rev' + version),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,text=True).strip(),
        qualified_files={str(p):sha(p) for folder in (review,cpu,previous)
            for p in folder.iterdir() if p.is_file() and p.name not in
            ('probe','client-policy.json')}, scope=__doc__)
    for n in plan['sources']: plan['sources'][n] = sha(ROOT / n)
    for n in ('tools/serve_realtime/speech_followup.py','tools/serve_realtime/test_speech_followup.py'):
        plan['sources'][n] = sha(ROOT / n)
    check_plan(plan)
    save(out / 'plan.json', plan)
    print(json.dumps(dict(plan=str(out / 'plan.json'), limits=LIMITS)))


class Stage(first.Stage):
    limits = LIMITS

    def __init__(self, plan):
        check_plan(plan)
        first.base.Stage.__init__(self, plan)
        for n, h in {**plan['qualified_files'], **plan['tts_assets']}.items():
            require(sha(n) == h, 'qualified content evidence drift')
        require(sha(plan['record']['path']) == plan['record']['sha256'], 'project record drift')

    def content(self):
        self.budget('websocket_sessions')
        self.budget('response_creates', 5)
        record = self.plan['record']
        args = dict(base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",
            api_key=self.keys['alpha'], ca_file=self.plan['tls']['certificate'],
            mode='content', record_file=record['path'], record_sha256=record['sha256'],
            expected_version=record['version'])
        env = {k: v for k, v in os.environ.items() if k not in
               ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR'] = self.plan['sdk_directory']
        run = subprocess.run(['node', str(ROOT / 'tools/serve_realtime/speech_sdk.mjs')],
            input=json.dumps(args), text=True, capture_output=True, env=env, timeout=370)
        (self.out / 'content.sdk.json').write_text(run.stdout)
        (self.out / 'content.stderr').write_text(run.stderr)
        require(run.returncode == 0 and len(run.stdout.encode()) <= 16 << 20,
                'actual new-user speech SDK failed')
        value = json.loads(run.stdout)
        total, audio = check_receipt(value, record)
        self.same_model()
        return total, audio, value['quality']

    def business(self):
        observations = {}
        for label, request in self.plan['comparisons'].items():
            self.budget('chats')
            conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=120)
            try:
                conn.request('POST', '/v1/completions', json.dumps(request), headers={
                    'Content-Type': 'application/json', 'X-Q4T-Client-Id': 'gamma',
                    'X-Q4T-Gateway-Token': self.token})
                reply = conn.getresponse()
                status, raw = reply.status, reply.read((1 << 20) + 1)
                require(len(raw) <= 1 << 20, 'diagnostic response bound')
                (self.out / ('fixed-' + label + '.wire')).write_bytes(raw)
            finally:
                conn.close()
            require(status == 200, 'fixed-seed original input HTTP failure')
            value = json.loads(raw)
            require(value['choices'][0]['finish_reason'] in ('stop', 'length'),
                    'diagnostic request failed')
            observations[label] = dict(request=request, response=value,
                scope='Fixed-seed observation only; original sampled failure is unchanged')
            save(self.out / 'comparisons.json', observations)
            self.same_model()
        total, audio, quality = self.content()
        settlement(self.snapshot('content-drained'), 'alpha', total, 5)
        status, raw = self.http('/v1/config', label='speech-config')
        require(status == 200, 'speech health')
        first.tts_health(json.loads(raw), True)
        for label in ('pcm', 'mulaw', 'alaw'):
            quality.append(self.transcribe(label, *audio[label]))
        self.snapshot('final-drained')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200, 'final health')
        first.tts_health(json.loads(raw), False)
        save(self.out / 'quality.json', quality)
        require(all(v['status'] == 'PASS' for v in quality),
                'new-user content quality failed; retain original outputs')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--output', type=Path, required=True)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare': prepare(args)
    else:
        with first.base.lifecycle.child_subreaper():
            Stage(json.loads(args.plan.read_text())).run()
