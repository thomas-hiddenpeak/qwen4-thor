"""One native ASR start: actual codecs/SDKs, scoped recovery and cancellation."""
import argparse
import base64
import http.client
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_schema_v2 import acceptance as base
from tools.serve_service.package import read, require, save, sha

WORK = ROOT / '.q4t-work/serve-audio-native-20261117'
LIMITS = dict(starts=1, chats=9, controls=2000, seconds=600)
MODEL = 'qwen3-asr-0.6b'
EXPECTED = {
    'zh': '甚至出现交易几乎停滞的情况。',
    'en': "Mhm. Oh yeah, yeah. He wasn't even that big when I started listening "
          "to him, but and his solo music didn't do overly well, but he did very "
          "well when he started writing for other people.",
}
FILES = ('config.json', 'preprocessor_config.json', 'generation_config.json',
         'vocab.json', 'merges.txt', 'tokenizer_config.json', 'model.safetensors')


def requests():
    return dict(zh=dict(model=MODEL, include=['logprobs']),
        en=dict(model=MODEL, include=['logprobs'], stream=True),
        forced=dict(model=MODEL, language='zh', prompt='请使用简体中文。',
                    response_format='text'),
        cancel=dict(model=MODEL, stream=True),
        chat=dict(model='qwen3.8-flash-next', temperature=0,
                  enable_thinking=False, max_completion_tokens=32,
                  messages=[dict(role='user', content='Compute 17 + 25. Reply only with the decimal number.')]))


def python_identity():
    python = WORK/'python-sdk-02/bin/python'
    require(python.is_file(), 'isolated Python SDK unavailable')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    version = subprocess.check_output([str(python), '-c',
        'import openai,platform;print(openai.__version__);print(platform.python_version())'],
        env=env, text=True, timeout=30).splitlines()
    require(version[0] == '2.8.1', 'Python SDK drift')
    paths = sorted(p for p in (WORK/'python-sdk-02/lib').rglob('*')
                   if p.is_file() and p.suffix != '.pyc')
    return dict(python=str(python), executable_sha256=sha(python),
                versions=version, files={str(p): sha(p) for p in paths})


def prepare(args):
    base.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    out = args.output.resolve()
    plan = read(out/'plan.json')
    plan['python_sdk'] = python_identity()
    for path in (ROOT/'tools/native_asr').glob('*'):
        if path.is_file(): plan['sources'][str(path.relative_to(ROOT))] = sha(path)
    acquired = {row['name']: row for row in read(WORK/'asset-acquisition-01.json')['files']}
    plan['asr_assets'] = {name: {k: acquired[name][k] for k in ('bytes', 'sha256')}
                          for name in FILES}
    plan['asr_directory'] = str(WORK/'assets-01')
    plan['clips'] = {language: dict(path=str(WORK/'real-audio-01'/(language+'.wav')),
                                   sha256=sha(WORK/'real-audio-01'/(language+'.wav')))
                      for language in EXPECTED}
    plan['expected'] = EXPECTED
    plan['planned'] = dict(starts=1, creates=9, successful_asr=3, cancelled_asr=1,
                            rejected_asr=3, core_chats=2)
    plan['quality'] = dict(zh_exact=True, en_word_error_rate_max=0.05,
                           en_normalization='lowercase words retaining apostrophes')
    plan['mutations'] = ['only private ASR fixture directory entries',
        'readonly hardlinks to fixed ASR assets; original contents never changed',
        'reveal the private weight link after observed missing-assets failure',
        'close one owned transcription socket after first body delta',
        'no model restart, published default change or external service action']
    save(out/'plan.json', plan)


def word_error_rate(actual, expected):
    a = re.findall(r"[a-z]+(?:'[a-z]+)?", actual.lower())
    b = re.findall(r"[a-z]+(?:'[a-z]+)?", expected.lower())
    row = list(range(len(b) + 1))
    for i, token in enumerate(a, 1):
        new = [i]
        for j, reference in enumerate(b, 1):
            new.append(min(new[-1] + 1, row[j] + 1,
                           row[j-1] + (token != reference)))
        row = new
    return row[-1] / max(1, len(b))


