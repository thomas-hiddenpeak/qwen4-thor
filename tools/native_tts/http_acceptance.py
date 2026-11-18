"""Bounded real Speech HTTP, SDK, scoped recovery, cancellation and content."""
import argparse
import base64
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_asr.transcription_acceptance import python_identity
from tools.native_tts.acceptance import CASES, defaults
from tools.native_tts.utterance_acceptance import content_check
from tools.serve_grammar_reap import acceptance as lifecycle
from tools.serve_protocol.stage import sdk_identity
from tools.serve_recovery.bundle import assets, check_assets
from tools.serve_service.package import read, require, save, sha

WORK = ROOT / '.q4t-work/serve-native-tts-20261118'
ASR = ROOT / '.q4t-work/serve-audio-native-20261117'
MODEL = 'qwen3-tts-12hz-1.7b-customvoice'
LIMITS = dict(starts=1, chats=13, controls=2000, seconds=1200,
    gpu_talker_loads=1, gpu_codec_loads=1, gpu_asr_loads=3,
    decode_cases=8, asr_tokens_per_case=64, artifact_mib=64,
    disk_headroom_mib=512)
CONTINUATION_LIMITS = dict(LIMITS, chats=11)
REMAINING_LIMITS = dict(LIMITS, chats=10)
FORMATS = ['mp3', 'aac', 'opus', 'flac', 'wav', 'pcm', 'pcm', 'mp3']
UTTERANCES = [0, 1, 0, 0, 0, 0, 1, 1]
MIMES = dict(mp3='audio/mpeg', aac='audio/aac', opus='audio/ogg',
             flac='audio/flac', wav='audio/wav', pcm='audio/pcm')


def speech(index):
    case = CASES[UTTERANCES[index]]
    return dict(model=MODEL, input=case['text'], voice=case['voice'],
        instructions=case['instruction'], response_format=FORMATS[index],
        stream_format='sse' if index == 6 else 'audio', speed=1)


