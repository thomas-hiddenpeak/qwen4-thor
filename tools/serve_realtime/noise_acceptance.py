"""Bounded real speech with labeled stationary noise, using native WSS/ASR."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_realtime import vad_acceptance as vad
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_service.package import require, save, sha

LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=3,
              committed_items=7, chats=1, controls=2000, seconds=600)
MODES = ('off', 'near_field', 'far_field')


def rms(values):
    return math.sqrt(sum(v * v for v in values) / len(values))


def make_clip(source, name, out):
    import audioop  # Fixed local development oracle; not a runtime dependency.
    require(sha(source['path']) == source['sha256'], 'retained audio drift')
    raw = Path(source['path']).read_bytes()
    mulaw = name == 'mulaw'
    rate, width = (8000, 1) if mulaw else (24000, 2)
    linear = audioop.ulaw2lin(raw, 2) if mulaw else raw
    samples = [v[0] / 32768 for v in struct.iter_unpack('<h', linear)]
    scale = min(1., .5 / max(abs(v) for v in samples))
    clean = [v * scale for v in samples]
    # Fixed 10 dB speech-RMS / stationary-hum-RMS. No tuning after ASR results.
    amplitude = rms(clean) * math.sqrt(2) * 10 ** (-10 / 20)
    padded = [0.] * (2 * rate) + clean + [0.] * (rate * 1200 // 1000)
    mixed = [v + amplitude * math.sin(2 * math.pi * 125 * i / rate)
             for i, v in enumerate(padded)]
    require(max(abs(v) for v in mixed) < 1, 'fixture must not clip')
    encoded = b''.join(struct.pack('<h', round(v * 32767)) for v in mixed)
    if mulaw:
        encoded = audioop.lin2ulaw(encoded, 2)
    require(0 < len(encoded) <= 2 * 1024 * 1024, 'fixture byte bound')
    target = out / (name + '.pcm')
    target.write_bytes(encoded)
    return dict(path=str(target), sha256=sha(target), bytes=len(encoded),
        source=source['path'], source_sha256=source['sha256'], rate=rate,
        width=width, original_recording=source['source'],
        original_recording_sha256=source['source_sha256'], clean_scale=scale,
        hum_peak=amplitude, target_snr_db=10, hum_hz=125,
        prefix_noise_ms=2000, tail_noise_ms=1200,
        codec_reference=str(Path(audioop.__file__).resolve()),
        codec_reference_sha256=sha(audioop.__file__))


def waveform_probe(binary, clip, name, out):
    results = {}
    for mode in MODES:
        target = out / (name + '-' + mode + '.f32')
        command = [str(binary), mode, 'mulaw' if name == 'mulaw' else 'pcm',
                   clip['path'], str(target)]
        run = subprocess.run(command, capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, CUDA_VISIBLE_DEVICES=''))
        save(out / (name + '-' + mode + '-capture.json'), dict(command=command,
             exit_code=run.returncode, stdout=run.stdout, stderr=run.stderr))
        require(run.returncode == 0, 'native waveform capture failed')
        values = [v[0] for v in struct.iter_unpack('<f', target.read_bytes())]
        require(len(values) == clip['bytes'] // clip['width'] * 16000 // clip['rate'],
                'actual processed sample extent')
        require(all(math.isfinite(v) and abs(v) <= 1 for v in values),
                'processed waveform finite normalized contract')
        results[mode] = dict(path=str(target), sha256=sha(target), samples=len(values),
                            quiet_rms=rms(values[19200:28800]))
    original = results['off']['quiet_rms']
    require(original > 0, 'positive actual noise')
    for mode, maximum in [('near_field', .5), ('far_field', .65)]:
        results[mode]['quiet_rms_ratio'] = results[mode]['quiet_rms'] / original
        require(results[mode]['quiet_rms_ratio'] < maximum,
                'frozen stationary noise reduction gate: ' + name + '/' + mode)
        require(results[mode]['sha256'] != results['off']['sha256'], 'no-op processor')
    require(results['near_field']['quiet_rms'] < results['far_field']['quiet_rms'],
            'near/far policy must differ')
    return results


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed sources before freezing')
    previous = base.WORK / 'vad-business-01'
    require(json.loads((previous / 'run/result.json').read_text())['status'] == 'PASS'
            and json.loads((previous / 'lease/exit.json').read_text())['status'] == 'COMPLETED',
            'previous native composition not qualified')
    plan = json.loads((previous / 'run/plan.json').read_text())
    original = json.loads((base.WORK / 'transcription-business-04/run/plan.json').read_text())
    for name, digest in plan['protected'].items():
        require(sha(name) == digest, 'protected default drift')
    require(sha(vad.VAD / vad.WEIGHT) == vad.DIGEST, 'fixed VAD asset drift')
    out = args.output.resolve()
    require(out.is_relative_to(base.WORK), 'private phase directory required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    save(out / 'preparation-plan.json', dict(native_cpu_waveform_captures=9,
        gpu_actions=0, seconds_per_capture=30, limits=LIMITS,
        scene='Unmodified retained real Chinese/English source, labeled 125 Hz hum at 10 dB SNR; 2s prefix/1.2s tail. Fixed before any waveform or model result.',
        signal_gate='1.2-1.8s noise-only RMS near<.5 original, far<.65 original, near<far; exact sample count; finite waveform. Not general denoising quality.',
        quality_gate='Original Chinese exact and English WER<=.05 for all three settings; one Chinese G.711 near-field auto turn.'))
    clips = {name: make_clip(original['clips'][name], name, out)
             for name in ('zh', 'en', 'mulaw')}
    probe = args.probe.resolve(strict=True)
    waves = {name: waveform_probe(probe, clip, name, out) for name, clip in clips.items()}
    save(out / 'waveforms.json', waves)
    policy = json.loads((previous / 'run/client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    for client in policy['clients']:
        client['requests_per_minute'] = 10
    save(out / 'client-policy.json', policy)
    (out / 'client-policy.json').chmod(0o600)
    binary = args.binary.resolve(strict=True)
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include', 'cmake',
        'CMakeLists.txt', 'tools/serve_realtime', 'tools/serve_grammar_reap',
        'tools/serve_recovery', 'tools/serve_service', 'tools/evalscope',
        'tools/native_asr', 'tools/release'], cwd=ROOT, text=True).splitlines()
    tls = certificate(out / 'credentials')
    plan.update(schema='q4t-realtime-noise-business-v1', limits=LIMITS,
        output=str(out), port=args.port, operations_port=args.port + 1,
        public_port=args.port + 2, binary=str(binary), binary_sha256=sha(binary),
        sources={n: sha(ROOT / n) for n in sources}, clips=clips,
        client_policy_sha256=sha(out / 'client-policy.json'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        tls=tls, tls_files={tls[n]: sha(tls[n]) for n in ('certificate', 'key')},
        reused_core_before={}, waveforms=waves,
        probe=dict(path=str(probe), sha256=sha(probe)),
        acceptance=dict(zh_exact=True, en_word_error_rate_max=.05,
            modes=list(MODES), auto_items=2, manual_items=5,
            actual_usage_matches_client_settlement=True, same_core_pid=True),
        scope='Real stationary-noise speech over official JS/native WSS/ASR; optional processor/same-PID isolation. Fixed admission fixture, not Guardian recovery, listening or broad speech-quality evaluation.')
    save(out / 'plan.json', plan)
    print(json.dumps(dict(plan=str(out / 'plan.json'), waveform_captures=9)))


class Stage(base.Stage):
    limits = LIMITS

    def __init__(self, plan):
        super().__init__(plan)
        require(plan['schema'] == 'q4t-realtime-noise-business-v1', 'phase schema')
        require(sha(Path(plan['vad_directory']) / vad.WEIGHT) == vad.DIGEST,
                'fixed VAD asset drift')

    def extra_model_args(self):
        return ['--vad-model-dir', self.plan['vad_directory']]

    def business(self):
        self.snapshot('before')
        quality = {}
        for name, client in [('zh', 'alpha'), ('en', 'beta'), ('mulaw', 'gamma')]:
            clip = self.plan['clips'][name]
            modes = MODES if name != 'mulaw' else ('near_field',)
            rows = []
            for mode in modes:
                settings = base.settings('audio/pcmu' if name == 'mulaw' else 'audio/pcm')
                settings['audio']['input']['noise_reduction'] = (
                    None if mode == 'off' else dict(type=mode))
                auto = name in ('zh', 'mulaw') and mode == 'near_field'
                row = dict(file=clip['path'], settings=settings)
                if auto:
                    settings['audio']['input']['turn_detection'] = dict(type='server_vad',
                        threshold=.5, prefix_padding_ms=300, silence_duration_ms=700)
                    row['auto_items'] = 1
                rows.append(row)
            result = self.sdk(name, client, rows)
            require(len(result['turns']) == len(rows), 'actual turn count')
            total = 0
            for mode, row, turn in zip(modes, rows, result['turns']):
                actual = turn['update']['session']['audio']['input']
                require(actual['noise_reduction'] == row['settings']['audio']['input']['noise_reduction'],
                        'effective noise setting')
                terminal = turn['terminal']
                quality[name + '/' + mode] = base.quality(terminal['transcript'],
                                                        'en' if name == 'en' else 'zh')
                base.usage(terminal, sdk=True)
                total += terminal['usage']['total_tokens']
                if row.get('auto_items'):
                    starts = [e for e in result['events'] if
                              e['type'] == 'input_audio_buffer.speech_started' and
                              e['item_id'] == turn['item_id']]
                    stops = [e for e in result['events'] if
                             e['type'] == 'input_audio_buffer.speech_stopped' and
                             e['item_id'] == turn['item_id']]
                    require(len(starts) == len(stops) == 1, 'automatic noise item identity')
                    require(1000 <= starts[0]['audio_start_ms'] < stops[0]['audio_end_ms'] <=
                            clip['bytes'] * 1000 / (clip['width'] * clip['rate']),
                            'noise-only prefix must not generate a speech item')
            state = self.snapshot(name + '-drained')
            key = lambda metric: metric + '{client="' + client + '"}'
            require(state[key('q4t_client_rate_admitted_total')] == len(rows) and
                    state[key('q4t_client_rate_settled_tokens_total')] == total and
                    state[key('q4t_client_rate_reserved_tokens')] == 0,
                    'per-item actual settlement')
        save(self.out / 'quality.json', quality)
        self.core('core-after')
        self.snapshot('final-drained')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200, 'config status')
        config = json.loads(raw)
        for name in ('native_asr_health', 'native_vad_health'):
            require(config[name]['state'] == 'ready' and
                    config[name]['compute_failures'] == 0, 'optional audio health')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--binary', type=Path, required=True)
    prep.add_argument('--probe', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--port', type=int, default=18338)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Stage(json.loads(args.plan.read_text())).run()
