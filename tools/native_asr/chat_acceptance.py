"""Bounded real Chat speech input, SDKs, quota, Batch and auxiliary isolation."""
import argparse
import base64
import copy
from dataclasses import asdict
import http.client
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import wave

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_asr import transcription_acceptance as asr
from tools.examples.chat_client import (read_json, read_sse, prepare_tool_calls,
    append_tool_results)
from tools.examples.project_records import ProjectRecords, VALIDATORS
from tools.serve_service.package import read, require, save, sha

WORK = ROOT / '.q4t-work/serve-chat-audio-20261118'
FIXTURES = WORK / 'fixtures-02'
LIMITS = dict(starts=1, chats=13, controls=2000, seconds=900)
CONTINUATION_LIMITS = dict(LIMITS, chats=11)
HEADERS = {'X-Q4T-Client-Id': 'chat-audio', 'X-Q4T-Gateway-Token': 'c' * 64}


def fixtures():
    FIXTURES.mkdir(mode=0o700)
    plugins = FIXTURES / 'plugins'
    plugins.mkdir()
    system = Path('/usr/lib/aarch64-linux-gnu/gstreamer-1.0')
    names = ('coreelements', 'wavparse', 'audioconvert', 'lame', 'audioparsers')
    for name in names:
        source = system / ('libgst' + name + '.so')
        require(source.is_file(), 'CPU audio fixture plugin missing')
        (plugins / source.name).symlink_to(source)
    source = asr.WORK / 'real-audio-01/asr_en.wav'
    command = ['gst-launch-1.0', '-q', '-e', 'filesrc', 'location=' + str(source),
        '!', 'wavparse', '!', 'audioconvert', '!', 'lamemp3enc', '!',
        'mpegaudioparse', '!', 'filesink', 'location=' + str(FIXTURES/'en.mp3')]
    env = dict(os.environ, CUDA_VISIBLE_DEVICES='', GST_PLUGIN_SYSTEM_PATH_1_0='',
        GST_PLUGIN_PATH_1_0=str(plugins), GST_REGISTRY_FORK='no',
        GST_REGISTRY=str(FIXTURES/'registry.bin'), XDG_CACHE_HOME=str(FIXTURES/'cache'))
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    save(FIXTURES/'encoding.json', dict(command=command, returncode=result.returncode,
        stdout=result.stdout, stderr=result.stderr, source_sha256=sha(source),
        cpu_plugins={p.name: sha(p) for p in plugins.iterdir()}))
    require(result.returncode == 0, 'CPU MP3 fixture encoding failed')
    for label, seconds in (('silence', 1), ('overlong', 31)):
        with wave.open(str(FIXTURES/(label+'.wav')), 'wb') as target:
            target.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            target.writeframes(bytes(seconds * 16000 * 2))
    cancel_source = asr.WORK / 'real-audio-01/asr_zh.wav'
    with wave.open(str(cancel_source), 'rb') as original:
        require(original.getframerate() == 16000 and original.getsampwidth() == 2
                and original.getnchannels() == 1, 'real source format drift')
        pcm = original.readframes(original.getnframes())
    with wave.open(str(FIXTURES/'cancel.wav'), 'wb') as target:
        target.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        target.writeframes((pcm * (960000 // len(pcm) + 1))[:960000])
    save(FIXTURES/'provenance.json', dict(gpu=0, fixture_generation_only=True,
        source=str(source), source_sha256=sha(source),
        cancellation_source=str(cancel_source), cancellation_source_sha256=sha(cancel_source),
        files={p.name: sha(p) for p in FIXTURES.iterdir()
               if p.is_file() and p.suffix in ('.wav', '.mp3')}))


def clip_paths():
    return dict(zh=asr.WORK/'real-audio-01/asr_zh.wav', en=FIXTURES/'en.mp3',
        silence=FIXTURES/'silence.wav', overlong=FIXTURES/'overlong.wav',
        cancel=FIXTURES/'cancel.wav')


def audio(label):
    path = clip_paths()[label]
    return dict(type='input_audio', input_audio=dict(format=path.suffix[1:],
        data='fixture:'+label))


def materialize(body):
    """Frozen clip identity -> wire bytes, only at typed user audio parts."""
    result = copy.deepcopy(body)
    for message in result.get('messages', []):
        parts = message.get('content')
        if not isinstance(parts, list): continue
        for part in parts:
            if part.get('type') != 'input_audio': continue
            source = part['input_audio']
            data = source['data']
            if not data.startswith('fixture:'): continue
            label = data[len('fixture:'):]
            require(label in clip_paths(), 'unknown frozen audio fixture')
            path = clip_paths()[label]
            require(source['format'] == path.suffix[1:], 'fixture format changed')
            source['data'] = base64.b64encode(path.read_bytes()).decode()
    return result


def request(text, clips=(), **options):
    result = dict(model='qwen3.8-flash-next', enable_thinking=False, temperature=0,
        max_completion_tokens=128, modalities=['text'], messages=[dict(role='user',
        content=[dict(type='text', text=text), *(audio(label) for label in clips)])])
    result.update(options)
    return result


def schema(properties):
    return dict(type='json_schema', json_schema=dict(name='audio_answer', strict=True,
        schema=asr.base.closed(properties)))


def requests():
    zh = request('录音中被描述为几乎停滞的事情是什么？只回答对应的两个汉字，不要解释。',
        ['zh'], n=2, store=True, max_completion_tokens=32)
    en = request('According to the recording, what activity brought him more success? '
        'Return a JSON object with activity as a brief English phrase.', ['en'],
        stream=True, stream_options=dict(include_usage=True),
        response_format=schema(dict(activity=dict(type='string'))))
    tool = copy.deepcopy(asr.base.requests()['tool'])
    for key in ('request_id', 'cancel_token', 'response_format'):
        tool.pop(key, None)
    tool.update(stream=False, max_completion_tokens=128)
    tool.pop('stream_options')
    tool['messages'][0]['content'] = [dict(type='text', text=
        'Read the first 16 lines of docs/MODEL.md using read_project_record. '
        'After the tool returns, answer with the document title, the two-Chinese-character '
        'subject of the first recording, and the activity bringing more success '
        'in the second recording.'), audio('zh'), audio('en')]
    bad = copy.deepcopy(zh)
    bad.update(n=1, store=False)
    bad['messages'][0]['content'][1]['input_audio']['data'] = 'YR=='
    return dict(zh=zh, en=en, tool=tool, invalid=bad,
        silence=request('Reply only OK.', ['silence'], max_completion_tokens=16),
        overlong=request('Transcribe this.', ['overlong']),
        cancel=request('Explain this recording.', ['cancel']),
        core=asr.requests()['chat'], batch=dict(zh, n=1, store=False))


def usage(value):
    u = value['usage']
    require(all(type(u[k]) is int and u[k] > 0 for k in
        ('prompt_tokens', 'completion_tokens', 'total_tokens')), 'missing actual Chat usage')
    require(u['prompt_tokens'] + u['completion_tokens'] == u['total_tokens'], 'usage sum')
    prep = u['q4t_audio_preparation']
    require(all(type(prep[k]) is int and prep[k] > 0 for k in
        ('input_tokens', 'output_tokens', 'audio_tokens')), 'missing actual ASR usage')
    require(prep['audio_tokens'] < prep['input_tokens'] < u['prompt_tokens'] and
        prep['output_tokens'] < u['completion_tokens'], 'preparation replaces answer usage')
    require(u['prompt_tokens_details']['audio_tokens'] == prep['audio_tokens'],
        'encoder positions mismatch')
    # Python's typed SDK may add nullable optional fields absent on the wire.
    return {k: u[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens',
        'q4t_audio_preparation', 'prompt_tokens_details')}


def reuse_completed(source):
    source = Path(source).resolve(strict=True)
    require(source.is_relative_to(WORK), 'reused evidence outside workdir')
    plan, result = read(source/'plan.json'), read(source/'result.json')
    require(result['status'] == 'FAILED' and result['counts']['starts'] == 1 and
        result['counts']['chats'] == 3 and result['default_unchanged'] and
        'real Chinese speech did not reach both answers' in result['error'],
        'unexpected Chat audio predecessor')
    require(plan['requests'] == requests(), 'frozen business requests changed')
    require(sha(WORK/'runtime-build-05/q4t-e740-retained') == plan['binary_sha256'],
        'failed candidate not retained')
    for name, digest in plan['sources'].items():
        if name.startswith(('src/audio/', 'include/q4t/audio/', 'src/server/asr_',
                            'src/server/chat_asr_')):
            require(sha(ROOT/name) == digest, 'reused ASR implementation changed')
    require(read(source/'lease/exit.json')['status'] == 'COMMAND_FAILED',
        'environment abort cannot be reused')
    cleanup = read(source/'cleanup.json')
    require(cleanup['model_exit'] == 0 and not cleanup['remaining'] and
        cleanup['ports_released'], 'reused cleanup incomplete')
    require(read(source/'missing-asr.http.json')['status'] == 503 and
        read(source/'missing-asr.wire')['error']['code'] == 'asr_assets_invalid' and
        read(source/'healthy-during-asr-failure.http.json')['status'] == 200,
        'reused auxiliary isolation failed')
    core = read(source/'core-during-asr-failure.wire')['choices'][0]
    require(core['finish_reason'] == 'stop' and core['message']['content'].strip() == '42',
        'reused core business failed')
    names = ('plan.json', 'result.json', 'cleanup.json', 'lease/exit.json',
        'missing-asr.http.json', 'missing-asr.wire', 'healthy-during-asr-failure.http.json',
        'core-during-asr-failure.wire', 'preflight.wire')
    return dict(directory=str(source), original_status='FAILED_PRESERVED',
        accepted_requests=2, binary_sha256=plan['binary_sha256'],
        identities={name: sha(source/name) for name in names})


def prepare(args):
    reused = reuse_completed(args.continue_from) if args.continue_from else None
    limits = CONTINUATION_LIMITS if reused else LIMITS
    asr.base.prepare(args, rows=requests(), limits=limits, scope=__doc__)
    out = args.output.resolve()
    plan = read(out/'plan.json')
    plan['python_sdk'] = asr.python_identity()
    acquired = {row['name']: row for row in read(asr.WORK/'asset-acquisition-01.json')['files']}
    plan['asr_assets'] = {name: {k: acquired[name][k] for k in ('bytes', 'sha256')}
                          for name in asr.FILES}
    plan['asr_directory'] = str(asr.WORK/'assets-01')
    plan['clips'] = {k: dict(path=str(p), sha256=sha(p)) for k,p in clip_paths().items()}
    plan['expected'] = asr.EXPECTED
    for path in (ROOT/'tools/native_asr').glob('*'):
        if path.is_file(): plan['sources'][str(path.relative_to(ROOT))] = sha(path)
    plan['quality'] = dict(zh_subject='交易', en_activity_contains=['writ', 'other'],
        tool_document='actual docs/MODEL.md lines 1..16', silence='OK', core='42')
    plan['planned'] = dict(starts=1, requests=limits['chats'], batch_rows=1, cancellation=1,
        no_automatic_retry=True, no_overlap_or_performance_matrix=True)
    if reused: plan['chat_continuation'] = reused
    plan['derived_requests'] = ['Typed fixture markers are expanded from the fixed clip SHA256 immediately before HTTP; raw audio is not plan metadata.',
        'Tool follow-up appends actual call ID, actual project file result and fixed final JSON task.']
    plan['command'] = ['/usr/bin/python3', '-B', str(Path(__file__).resolve()),
        'run', '--plan', str(out/'plan.json')]
    save(out/'plan.json', plan)


class Stage(asr.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_CHAT_AUDIO_BUSINESS'

    def __init__(self, plan):
        if plan.get('chat_continuation'):
            self.limits = CONTINUATION_LIMITS
            self.success_status = 'PASS_CHAT_AUDIO_REMAINING_WITH_REUSED_ISOLATION'
            require(reuse_completed(plan['chat_continuation']['directory']) ==
                    plan['chat_continuation'], 'reused Chat evidence drift')
        super().__init__(plan)

    def headers(self, client='chat-audio'):
        return dict(HEADERS, **{'X-Q4T-Client-Id': client})

    def http(self, path, body=None, *, label=None, timeout=30, client='chat-audio',
             content_type='application/json'):
        if body is None: self.budget('controls')
        conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=timeout)
        headers = {} if path in ('/readyz', '/metrics', '/v1/config') else self.headers(client)
        if body is not None: headers['Content-Type'] = content_type
        raw_body = body if isinstance(body, bytes) else (
            None if body is None else json.dumps(body, ensure_ascii=False).encode())
        started = time.monotonic()
        try:
            conn.request('GET' if body is None else 'POST', path, raw_body, headers)
            response = conn.getresponse()
            raw = response.read(4*1024*1024+1)
            require(len(raw) <= 4*1024*1024, 'response capture bound')
            if label:
                (self.out/(label+'.wire')).write_bytes(raw)
                save(self.out/(label+'.http.json'), dict(status=response.status,
                    headers=dict(response.getheaders()), seconds=time.monotonic()-started))
            return response.status, raw
        finally: conn.close()

    def start(self):
        for port in (self.plan['port'], self.plan['operations_port']):
            with socket.socket() as sock: sock.bind(('127.0.0.1', port))
        assets = self.out/'asr-assets'
        assets.mkdir(mode=0o700)
        for name, record in self.plan['asr_assets'].items():
            source = Path(self.plan['asr_directory'])/name
            require(source.stat().st_size == record['bytes'] and sha(source) == record['sha256'],
                'ASR asset drift')
            withheld = name == 'model.safetensors' and not self.plan.get('chat_continuation')
            os.link(source, assets/(name+'.withheld' if withheld else name))
        clients = [dict(id=name, max_inflight=4, max_control_inflight=4,
            max_prompt_tokens=16384, max_output_tokens=512, max_body_bytes=4*1024*1024,
            requests_per_minute=rpm, tokens_per_minute=200000)
            for name,rpm in [('chat-audio', 32), ('one-rpm', 1)]]
        save(self.out/'client-policy.json', dict(version=3, revision=0, fair_queue=True,
            gateway_token=HEADERS['X-Q4T-Gateway-Token'], clients=clients))
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '128', '--max-inflight-requests', '8', '--max-queued-requests', '2',
            '--asr-model-dir', str(assets), '--asr-max-output-tokens', '128',
            '--object-store-dir', str(self.out/'objects'),
            '--client-policy-file', str(self.out/'client-policy.json')]
        save(self.out/'model-command.json', args)
        self.budget('starts')
        with (self.out/'model.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = asr.base.lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/'model-identity.json', dict(pid=self.model.pid, **self.model_identity))
        end = time.monotonic()+180
        while time.monotonic() < end:
            self.same_model()
            try:
                if self.http('/readyz', timeout=1)[0] == 200: break
            except (OSError, http.client.HTTPException): pass
            time.sleep(.1)
        else: raise TimeoutError('model readiness')
        for name in ('capabilities', 'config'):
            status, raw = self.http('/v1/'+name, label='initial-'+name)
            require(status == 200, name+' unavailable')
            save(self.out/(name+'.json'), json.loads(raw))
        caps = read(self.out/'capabilities.json')['transcriptions']
        require(caps['configured'] and caps['chat_input']['profile'] ==
            'q4t-chat-audio-input-v1', 'Chat audio capability missing')

    def json_chat(self, label, body, *, client='chat-audio', code=200):
        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', materialize(body), label=label,
            timeout=180, client=client)
        require(status == code, label+': '+raw[:512].decode(errors='replace'))
        self.drained(label+'-drain')
        return json.loads(raw)

    def sdk(self, label, body, *, python=False, client='chat-audio'):
        self.budget('chats')
        value = dict(base_url=f"http://127.0.0.1:{self.plan['port']}/v1",
            sdk_directory=self.plan['sdk']['sdk_directory'], request=materialize(body),
            headers=self.headers(client))
        command = ([self.plan['python_sdk']['python'], '-B', str(ROOT/'tools/native_asr/chat_sdk.py')]
            if python else ['node', str(ROOT/'tools/native_asr/chat_sdk.mjs')])
        env = {k:v for k,v in os.environ.items() if k not in (
            'NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD', 'HTTP_PROXY', 'HTTPS_PROXY',
            'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'NODE_USE_ENV_PROXY')}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run(command, input=json.dumps(value), capture_output=True,
            text=True, env=env, timeout=190)
        (self.out/(label+'.stderr')).write_text(result.stderr)
        require(result.stdout, 'SDK returned no record')
        value = json.loads(result.stdout)
        wire = base64.b64decode(value.pop('wire_base64'), validate=True)
        (self.out/(label+'.wire')).write_bytes(wire)
        save(self.out/(label+'.sdk.json'), value)
        require(result.returncode == 0 and value['ok'] and len(value['exchanges']) == 1,
            'SDK request failed or retried: '+label)
        self.drained(label+'-drain')
        return value['result'], wire

    def batch(self, body):
        self.budget('chats')  # The one original row; management is not a Chat.
        row = dict(custom_id='original-audio', method='POST', url='/v1/chat/completions', body=materialize(body))
        text = json.dumps(row, separators=(',', ':'))+'\n'
        wire = ('--b\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch'
            '\r\n--b\r\nContent-Disposition: form-data; name="file"; filename="input.jsonl"'
            '\r\n\r\n'+text+'\r\n--b--\r\n').encode()
        self.budget('controls')
        status, raw = self.http('/v1/files', wire, label='batch-upload',
            content_type='multipart/form-data; boundary=b')
        require(status == 200, 'Batch audio source upload failed')
        self.budget('controls')
        status, raw = self.http('/v1/batches', dict(input_file_id=json.loads(raw)['id'],
            endpoint=row['url'], completion_window='24h'), label='batch-create')
        require(status == 200, 'Batch audio create failed')
        job = json.loads(raw)
        end = time.monotonic()+90
        while time.monotonic() < end:
            status, raw = self.http('/v1/batches/'+job['id'])
            require(status == 200, 'Batch audio observation failed')
            job = json.loads(raw)
            if job['status'] in ('completed', 'failed', 'cancelled', 'expired'): break
            time.sleep(.1)
        save(self.out/'batch-terminal.json', job)
        require(job['status'] == 'completed' and job['request_counts']['completed'] == 1,
            'Batch audio did not complete exactly one original row')
        status, raw = self.http('/v1/files/'+job['output_file_id']+'/content', label='batch-result')
        require(status == 200, 'Batch audio output missing')
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        require(len(rows) == 1 and rows[0]['custom_id'] == row['custom_id'] and
            rows[0]['response']['status_code'] == 200, 'Batch audio row failed')
        response = rows[0]['response']['body']
        usage(response)
        require(response['choices'][0]['message']['content'].strip() == '交易',
            'Batch audio content differs')
        self.drained('batch-drain')

    def cancel(self, body):
        before = self.drained('cancel-before')
        self.budget('chats')
        connection = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=30)
        try:
            connection.request('POST', '/v1/chat/completions', json.dumps(materialize(body)),
                dict(self.headers(), **{'Content-Type': 'application/json'}))
            end = time.monotonic()+30
            while time.monotonic() < end:
                status, raw = self.http('/metrics', timeout=5)
                require(status == 200, 'cancellation observation unavailable')
                current = asr.base.lifecycle.parse_metrics(raw)
                if current['q4t_execution_steps_total'] > before['q4t_execution_steps_total']:
                    save(self.out/'cancel-progress.json', dict(before=before, observed=current))
                    break
                time.sleep(.02)
            else: raise TimeoutError('no actual ASR progress before cancellation')
            connection.sock.shutdown(socket.SHUT_RDWR)
        finally: connection.close()
        after = self.drained('cancel-after')
        require(after['q4t_requests_aborted_total'] == before['q4t_requests_aborted_total']+1,
            'in-flight audio cancellation not observed')

    def business(self):
        rows = self.plan['requests']
        self.budget('controls')
        status, raw = self.http('/v1/chat/preflight', dict(request=materialize(rows['zh'])), label='preflight')
        value = json.loads(raw)
        require(status == 200 and value['status'] == 'requires_audio_preparation' and
            value['input']['tokens'] is None and not value['checks']['gpu_executed'],
            'audio preflight invented exact preparation')
        if not self.plan.get('chat_continuation'):
            failed = self.json_chat('missing-asr', rows['zh'], code=503)
            require(failed['error']['code'] == 'asr_assets_invalid', 'auxiliary failure classification')
            require(self.http('/readyz', label='healthy-during-asr-failure')[0] == 200,
                'ASR failure closed healthy core')
            plain = self.json_chat('core-during-asr-failure', rows['core'])
            require(plain['choices'][0]['message']['content'].strip() == '42', 'healthy core failed')
            assets = self.out/'asr-assets'
            (assets/'model.safetensors.withheld').rename(assets/'model.safetensors')
            time.sleep(5.1)  # Existing bounded ASR cooldown, not a model restart.
        sdk, raw = self.sdk('python-zh', rows['zh'], python=True, client='one-rpm')
        value = json.loads(raw)
        require(usage(value) == usage(sdk), 'Python SDK lost audio usage')
        require(len(value['choices']) == 2 and all(c['finish_reason'] == 'stop' and
            c['message']['content'].strip() == '交易' for c in value['choices']),
            'real Chinese speech did not reach both answers')
        limited = self.json_chat('one-rpm-exhausted', rows['core'], client='one-rpm', code=429)
        require(limited['error']['code'] == 'client_rpm_exhausted', 'one request RPM accounting')
        status, stored = self.http('/v1/chat/completions/'+value['id']+'/messages',
            client='one-rpm', label='stored-audio-history')
        require(status == 200 and json.loads(stored)['data'][0]['content'] ==
            materialize(rows['zh'])['messages'][0]['content'], 'stored audio source was replaced by transcript')
        sdk, raw = self.sdk('js-en-mp3', rows['en'])
        completion = read_sse(io.BytesIO(raw))
        usage(dict(usage=completion.usage))
        actual = json.loads(completion.message['content'])['activity'].lower()
        require(completion.finish_reason == 'stop' and 'writ' in actual and 'other' in actual,
            'real MP3 meaning differs')
        sdk_usage = [e['usage'] for e in sdk['events'] if e.get('usage')]
        require(sdk_usage == [completion.usage], 'JS SDK lost final usage or duplicated it')
        value = self.json_chat('two-audio-tool', rows['tool'])
        usage(value)
        completion = read_json(io.BytesIO(json.dumps(value).encode()))
        calls = prepare_tool_calls(completion, VALIDATORS)
        require(len(calls) == 1 and calls[0].arguments ==
            dict(path='docs/MODEL.md', start_line=1, line_count=16), 'actual tool arguments differ')
        result = ProjectRecords(ROOT).execute(calls[0])
        require(result['ok'] and result['source']['sha256'] ==
            self.plan['sources']['docs/MODEL.md'], 'actual project tool source drift')
        save(self.out/'tool-execution.json', dict(call=asdict(calls[0]), result=result))
        follow = copy.deepcopy(rows['tool'])
        follow['messages'] = append_tool_results(follow['messages'], completion,
            {calls[0].call_id: json.dumps(result, ensure_ascii=False)})
        follow['messages'].append(dict(role='user', content='Return JSON with title from the '
            'tool document, zh_topic as the two-Chinese-character subject described in the '
            'first recording, and en_activity as the activity bringing more success in the second.'))
        follow.update(tool_choice='auto', max_completion_tokens=256,
            response_format=schema({k: dict(type='string') for k in
                ('title', 'zh_topic', 'en_activity')}))
        final = self.json_chat('two-audio-tool-final', follow)
        usage(final)
        actual = json.loads(final['choices'][0]['message']['content'])
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(actual['title'] in (title, title.lstrip('# ')) and actual['zh_topic'] == '交易'
            and 'writ' in actual['en_activity'].lower() and 'other' in actual['en_activity'].lower(),
            'audio order or actual tool/JSON closure differs')
        silent = self.json_chat('silence', rows['silence'])
        usage(silent)
        require(silent['choices'][0]['message']['content'].strip() == 'OK', 'silence blocked text')
        self.json_chat('invalid-base64', rows['invalid'], code=400)
        self.json_chat('shared-duration', rows['overlong'], code=413)
        self.batch(rows['batch'])
        self.cancel(rows['cancel'])
        final = self.json_chat('core-after-cancellation', rows['core'])
        require(final['choices'][0]['message']['content'].strip() == '42', 'final core failed')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200 and json.loads(raw)['native_asr_health']['state'] == 'ready',
            'ASR did not recover independently')
        require(self.counts['chats'] == self.limits['chats'], 'frozen request coverage incomplete')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    sub.add_parser('fixtures')
    p = sub.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18280)
    p.add_argument('--continue-from', type=Path)
    p = sub.add_parser('run'); p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'fixtures': fixtures()
    elif args.mode == 'prepare': prepare(args)
    else:
        with asr.base.lifecycle.child_subreaper(): Stage(read(args.plan)).run()


if __name__ == '__main__': main()