def sse(raw):
    require(b'[DONE]' not in raw, 'Chat terminator in Speech')
    events = [json.loads(line[6:]) for line in raw.splitlines()
              if line.startswith(b'data: ')]
    require(len(events) > 1 and events[-1].get('type') == 'speech.audio.done',
            'missing successful Speech terminal')
    require(all(v.get('type') == 'speech.audio.delta' for v in events[:-1]),
            'unexpected Speech event/order')
    data = b''.join(base64.b64decode(v['audio'], validate=True)
                    for v in events[:-1])
    usage = events[-1]['usage']
    require(all(type(usage[k]) is int and usage[k] > 0 for k in
                ('input_tokens', 'output_tokens', 'total_tokens')) and
            usage['input_tokens'] + usage['output_tokens'] == usage['total_tokens'],
            'Speech actual usage missing')
    require(data and usage['output_tokens'] % 16 == 1, 'natural codec EOS usage')
    return data, dict(events=len(events), usage=usage)


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain']),
            'commit reviewed sources before freezing')
    out = args.output.absolute()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'work output only')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    model = Path.home()/'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
    names = subprocess.check_output(['git', 'ls-files', 'src', 'include',
        'cmake', 'CMakeLists.txt', 'tools/native_tts', 'tools/native_asr',
        'tools/serve_grammar_reap', 'tools/serve_protocol', 'tools/serve_recovery',
        'tools/serve_service', 'tools/evalscope', 'tools/examples', 'tools/release'],
        text=True).splitlines()
    binaries = dict(engine=args.binary.absolute(), decoder=args.decoder.absolute(),
        asr=ASR/'validated-binaries/q4t_asr_replay-compute-01')
    tts_assets = WORK/'assets-1b7-01'
    reused = reuse(args.continue_from) if args.continue_from else None
    completed = reuse_speech(args.reuse_speech) if args.reuse_speech else None
    require(not completed or reused, 'Speech reuse requires the retained fault evidence')
    plan = dict(schema='q4t-speech-http-v1', output=str(out),
        limits=REMAINING_LIMITS if completed else CONTINUATION_LIMITS if reused else LIMITS,
        port=args.port, operations_port=args.port+1, model=str(model),
        model_assets=assets(model), requests=[speech(i) for i in range(8)],
        sources={name: sha(ROOT/name) for name in names},
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        binaries={k: dict(path=str(v), sha256=sha(v)) for k,v in binaries.items()},
        defaults=defaults(), sdk=sdk_identity(), python_sdk=python_identity(),
        tts_directory=str(tts_assets), tts_assets={str(p.relative_to(tts_assets)):
            dict(bytes=p.stat().st_size, sha256=sha(p))
            for p in sorted(tts_assets.rglob('*')) if p.is_file()},
        asr_assets={p.name: sha(p) for p in sorted((ASR/'assets-01').iterdir())
                    if p.is_file()},
        acceptance=dict(exact_normalized_unprompted_asr=True,
            all_six_formats=True, actual_speech_sse_usage=True,
            same_pid_core_before_after=True, default_unchanged=True,
            no_model_restart=True, four_times_speed_qualified=False),
        mutations=['withhold/reveal private codec config hardlink only',
            'close one owned Speech stream after real bytes',
            'stop only the owned candidate before three ASR processes'])
    if reused:
        require(plan['binaries']['engine'] == reused['engine'], 'reused engine drift')
        plan['continuation'] = reused
    if completed:
        require(plan['binaries']['engine'] == completed['engine'], 'Speech engine drift')
        plan['completed_speech'] = completed
    save(out/'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), limits=plan['limits'])))


def reuse(path):
    path = Path(path).resolve(strict=True)
    require(path.is_relative_to(WORK), 'reuse outside TTS evidence')
    plan, result = read(path/'plan.json'), read(path/'result.json')
    require(result['status'] == 'FAILED' and result['counts']['chats'] == 3 and
        result['counts']['starts'] == 1 and result['defaults_unchanged'] and
        'owned auxiliary child survived request drain' in result['error'], 'unexpected failure')
    require(read(path/'lease/exit.json')['status'] == 'COMMAND_FAILED', 'ENV_ABORT cannot be reused')
    cleanup = read(path/'cleanup.json')
    require(cleanup['model_exit'] == 0 and not cleanup['remaining'] and cleanup['ports_released'], 'cleanup')
    for name,h in plan['sources'].items():
        if name != 'tools/native_tts/http_acceptance.py':
            require(sha(ROOT/name) == h, 'reused source drift: '+name)
    for label,code in [('missing-assets','tts_assets_invalid'), ('cooldown','tts_retry_pending')]:
        require(read(path/(label+'.http.json'))['status'] == 503 and
            read(path/(label+'.wire'))['error']['code'] == code, 'reused fault')
    require(read(path/'fault-ready.http.json')['status'] == 200, 'reused readiness')
    row = read(path/'core-during-fault.wire')['choices'][0]
    require(read(path/'core-during-fault.http.json')['status'] == 200 and
        row['finish_reason'] == 'stop' and row['message']['content'].strip() == '42', 'reused core content')
    require(defaults() == plan['defaults'], 'reused default drift')
    require(sha(plan['binaries']['engine']['path']) == plan['binaries']['engine']['sha256'], 'reused engine')
    return dict(directory=str(path), original_status='FAILED_preserved', reused_posts=3,
        engine=plan['binaries']['engine'],
        reason='driver confused two expected persistent workers with per-request leaks',
        files={str(p.relative_to(path)):sha(p) for p in sorted(path.rglob('*'))
            if p.is_file() and 'tts-assets' not in p.relative_to(path).parts})


def reuse_speech(path):
    path = Path(path).resolve(strict=True)
    require(path.is_relative_to(WORK), 'Speech evidence outside workdir')
    plan, result = read(path/'plan.json'), read(path/'result.json')
    require(result['status'] == 'COMPLETED_LOAD_DIAGNOSIS' and result['observer_exit'] == 0
        and result['counts']['chats'] == 1 and result['defaults_unchanged'], 'incomplete Speech diagnosis')
    require(read(path/'lease/exit.json')['status'] == 'COMPLETED', 'invalid Speech lease')
    cleanup = read(path/'cleanup.json')
    require(cleanup['model_exit'] == 0 and not cleanup['remaining'] and cleanup['ports_released'], 'Speech cleanup')
    for name,h in plan['sources'].items():
        if name != 'tools/native_tts/http_acceptance.py':
            require(sha(ROOT/name) == h, 'Speech source drift: '+name)
    require(read(path/'speech.request.json') == speech(0), 'Speech request drift')
    response = read(path/'speech.http.json')
    require(response['status'] == 200 and not response['cancelled'] and
        response['headers']['Transfer-Encoding'] == 'chunked' and
        response['headers']['Content-Type'] == 'audio/mpeg' and response['chunks'] > 1 and
        response['first_seconds'] < response['seconds'] and
        (path/'speech.wire').stat().st_size == response['bytes'] > 1024, 'Speech framing')
    require(defaults() == plan['defaults'] and sha(plan['binaries']['engine']['path']) ==
        plan['binaries']['engine']['sha256'], 'Speech protected identity')
    return dict(directory=str(path), engine=plan['binaries']['engine'], case=0,
        state='completed_transport_audio_pending_decoding_and_asr',
        files={str(p.relative_to(path)):sha(p) for p in sorted(path.rglob('*'))
            if p.is_file() and 'tts-assets' not in p.relative_to(path).parts})


class Stage(lifecycle.Stage):
    def __init__(self, plan):
        require(not os.environ.get('PYTHONOPTIMIZE') and not any(
            key.startswith('Q4T_ABLATE_') for key in os.environ), 'unqualified environment')
        self.plan, self.out = plan, Path(plan['output'])
        self.started = time.monotonic()
        self.counts = dict(starts=0, chats=0, controls=0, gpu_asr_loads=0)
        self.model = self.model_identity = self.fault_thread = None
        self.history = {}
        self.lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
        self.limits = (REMAINING_LIMITS if plan.get('completed_speech') else
                       CONTINUATION_LIMITS if plan.get('continuation') else LIMITS)
        if plan.get('continuation'):
            require(plan['continuation'] == reuse(plan['continuation']['directory']), 'reuse identity')
        if plan.get('completed_speech'):
            require(plan['completed_speech'] == reuse_speech(plan['completed_speech']['directory']), 'Speech reuse')
        lifecycle.verify_lease(self.lease, lifecycle.guard.common_lock())
        require(plan['limits'] == self.limits and plan['schema'] == 'q4t-speech-http-v1'
            and plan['requests'] == [speech(i) for i in range(8)], 'frozen plan drift')
        for p,h in plan['sources'].items(): require(sha(ROOT/p) == h, 'source drift: '+p)
        for row in plan['binaries'].values(): require(sha(row['path']) == row['sha256'], 'binary drift')
        require(defaults() == plan['defaults'], 'default drift')
        require(sdk_identity() == plan['sdk'] and python_identity() == plan['python_sdk'], 'SDK drift')
        check_assets(plan['model'], plan['model_assets'])
        for name,h in plan['asr_assets'].items(): require(sha(ASR/'assets-01'/name) == h, 'ASR drift')
        self.artifacts()

    def artifacts(self):
        # Private assets are readonly hardlinks, not new disk artifacts.
        count = sum(p.stat().st_size for p in self.out.rglob('*')
            if p.is_file() and 'tts-assets' not in p.relative_to(self.out).parts)
        require(count <= LIMITS['artifact_mib'] << 20, 'artifact budget')
        require(shutil.disk_usage(ROOT).free >= LIMITS['disk_headroom_mib'] << 20,
                'disk headroom')

    def budget(self, kind):
        require(time.monotonic()-self.started < LIMITS['seconds']-40, 'stage deadline')
        require(self.counts[kind] < self.limits[kind], 'attempt budget: '+kind)
        self.counts[kind] += 1
        save(self.out/'counts.json', self.counts)

    def same_model(self):
        super().same_model()
        for pid,row in lifecycle.guard.processes().items():
            if row.get('ppid') == self.model.pid:
                self.history[pid] = row

    def drained(self, label):
        result = super().drained(label)
        children = {pid:row for pid,row in lifecycle.guard.processes().items()
                    if row.get('ppid') == self.model.pid}
        save(self.out/(label+'.children.json'), children)
        # These two existing services intentionally span requests. Still bind
        # each process to this parent and candidate inode; all others must exit.
        expected = Path(self.plan['binaries']['engine']['path']).stat()
        kinds = []
        for pid,row in children.items():
            argv = row.get('argv', [])
            require(len(argv) == 4 and argv[1] in
                ('--q4t-cache-worker', '--q4t-object-worker') and
                argv[0] == argv[1][2:] and argv[2] == str(self.model.pid),
                'unexpected worker survived request drain')
            image = (Path('/proc')/str(pid)/'exe').stat()
            require((image.st_dev,image.st_ino) == (expected.st_dev,expected.st_ino),
                    'persistent worker image differs')
            kinds.append(argv[1]); self.history[pid] = row
        require(len(kinds) == len(set(kinds)), 'duplicate persistent workers')
        return result

    def start(self):
        for port in (self.plan['port'], self.plan['operations_port']):
            with socket.socket() as sock: sock.bind(('127.0.0.1', port))
        private = self.out/'tts-assets'
        private.mkdir(mode=0o700)
        for name,row in self.plan['tts_assets'].items():
            source = Path(self.plan['tts_directory'])/name
            require(source.stat().st_size == row['bytes'] and sha(source) == row['sha256'], 'TTS asset drift')
            target = private/(name+'.withheld' if name == 'speech_tokenizer/config.json' else name)
            target.parent.mkdir(exist_ok=True)
            os.link(source, target)
        args = [self.plan['binaries']['engine']['path'], 'serve',
            '--model-dir', self.plan['model'], '--host', '127.0.0.1',
            '--port', str(self.plan['port']), '--operations-port', str(self.plan['operations_port']),
            '--mtp', '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '64', '--max-inflight-requests', '4', '--max-queued-requests', '2',
            '--tts-model-dir', str(private), '--tts-max-text-tokens', '512',
            '--tts-max-prefill-tokens', '512', '--tts-max-frames', '160',
            '--tts-workspace-mib', '2048', '--tts-max-output-mib', '16']
        save(self.out/'model-command.json', args)
        self.budget('starts')
        with (self.out/'model.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = lifecycle.guard.processes()[self.model.pid]
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
        for name in ('capabilities', 'config', 'models'):
            status,raw = self.http('/v1/'+name, label='initial-'+name)
            require(status == 200, 'initial '+name)
        require(MODEL in [v['id'] for v in read(self.out/'initial-models.wire')['data']], 'speech catalog')
        require(read(self.out/'initial-capabilities.wire')['speech']['enabled'], 'speech declaration')

    def post(self, label, body, *, cancel=False):
        self.budget('chats')
        save(self.out/(label+'.request.json'), body)
        conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=180)
        started = time.monotonic()
        try:
            conn.request('POST', '/v1/audio/speech', json.dumps(body,ensure_ascii=False).encode(),
                {'Content-Type': 'application/json', 'Connection': 'close'})
            response = conn.getresponse()
            chunks, size, first = [], 0, None
            while True:
                chunk = response.read1(8192)
                if not chunk: break
                if first is None: first = time.monotonic()-started
                chunks.append(chunk); size += len(chunk)
                require(size <= 2 << 20, 'HTTP byte budget')
                if cancel:
                    require(response.status == 200 and size > 0, 'cancel before real output')
                    if conn.sock: conn.sock.shutdown(socket.SHUT_RDWR)
                    response.close()
                    break
            raw = b''.join(chunks)
            (self.out/(label+'.wire')).write_bytes(raw)
            save(self.out/(label+'.http.json'), dict(status=response.status,
                headers=dict(response.getheaders()), bytes=len(raw), chunks=len(chunks),
                first_seconds=first, seconds=time.monotonic()-started, cancelled=cancel))
            return response.status, raw
        finally: conn.close()

    def core(self, label):
        self.budget('chats')
        status,raw = self.http('/v1/chat/completions', dict(model='qwen3.8-flash-next',
            enable_thinking=False, temperature=0, max_completion_tokens=32,
            messages=[dict(role='user', content='Compute 17 + 25. Reply only with the decimal number.')]), label=label)
        require(status == 200, 'core Chat unavailable')
        row = json.loads(raw)['choices'][0]
        require(row['finish_reason'] == 'stop' and row['message']['content'].strip() == '42', 'core content')
        self.drained(label+'-drain')

    def sdk(self, index):
        self.budget('chats')
        label = 'case-'+str(index)
        output = self.out/(label+'.wire')
        value = dict(base_url=f"http://127.0.0.1:{self.plan['port']}/v1",
            sdk_directory=self.plan['sdk']['sdk_directory'], request=speech(index), output=str(output))
        save(self.out/(label+'.request.json'), value)
        cmd = ([self.plan['python_sdk']['python'], '-B', str(ROOT/'tools/native_tts/http_sdk.py')]
            if index == 6 else ['node', str(ROOT/'tools/native_tts/http_sdk.mjs')])
        child = subprocess.run(cmd, input=json.dumps(value), text=True,
            capture_output=True, timeout=180, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        (self.out/(label+'.sdk.log')).write_text(child.stdout+child.stderr)
        require(child.returncode == 0, 'SDK failed: '+label)
        result = json.loads(child.stdout)
        save(self.out/(label+'.sdk.json'), result)
        require(result['ok'] and result['status'] == 200 and len(result['exchanges']) == 1,
                'SDK failed/retried')
        raw = output.read_bytes()
        require(sha(output) == result['sha256'], 'SDK payload drift')
        return raw, result['headers']

    def business(self):
        faults = ([('recovery-bootstrap','tts_assets_invalid')] if self.plan.get('continuation')
            else [('missing-assets','tts_assets_invalid'), ('cooldown','tts_retry_pending')])
        for label,code in faults:
            status,raw = self.post(label, speech(0))
            require(status == 503 and json.loads(raw)['error']['code'] == code, 'asset error classification')
        require(self.http('/readyz', label='fault-ready')[0] == 200, 'auxiliary fault poisoned core')
        self.http('/v1/config', label='fault-config')
        health = read(self.out/'fault-config.wire')['native_tts_health']
        require(health['state'] == 'degraded' and health['load_failures'] == 1, 'scoped health')
        if not self.plan.get('continuation'): self.core('core-during-fault')
        hidden = self.out/'tts-assets/speech_tokenizer/config.json.withheld'
        hidden.rename(hidden.with_suffix(''))
        time.sleep(5.1)  # One bounded known cooldown, never an automatic retry.
        rows = []
        for index in range(8):
            label = 'case-'+str(index)
            if index == 0 and self.plan.get('completed_speech'):
                source = Path(self.plan['completed_speech']['directory'])
                raw = (source/'speech.wire').read_bytes()
                response = read(source/'speech.http.json')
                headers = response['headers']
                (self.out/(label+'.wire')).write_bytes(raw)
                save(self.out/(label+'.http.json'), dict(response, reused_from=str(source)))
                save(self.out/(label+'.request.json'), speech(index))
            elif index < 6:
                status,raw = self.post(label, speech(index))
                require(status == 200, 'Speech failed: '+label)
                headers = read(self.out/(label+'.http.json'))['headers']
            else: raw,headers = self.sdk(index)
            headers = {k.lower():v for k,v in headers.items()}
            require(headers.get('transfer-encoding') == 'chunked', 'Speech streaming frame missing')
            expected = 'text/event-stream' if index == 6 else MIMES[FORMATS[index]]
            require(headers['content-type'].split(';')[0] == expected, 'Speech MIME differs')
            if index == 6:
                raw,event = sse(raw)
                save(self.out/(label+'.events.json'), event)
            path = self.out/(label+'.'+FORMATS[index])
            path.write_bytes(raw)
            rows.append(dict(path=str(path), sha256=sha(path), format=FORMATS[index]))
            self.drained(label+'-drain'); self.artifacts()
        cancel = speech(0); cancel['input'] = CASES[0]['text'] * 20
        self.post('cancel', cancel, cancel=True)
        self.drained('cancel-drain')
        self.core('core-after-cancel')
        self.http('/v1/config', label='final-config')
        require(read(self.out/'final-config.wire')['native_tts_health']['state'] == 'ready', 'TTS health lost')
        self.same_model()
        save(self.out/'decode-plan.json', dict(schema='q4t-speech-http-decode-v1', cases=rows))

    def command(self, label, args, timeout=180, env=None):
        require(time.monotonic()-self.started < LIMITS['seconds']-40, 'stage deadline')
        started = time.monotonic()
        with (self.out/(label+'.log')).open('xb') as log:
            child = subprocess.run(args, cwd=ROOT, env=env, pass_fds=(self.lease,),
                stdout=log, stderr=subprocess.STDOUT,
                timeout=min(timeout, LIMITS['seconds']-40-(time.monotonic()-self.started)))
        save(self.out/(label+'.command.json'), dict(command=args, exit=child.returncode,
            seconds=time.monotonic()-started))
        require(child.returncode == 0, label+' failed')
        self.artifacts()

    def content(self):
        self.command('decode', [self.plan['binaries']['decoder']['path'], '--speech-http-decode',
            str(self.out/'decode-plan.json'), str(self.out/'decoded')], 60)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
            PYTHONPATH=str(ASR/'reference-deps'), CUDA_VISIBLE_DEVICES='', USE_TORCH='0')
        self.command('features', [str(ROOT/'.q4t-work/refenv/bin/python'), '-B', __file__,
            'features', '--output', str(self.out)], 60, env)
        checks = []
        for group in range(3):
            self.budget('gpu_asr_loads')
            self.command('asr-'+str(group), [self.plan['binaries']['asr']['path'],
                str(ASR/'assets-01'), str(self.out/'features'/f'plan-{group}.json'),
                str(self.out/f'asr-{group}')], 180)
            for local in range(min(3, 8-group*3)):
                index = group*3+local
                directory = self.out/f'asr-{group}'/f'case-{local}'
                receipt = read(directory/'result.json')
                text = (directory/'text.txt').read_text()
                try: check = content_check(text, CASES[UTTERANCES[index]])
                except AssertionError as error: check = dict(passed=False, error=str(error), transcript=text)
                check['passed'] = check['passed'] and receipt['eos'] and 0 < receipt['generated_tokens'] <= 64
                checks.append(dict(case=index, format=FORMATS[index], **check))
        save(self.out/'content.json', checks)
        require(all(c['passed'] for c in checks), 'actual Speech content differs')
        hashes = [sha(self.out/'decoded'/f'case-{i}'/'pcm.f32') for i in (3,4,5)]
        require(len(set(hashes)) == 1, 'lossless container altered identical speech')

    def run(self):
        result = dict(status='FAILED')
        previous = signal.getsignal(signal.SIGALRM)
        def expired(_signum, _frame): raise TimeoutError('Speech stage deadline')
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(.01, LIMITS['seconds']-40-(time.monotonic()-self.started)))
        try:
            self.start(); self.business()
            self.cleanup()
            require(self.model.returncode == 0, 'candidate graceful shutdown failed')
            self.content()
            result['status'] = 'PASS_SPEECH_HTTP_SDK_CONTENT_PENDING_LEASE'
        except BaseException:
            result['error'] = traceback.format_exc()
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            try: self.cleanup()
            except BaseException:
                result.update(status='FAILED', cleanup_error=traceback.format_exc())
                raise
            finally:
                result.update(counts=self.counts, seconds=time.monotonic()-self.started,
                    defaults_unchanged=defaults() == self.plan['defaults'])
                if not result['defaults_unchanged']: result['status'] = 'FAILED'
                save(self.out/'result.json', result)


