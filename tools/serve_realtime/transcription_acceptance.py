"""Bounded actual Realtime ASR/SDK/quota/cancellation under one strict GPU lease."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import traceback
import wave

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_grammar_reap import acceptance as lifecycle
from tools.serve_recovery.bundle import assets, check_assets
from tools.serve_service.package import require, save, sha
from tools.native_asr.transcription_acceptance import EXPECTED, FILES, quality, usage

WORK = ROOT / '.q4t-work/serve-realtime-20261119'
ASR = ROOT / '.q4t-work/serve-audio-native-20261117'
LIMITS = dict(starts=1, websocket_sessions=3, committed_items=5, chats=2,
              controls=2000, seconds=600)


def settings(fmt='audio/pcm'):
    value = dict(type=fmt)
    if fmt == 'audio/pcm': value['rate'] = 24000
    return dict(type='transcription', include=['item.input_audio_transcription.logprobs'],
                audio=dict(input=dict(format=value,
                    transcription=dict(model='qwen3-asr-0.6b'),
                    turn_detection=None, noise_reduction=None)))


def prepare(args):
    out = args.output.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    require(out.is_relative_to(WORK), 'private phase directory required')
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed sources before freezing')
    binary = args.binary.resolve(strict=True)
    model = Path.home()/'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
    sdk = WORK/'sdk'
    fixed = {str(p): sha(p) for p in sorted((sdk/'node_modules').rglob('*')) if p.is_file()}
    clips = {}
    for label, language, rate in [('zh', 'zh', 24000),
                                  ('en', 'en', 24000),
                                  ('mulaw', 'zh', 8000)]:
        source = ASR/'real-audio-01'/('asr_'+language+'.wav')
        target = out/(label+'.pcm')
        container = out/(label+'.wav')
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(source),
                   '-ac', '1', '-ar', str(rate), '-c:a', 'pcm_s16le',
                   '-f', 'wav', str(container)]
        with (out/(label+'-conversion.log')).open('xb') as log:
            subprocess.run(command, check=True, timeout=30, stdout=log, stderr=log,
                           env=dict(os.environ, CUDA_VISIBLE_DEVICES=''))
        require(container.stat().st_size <= 2*1024*1024, 'container bound')
        with wave.open(str(container)) as audio:
            require(audio.getnchannels() == 1 and audio.getsampwidth() == 2 and
                    audio.getframerate() == rate, 'converted audio contract')
            raw = audio.readframes(audio.getnframes())
        if label == 'mulaw':
            # Fixed local CPython 3.12 oracle, development-only, no runtime CLI.
            import audioop
            raw = audioop.lin2ulaw(raw, 2)
        target.write_bytes(raw)
        require(0 < target.stat().st_size <= 2*1024*1024, 'audio fixture bound')
        clips[label] = dict(path=str(target), sha256=sha(target), bytes=target.stat().st_size,
                           source=str(source), source_sha256=sha(source), command=command)
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    policy = dict(version=3, revision=1, gateway_token=token, fair_queue=True,
        clients=[dict(id=name, max_inflight=1, max_control_inflight=2,
            max_prompt_tokens=4096, max_output_tokens=128, max_body_bytes=1048576,
            requests_per_minute=2 if name == 'alpha' else 10,
            tokens_per_minute=6000) for name in ('alpha', 'beta', 'gamma')])
    save(out/'client-policy.json', policy)
    (out/'client-policy.json').chmod(0o600)
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include', 'cmake',
        'CMakeLists.txt', 'tools/serve_realtime', 'tools/serve_grammar_reap',
        'tools/serve_recovery', 'tools/serve_service', 'tools/evalscope',
        'tools/native_asr', 'tools/release'], cwd=ROOT, text=True).splitlines()
    protected = [ROOT/p for p in ('build/q4t', 'build/q4t.release.json',
        'build/service/manifest.json', 'build/service.release.json',
        'build/service-native-state/ledger.json')]
    plan = dict(schema='q4t-realtime-transcription-business-v1', limits=LIMITS,
        output=str(out), port=args.port, operations_port=args.port+1,
        binary=str(binary), binary_sha256=sha(binary), model=str(model),
        model_assets=assets(model), asr_directory=str(ASR/'assets-01'),
        asr_assets={n: sha(ASR/'assets-01'/n) for n in FILES},
        sources={n: sha(ROOT/n) for n in sources}, protected={str(p):sha(p) for p in protected},
        sdk_directory=str(sdk), sdk_files=fixed,
        sdk_versions=dict(openai='7.47.0', ws='8.21.0'), clips=clips,
        fixture_tools={str(Path('/usr/bin/ffmpeg')):sha('/usr/bin/ffmpeg'),
                       str(Path(audioop.__file__)):sha(audioop.__file__)},
        client_policy_sha256=sha(out/'client-policy.json'),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        expected=EXPECTED, acceptance=dict(zh_exact=True,en_word_error_rate_max=0.05,
            actual_usage_matches_client_settlement=True, third_alpha_item='client_rpm_exhausted',
            disconnect_after_real_delta=True, same_core_pid=True, no_automatic_replay=True),
        scope='Direct native backend + official JS SDK. Native TLS/Guardian composition remains a subsequent task; no performance or full-model quality claim.')
    save(out/'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), source_files=len(sources))))


class Stage(lifecycle.Stage):
    limits = LIMITS

    def __init__(self, plan):
        self.plan, self.out = plan, Path(plan['output'])
        self.started = time.monotonic()
        self.counts = {k: 0 for k in LIMITS if k != 'seconds'}
        self.model = self.model_identity = self.fault_thread = None
        self.history = {}
        self.lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
        lifecycle.verify_lease(self.lease, lifecycle.guard.common_lock())
        require(plan['limits'] == LIMITS and plan['expected'] == EXPECTED, 'plan drift')
        for name, digest in plan['sources'].items(): require(sha(ROOT/name) == digest, 'source drift')
        for name, digest in plan['protected'].items(): require(sha(name) == digest, 'default drift')
        for name, digest in plan['sdk_files'].items(): require(sha(name) == digest, 'SDK drift')
        for name, digest in plan['asr_assets'].items():
            require(sha(Path(plan['asr_directory'])/name) == digest, 'ASR asset drift')
        for row in plan['clips'].values():
            require(sha(row['path']) == row['sha256'] and sha(row['source']) == row['source_sha256'], 'audio drift')
        require(sha(plan['binary']) == plan['binary_sha256'], 'binary drift')
        require(sha(self.out/'client-policy.json') == plan['client_policy_sha256'], 'policy drift')
        check_assets(plan['model'], plan['model_assets'])
        self.token = json.loads((self.out/'client-policy.json').read_text())['gateway_token']

    def budget(self, kind, amount=1):
        require(time.monotonic()-self.started < LIMITS['seconds']-40, 'phase deadline')
        require(self.counts[kind]+amount <= LIMITS[kind], 'attempt limit: '+kind)
        self.counts[kind] += amount
        save(self.out/'counts.json', self.counts)

    def start(self):
        for port in (self.plan['port'], self.plan['operations_port']):
            with socket.socket() as sock: sock.bind(('127.0.0.1', port))
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '64', '--max-inflight-requests', '8',
            '--max-queued-requests', '3', '--asr-model-dir', self.plan['asr_directory'],
            '--asr-max-output-tokens', '128', '--client-policy-file', str(self.out/'client-policy.json')]
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
        else: raise TimeoutError('readiness')

    def same_model(self):
        current = lifecycle.guard.processes().get(self.model.pid, {})
        require(self.model.poll() is None and
                current.get('start_ticks') == self.model_identity['start_ticks'],
                'owned realtime model exited or changed; inspect retained model.log')

    def core(self, label):
        self.budget('chats')
        conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=120)
        try:
            conn.request('POST', '/v1/chat/completions', json.dumps(dict(
                model='qwen3.8-flash-next', temperature=0, enable_thinking=False,
                max_completion_tokens=32, messages=[dict(role='user', content=
                    'Compute 17 + 25. Reply only with the decimal number.')])),
                headers={'Content-Type':'application/json', 'X-Q4T-Client-Id':'gamma',
                         'X-Q4T-Gateway-Token':self.token})
            response = conn.getresponse(); raw = response.read(1048577)
            require(len(raw) <= 1048576, 'core response bound')
            (self.out/(label+'.wire')).write_bytes(raw)
            require(response.status == 200, 'core status')
            value = json.loads(raw)['choices'][0]
            require(value['finish_reason'] == 'stop' and value['message']['content'].strip() == '42', 'core quality')
        finally: conn.close()
        self.same_model()

    def snapshot(self, label):
        end = time.monotonic()+15
        while time.monotonic() < end:
            self.same_model()
            status, raw = self.http('/metrics')
            require(status == 200, 'metrics')
            values = lifecycle.parse_metrics(raw)
            if all(values[k] == 0 for k in ('q4t_active_chats', 'q4t_realtime_sessions',
                       'q4t_realtime_host_reserved_bytes')) and values['q4t_seq_slots_free'] == 1:
                save(self.out/(label+'.json'), values)
                return values
            time.sleep(.02)
        raise TimeoutError('request/audio/session resources did not drain')

    def sdk(self, label, client, clips, probes=False):
        self.budget('websocket_sessions')
        self.budget('committed_items', len(clips))
        args = dict(base_url=f"http://127.0.0.1:{self.plan['port']}/v1",
            headers={'X-Q4T-Client-Id':client, 'X-Q4T-Gateway-Token':self.token},
            clips=clips, probes=probes)
        env = {k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR'] = self.plan['sdk_directory']
        run = subprocess.run(['node', str(ROOT/'tools/serve_realtime/transcription_sdk.mjs')],
            input=json.dumps(args), text=True, capture_output=True, env=env, timeout=135)
        (self.out/(label+'.sdk.json')).write_text(run.stdout)
        (self.out/(label+'.stderr')).write_text(run.stderr)
        require(run.returncode == 0, 'SDK failed: '+label)
        result = json.loads(run.stdout)
        require(result['status'] == 'PASS' and result['connections'] == 1 and
                not result['automatic_replay'], 'SDK boundary')
        self.same_model()
        return result

    def business(self):
        self.core('core-before')
        clips = self.plan['clips']
        normal = lambda name: dict(file=clips[name]['path'], settings=settings())
        before = self.snapshot('before')
        result = self.sdk('alpha', 'alpha', [normal('zh'), normal('en'),
            dict(normal('zh'), expect_failure='client_rpm_exhausted')], probes=True)
        for index, language in enumerate(('zh','en')):
            terminal = result['turns'][index]['terminal']
            quality(terminal['transcript'], language)
            usage(terminal, sdk=True)
        after = self.snapshot('alpha-drained')
        key = lambda name: name+'{client="alpha"}'
        actual = sum(v['terminal']['usage']['total_tokens'] for v in result['turns'][:2])
        require(after[key('q4t_client_rate_admitted_total')] == 2 and
            after[key('q4t_client_rate_settled_tokens_total')] == actual and
            after[key('q4t_client_rate_reserved_tokens')] == 0 and
            after['q4t_client_rate_rejected_total{client="alpha",kind="rpm"}'] == 1,
            'per-item RPM/actual TPM settlement')
        self.sdk('cancel', 'beta', [dict(normal('en'), cancel_after_delta=True)])
        cancelled = self.snapshot('cancel-drained')
        require(cancelled['q4t_requests_aborted_total'] == before['q4t_requests_aborted_total']+1,
                'actual cancellation not recorded')
        result = self.sdk('mulaw', 'beta', [dict(file=clips['mulaw']['path'],
                                                settings=settings('audio/pcmu'))])
        terminal = result['turns'][0]['terminal']
        quality(terminal['transcript'], 'zh'); usage(terminal, sdk=True)
        final = self.snapshot('final-drained')
        require(final['q4t_client_rate_reserved_tokens{client="beta"}'] == 0,
                'cancelled client leaked reservation')
        self.core('core-after')
        self.snapshot('core-drained')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200, 'final configuration')
        health = json.loads(raw)['native_asr_health']
        require(health['state'] == 'ready' and health['load_attempts'] <= 2 and
                health['compute_failures'] == 0, 'ASR health/reload bound')

    def run(self):
        result = dict(status='FAIL')
        previous = signal.getsignal(signal.SIGALRM)
        def expired(_signal, _frame): raise TimeoutError('stage deadline')
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(.01, LIMITS['seconds']-40-(time.monotonic()-self.started)))
        try:
            self.start(); self.business(); result['status'] = 'PASS'
        except BaseException:
            result['error'] = traceback.format_exc(); raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0); signal.signal(signal.SIGALRM, previous)
            try:
                self.cleanup()
                require(all(sha(n)==h for n,h in self.plan['protected'].items()), 'default changed')
                result['default_unchanged'] = True
            except BaseException:
                result['status']='FAIL';result['cleanup_error']=traceback.format_exc();raise
            finally:
                result.update(counts=self.counts, seconds=time.monotonic()-self.started,
                              binary_sha256=self.plan['binary_sha256'])
                save(self.out/'result.json', result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare');prep.add_argument('--binary',type=Path,required=True)
    prep.add_argument('--output',type=Path,required=True);prep.add_argument('--port',type=int,default=18318)
    run = sub.add_parser('run');run.add_argument('--plan',type=Path,required=True)
    args = parser.parse_args()
    if args.action == 'prepare': prepare(args)
    else:
        with lifecycle.child_subreaper(): Stage(json.loads(args.plan.read_text())).run()
