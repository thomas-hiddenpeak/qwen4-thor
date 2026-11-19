"""Bounded actual automatic-VAD transcription, using the qualified WSS driver."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_realtime import transcription_acceptance as manual
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_service.package import require, save, sha

LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=2,
              committed_items=3, chats=1, controls=2000, seconds=600)
VAD = ROOT / '.q4t-work/serve-audio-aligner-20261117/vad-01'
WEIGHT = 'silero_vad_16k.safetensors'
DIGEST = 'c59271c284ae9c8335d795d60e0bfdb71aaaceec578d9bd9ffc1b8153c319ea1'


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed sources before freezing')
    previous = manual.WORK / 'transcription-business-04'
    require(json.loads((previous/'run/result.json').read_text())['status'] == 'PASS'
            and json.loads((previous/'lease/exit.json').read_text())['status'] == 'COMPLETED',
            'fixture source not qualified')
    plan = json.loads((previous/'run/plan.json').read_text())
    for name, digest in plan['protected'].items():
        require(sha(name) == digest, 'protected default drift')
    require(sha(VAD/WEIGHT) == DIGEST, 'fixed VAD asset identity')
    out = args.output.resolve()
    require(out.is_relative_to(manual.WORK), 'private phase directory required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    clips = {}
    for name, repeats, rate, width, quiet in [('zh', 2, 24000, 2, b'\0'),
                                             ('mulaw', 1, 8000, 1, b'\xff')]:
        source = plan['clips'][name]
        require(sha(source['path']) == source['sha256'], 'retained PCM drift')
        raw = Path(source['path']).read_bytes()
        require(len(raw) % width == 0, 'whole fixture samples')
        before = quiet * (rate * width * 600 // 1000)
        after = quiet * (rate * width * 1200 // 1000)
        audio = before + (raw + after) * repeats
        require(len(audio) <= 2 * 1024 * 1024, 'fixture byte bound')
        target = out/(name+'.pcm')
        target.write_bytes(audio)
        clips[name] = dict(path=str(target), sha256=sha(target), bytes=len(audio),
            source=source['path'], source_sha256=source['sha256'],
            utterances=repeats, rate=rate, width=width,
            prefix_silence_ms=600, inter_and_tail_silence_ms=1200,
            original_recording=source['source'],
            original_recording_sha256=source['source_sha256'])
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    policy = json.loads((previous/'run/client-policy.json').read_text())
    policy['gateway_token'] = token
    save(out/'client-policy.json', policy)
    (out/'client-policy.json').chmod(0o600)
    binary = args.binary.resolve(strict=True)
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include', 'cmake',
        'CMakeLists.txt', 'tools/serve_realtime', 'tools/serve_grammar_reap',
        'tools/serve_recovery', 'tools/serve_service', 'tools/evalscope',
        'tools/native_asr', 'tools/release'], cwd=ROOT, text=True).splitlines()
    tls = certificate(out/'credentials')
    plan.update(schema='q4t-realtime-server-vad-business-v1', limits=LIMITS,
        output=str(out), port=args.port, operations_port=args.port+1,
        public_port=args.port+2, binary=str(binary), binary_sha256=sha(binary),
        sources={n:sha(ROOT/n) for n in sources}, clips=clips,
        client_policy_sha256=sha(out/'client-policy.json'),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        tls=tls, tls_files={tls[n]:sha(tls[n]) for n in ('certificate','key')},
        vad_directory=str(VAD), vad_assets={WEIGHT:DIGEST}, reused_core_before={},
        acceptance=dict(zh_utterances_exact=3, automatic_commits_without_client_commit=True,
            continuous_pcm_items=2, g711_items=1, sample_order_and_item_identity=True,
            zero_silence_items=True, actual_usage_matches_client_settlement=True,
            same_core_pid=True, no_automatic_replay=True),
        scope='Actual continuous Silero/ASR via official JS/native WSS; fixed PID-bound admission fixture, not Guardian recovery. 29 native CPU cases reused; optional processors remain master tasks.')
    save(out/'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), source_files=len(sources))))


class Stage(manual.Stage):
    limits = LIMITS

    def __init__(self, plan):
        super().__init__(plan)
        require(plan['schema'] == 'q4t-realtime-server-vad-business-v1', 'phase schema')
        require(plan['vad_assets'] == {WEIGHT:DIGEST}, 'VAD reference identity')
        require(sha(Path(plan['vad_directory'])/WEIGHT) == DIGEST, 'VAD asset drift')

    def extra_model_args(self):
        return ['--vad-model-dir', self.plan['vad_directory']]

    def business(self):
        self.snapshot('before')
        for name, client, fmt in [('zh','alpha','audio/pcm'),
                                  ('mulaw','beta','audio/pcmu')]:
            clip = self.plan['clips'][name]
            setting = manual.settings(fmt)
            setting['audio']['input']['turn_detection'] = dict(type='server_vad',
                threshold=.5, prefix_padding_ms=300, silence_duration_ms=700)
            result = self.sdk(name, client, [dict(file=clip['path'], settings=setting,
                                                  auto_items=clip['utterances'])])
            require(len(result['turns']) == clip['utterances'], 'actual item count')
            starts = [v for v in result['events'] if v['type']=='input_audio_buffer.speech_started']
            stops = [v for v in result['events'] if v['type']=='input_audio_buffer.speech_stopped']
            commits = [v for v in result['events'] if v['type']=='input_audio_buffer.committed']
            require(len(starts)==len(stops)==len(commits)==clip['utterances'], 'VAD event count')
            previous, last_end, total = None, 0, 0
            for turn, started, stopped, committed in zip(result['turns'],starts,stops,commits):
                item = turn['item_id']
                require(item==started['item_id']==stopped['item_id']==committed['item_id'],
                        'speech/item identity differs')
                begin, end = started['audio_start_ms'], stopped['audio_end_ms']
                require(last_end <= begin < end <= clip['bytes']*1000/(clip['width']*clip['rate']),
                        'audio sample order/range')
                require(committed['previous_item_id']==previous, 'conversation item chain')
                terminal = turn['terminal']
                manual.quality(terminal['transcript'], 'zh')
                manual.usage(terminal, sdk=True)
                total += terminal['usage']['total_tokens']
                previous, last_end = item, end
            after = self.snapshot(name+'-drained')
            key = lambda n: n+'{client="'+client+'"}'
            require(after[key('q4t_client_rate_admitted_total')] == clip['utterances'] and
                    after[key('q4t_client_rate_settled_tokens_total')] == total and
                    after[key('q4t_client_rate_reserved_tokens')] == 0, 'per-VAD-item usage')
        self.core('core-after')
        self.snapshot('final-drained')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200, 'config status')
        config = json.loads(raw)
        for name in ('native_asr_health','native_vad_health'):
            health = config[name]
            require(health['state']=='ready' and health['compute_failures']==0,
                    'native audio health: '+name)
        require(config['native_vad_health']['load_attempts']==1, 'VAD should share immutable weights')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--binary',type=Path,required=True)
    prep.add_argument('--output',type=Path,required=True)
    prep.add_argument('--port',type=int,default=18328)
    run = sub.add_parser('run');run.add_argument('--plan',type=Path,required=True)
    args = parser.parse_args()
    if args.action == 'prepare': prepare(args)
    else:
        with manual.lifecycle.child_subreaper():
            Stage(json.loads(args.plan.read_text())).run()