def quality(text, language):
    require(isinstance(text, str) and bool(text.strip()), 'empty transcription')
    if language == 'zh': require(text == EXPECTED['zh'], 'actual Chinese transcription differs')
    error = word_error_rate(text, EXPECTED['en']) if language == 'en' else 0
    require(error <= 0.05, 'actual English WER exceeds frozen business bound')
    return dict(text=text, exact=text == EXPECTED[language], word_error_rate=error)


def usage(value):
    u = value['usage']
    require(u['type'] == 'tokens' and all(type(u[k]) is int and u[k] > 0
        for k in ('input_tokens', 'output_tokens', 'total_tokens')), 'missing actual token usage')
    require(u['input_tokens'] + u['output_tokens'] == u['total_tokens'], 'usage sum')
    details = u['input_token_details']
    require(details['audio_tokens'] > 0 and details['text_tokens'] >= 15 and
            details['audio_tokens'] + details['text_tokens'] == u['input_tokens'], 'audio/text usage')
    logs = value['logprobs']
    require(logs and all(isinstance(x['token'], str) and math.isfinite(x['logprob']) and
        x['logprob'] <= 0 and x['bytes'] and all(type(b) is int and 0 <= b <= 255
        for b in x['bytes']) for x in logs), 'actual token probabilities absent')