def features(out):
    import numpy as np
    import transformers
    from transformers import Qwen2TokenizerFast, WhisperFeatureExtractor
    require(transformers.__version__ == '4.57.6', 'feature reference drift')
    tokenizer = Qwen2TokenizerFast.from_pretrained(ASR/'assets-01', local_files_only=True)
    extractor = WhisperFeatureExtractor.from_dict(read(ASR/'assets-01/preprocessor_config.json'))
    template = tokenizer.encode('<|im_start|>system\n<|im_end|>\n<|im_start|>user\n'
        '<|audio_start|><|audio_pad|><|audio_end|><|im_end|>\n<|im_start|>assistant\n', add_special_tokens=False)
    require(template.index(151676) == 9 and template.count(151676) == 1, 'ASR template')
    target = out/'features'; target.mkdir()
    cases = []
    for index in range(8):
        directory = out/'decoded'/f'case-{index}'
        record = read(directory/'result.json')
        pcm = directory/'pcm.f32'; data = np.fromfile(pcm, '<f4')
        require(record['worker_starts'] == record['worker_reaped'] == 1 and
            record['samples'] == data.size <= 240000 and record['sample_rate'] == 16000 and
            sha(pcm) == record['decoded_sha256'], 'decoded identity')
        require(np.isfinite(data).all() and float(np.max(np.abs(data))) > .01, 'inaudible/nonfinite speech')
        mel = extractor._np_extract_fbank_features(data[None,:], 'cpu')[0]
        frames = data.size//160
        require(mel.shape == (128, frames) and np.isfinite(mel).all(), 'mel features')
        tokens = frames//100*13 + ((frames%100)+7)//8
        path = target/f'case-{index}-mel.f32'; mel.astype('<f4').tofile(path)
        cases.append(dict(frames=frames, samples=int(data.size), audio_tokens=tokens,
            audio_start=9, max_new_tokens=64, tokens=template[:9]+[151676]*tokens+template[10:],
            pcm=str(pcm), pcm_sha256=sha(pcm), mel=str(path), mel_sha256=sha(path)))
    for group in range(3): save(target/f'plan-{group}.json', dict(cases=cases[group*3:group*3+3]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    prepare_parser = sub.add_parser('prepare')
    for name in ('binary', 'decoder', 'output'): prepare_parser.add_argument('--'+name, type=Path, required=True)
    prepare_parser.add_argument('--port', type=int, default=18484)
    prepare_parser.add_argument('--continue-from', type=Path)
    prepare_parser.add_argument('--reuse-speech', type=Path)
    sub.add_parser('run').add_argument('--plan', type=Path, required=True)
    sub.add_parser('features').add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    elif args.mode == 'features': features(args.output)
    else:
        with lifecycle.child_subreaper(): Stage(read(args.plan)).run()


if __name__ == '__main__': main()