class Stage(base.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_ASR_HTTP_SDK_SCOPED_RECOVERY'

    def run(self):
        result = dict(status='FAILED')
        previous = signal.getsignal(signal.SIGALRM)
        def expired(_signum, _frame):
            raise TimeoutError('ASR stage deadline; cleanup reserve retained')
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL,
                         max(.01, self.limits['seconds'] - 40 - (time.monotonic() - self.started)))
        try:
            self.start()
            self.business()
            result['status'] = self.success_status
        except BaseException:
            result['error'] = traceback.format_exc()
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            try:
                self.cleanup()
                require(all(sha(p) == h for p,h in self.plan['protected'].items()), 'default changed')
                result['default_unchanged'] = True
            except BaseException:
                result['status'] = 'FAILED'
                result['cleanup_error'] = traceback.format_exc()
                raise
            finally:
                result.update(counts=self.counts, seconds=time.monotonic()-self.started,
                              binary_sha256=self.plan['binary_sha256'])
                save(self.out/'result.json', result)

    def __init__(self, plan):
        super().__init__(plan)
        require(plan['python_sdk'] == python_identity(), 'Python SDK identity drift')
        require(plan['expected'] == EXPECTED, 'quality oracle drift')
        for row in plan['clips'].values(): require(sha(row['path']) == row['sha256'], 'clip drift')

    def start(self):
        for port in (self.plan['port'], self.plan['operations_port']):
            with socket.socket() as sock: sock.bind(('127.0.0.1', port))
        assets = self.out/'asr-assets'
        assets.mkdir(mode=0o700)
        for name, record in self.plan['asr_assets'].items():
            source = Path(self.plan['asr_directory'])/name
            require(source.stat().st_size == record['bytes'] and sha(source) == record['sha256'], 'ASR asset drift')
            target = assets/(name + '.withheld' if name == 'model.safetensors' else name)
            os.link(source, target)
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '64', '--max-inflight-requests', '8',
            '--max-queued-requests', '2', '--asr-model-dir', str(assets),
            '--asr-max-output-tokens', '128']
        save(self.out/'model-command.json', args)
        self.budget('starts')
        with (self.out/'model.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = base.lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/'model-identity.json', dict(pid=self.model.pid, **self.model_identity))
        end = time.monotonic() + 180
        while time.monotonic() < end:
            self.same_model()
            try:
                if self.http('/readyz', timeout=1)[0] == 200: break
            except (OSError, http.client.HTTPException): pass
            time.sleep(.1)
        else: raise TimeoutError('model readiness')
        for name in ('capabilities', 'config', 'models'):
            status, raw = self.http('/v1/'+name, label='initial-'+name)
            require(status == 200, name+' unavailable')
            save(self.out/(name+'.json'), json.loads(raw))
        self.client.protocol_sha256 = read(self.out/'capabilities.json')['protocol_source_sha256']
        require(MODEL in [v['id'] for v in read(self.out/'models.json')['data']], 'ASR model missing')
        require(read(self.out/'capabilities.json')['transcriptions']['configured'], 'ASR undeclared')

    def multipart(self, label, fields, audio, *, cancel=False):
        boundary = 'q4t-native-audio-qualification'
        pieces = []
        for key, value in fields:
            pieces.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+key+'"\r\n\r\n'+value+'\r\n').encode())
        pieces.extend([('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode(),
                       audio, ('\r\n--'+boundary+'--\r\n').encode()])
        self.budget('chats')
        conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=180)
        raw = b''
        try:
            conn.request('POST', '/v1/audio/transcriptions', body=b''.join(pieces),
                         headers={'Content-Type': 'multipart/form-data; boundary='+boundary})
            response = conn.getresponse()
            if cancel:
                require(response.status == 200, 'cancellation request did not stream')
                while len(raw) < 1024 * 1024:
                    line = response.readline(65537)
                    require(line and len(line) <= 65536, 'stream ended before cancellation')
                    raw += line
                    if line.startswith(b'data: {'):
                        event = json.loads(line[6:])
                        if event.get('type') == 'transcript.text.delta' and event['delta']: break
                else: raise RuntimeError('cancellation stream byte budget')
                # Abort the entire owned connection even if the response object
                # retains the socket while waiting for its remaining body.
                if conn.sock: conn.sock.shutdown(socket.SHUT_RDWR)
                response.close()
            else:
                raw = response.read(4 * 1024 * 1024 + 1)
                require(len(raw) <= 4 * 1024 * 1024, 'wire budget')
            (self.out/(label+'.wire')).write_bytes(raw)
            save(self.out/(label+'.http.json'), dict(status=response.status,
                headers=dict(response.getheaders()), cancellation=cancel))
            return response.status, raw
        finally: conn.close()

    def sdk(self, label, language, request, python=False):
        self.budget('chats')
        args = dict(base_url=f"http://127.0.0.1:{self.plan['port']}/v1",
                    sdk_directory=self.plan['sdk']['sdk_directory'],
                    audio=self.plan['clips'][language]['path'], filename=language+'.wav',
                    request=request)
        save(self.out/(label+'.request.json'), args)
        command = ([self.plan['python_sdk']['python'], '-B',
                    str(ROOT/'tools/native_asr/transcription_sdk.py')] if python else
                   ['node', str(ROOT/'tools/native_asr/transcription_sdk.mjs')])
        env = {k:v for k,v in os.environ.items() if k not in (
            'NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD', 'HTTP_PROXY', 'HTTPS_PROXY',
            'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'NODE_USE_ENV_PROXY')}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        run = subprocess.run(command, input=json.dumps(args), capture_output=True,
                             text=True, env=env, timeout=190)
        (self.out/(label+'.stderr.txt')).write_text(run.stderr)
        require(run.stdout, 'SDK returned no record')
        result = json.loads(run.stdout)
        (self.out/(label+'.wire')).write_bytes(base64.b64decode(result.pop('wire_base64'), validate=True))
        save(self.out/(label+'.sdk.json'), result)
        require(run.returncode == 0 and result['ok'], 'actual SDK request failed: '+label)
        require(len(result['exchanges']) == 1, 'SDK retried request')
        self.same_model()
        self.drained(label+'-drain')
        return result['result']

    def healthy(self, label):
        status, raw = self.http('/readyz', label=label+'-ready')
        require(status == 200, 'ASR failure poisoned core readiness')
        status, raw = self.http('/v1/config', label=label+'-config')
        require(status == 200, 'configuration unavailable')
        return json.loads(raw)['native_asr_health']

    def core(self, label):
        status, raw = self.submit(label, self.plan['requests']['chat'])
        require(status == 200, 'core chat failed after ASR operation')
        value = json.loads(raw)
        require(value['choices'][0]['message']['content'].strip() == '42' and
                value['choices'][0]['finish_reason'] == 'stop', 'actual core answer differs')
        self.same_model()
        self.drained(label+'-drain')

    def business(self):
        zh = Path(self.plan['clips']['zh']['path']).read_bytes()
        en = Path(self.plan['clips']['en']['path']).read_bytes()
        fields = [('model', MODEL)]
        status, raw = self.multipart('missing-weights', fields, zh)
        require(status == 503 and json.loads(raw)['error']['code'] == 'asr_assets_invalid', 'missing assets were not classified')
        failed_at = time.monotonic()
        health = self.healthy('missing')
        require(health['state'] == 'degraded' and health['load_failures'] == 1, 'ASR failure not visible')
        status, raw = self.multipart('cooldown', fields, zh)
        require(status == 503 and json.loads(raw)['error']['code'] == 'asr_retry_pending', 'unbounded asset retry')
        self.core('core-during-asr-failure')
        assets = self.out/'asr-assets'
        os.rename(assets/'model.safetensors.withheld', assets/'model.safetensors')
        # Wait only for the observed attempt's explicit cooldown, not a blind
        # retry loop. There is exactly one next business attempt.
        time.sleep(max(0, 5.1 - (time.monotonic() - failed_at)))
        value = self.sdk('python-zh-json', 'zh', self.plan['requests']['zh'], python=True)
        quality(value['text'], 'zh');usage(value)
        save(self.out/'zh-quality.json', quality(value['text'], 'zh'))
        health = self.healthy('recovered')
        require(health['state'] == 'ready' and health['load_failures'] == 1 and
                health['compute_failures'] == 0, 'ASR did not recover without restart')
        value = self.sdk('js-en-stream', 'en', self.plan['requests']['en'])
        events = value['events']
        wire = (self.out/'js-en-stream.wire').read_bytes()
        require(events and events[-1]['type'] == 'transcript.text.done' and
                all(e['type'] == 'transcript.text.delta' for e in events[:-1]) and
                b'[DONE]' not in wire, 'wrong transcription event terminal')
        final = events[-1]
        require(''.join(e['delta'] for e in events[:-1]) == final['text'], 'delta/final text drift')
        require([p for e in events[:-1] for p in e.get('logprobs', [])] == final['logprobs'], 'delta/final probabilities drift')
        usage(final)
        save(self.out/'en-quality.json', quality(final['text'], 'en'))
        value = self.sdk('js-forced-text', 'zh', self.plan['requests']['forced'])
        save(self.out/'forced-quality.json', quality(value, 'zh'))
        status, raw = self.multipart('malformed', [('model', MODEL), ('stream', 'not_bool')], zh)
        require(status == 400 and json.loads(raw)['error']['param'] == 'stream', 'malformed field was executed')
        before = self.drained('before-cancel')
        self.multipart('cancel-stream', [('model', MODEL), ('stream', 'true')], en, cancel=True)
        after = self.drained('after-cancel')
        require(after['q4t_requests_aborted_total'] == before['q4t_requests_aborted_total'] + 1, 'actual cancellation was not observed')
        self.core('core-after-cancel')
        self.healthy('final')
        require(self.counts['chats'] == 9, 'attempt accounting changed')
        save(self.out/'quality.json', dict(zh=read(self.out/'zh-quality.json'),
             en=read(self.out/'en-quality.json'), forced=read(self.out/'forced-quality.json'),
             original_compute_logits_gate='FAILED preserved; not this API acceptance',
             timestamp_diarization_vad_pending=True, full_model_quality=False,
             default_replacement=False, all_master_tasks_continue=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    prepare_args = sub.add_parser('prepare')
    prepare_args.add_argument('--binary', type=Path, required=True)
    prepare_args.add_argument('--output', type=Path, required=True)
    prepare_args.add_argument('--port', type=int, default=18224)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    else:
        with base.lifecycle.child_subreaper(): Stage(read(args.plan)).run()


if __name__ == '__main__': main()
