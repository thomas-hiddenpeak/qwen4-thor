#!/usr/bin/env python3
"""Two complete utterances with natural EOS and independent ASR content check."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import time
import unicodedata
import wave

try:
    from .acceptance import ROOT, CASES, defaults, digest
except ImportError:
    from acceptance import ROOT, CASES, defaults, digest

LIMITS = dict(seconds=1200, native_seconds=600, asr_seconds=600,
              cases=2, max_frames_per_case=160, generation_advances=48000,
              codec_advances=4096, asr_tokens_per_case=128,
              gpu_talker_loads=1, gpu_codec_loads=1, gpu_asr_loads=1,
              cpu_model_loads=0, main_llm_loads=0, http_requests=0)
CONTRACT = dict(natural_eos=True, max_frames_exclusive=160,
                exact_normalized_transcription=True, minimum_rms=0.0001,
                minimum_peak=0.01, sample_rate=24000, samples_per_frame=1920,
                expressive_style_qualified=False)
DRIVERS = ('utterance_replay.cpp', 'utterance_acceptance.py', 'acceptance.py')


def normalize(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    return ''.join(c for c in value if c.isalnum())


def content_check(value, case):
    prefix = 'language ' + case['language'].capitalize() + '<asr_text>'
    assert value.startswith(prefix), 'wrong ASR language or missing transcript marker'
    transcript = value[len(prefix):]
    assert '<' not in transcript and '>' not in transcript
    return dict(expected=case['text'], transcript=transcript,
                passed=bool(normalize(case['text']) and
                            normalize(transcript) == normalize(case['text'])))


def validate_plan(plan):
    assert plan['schema'] == 'q4t-native-tts-utterance-v1'
    assert plan['limits'] == LIMITS and plan['contract'] == CONTRACT
    assert plan['cases'] == CASES


def validate_wave(path, record):
    import numpy as np
    assert record['eos'] is True and record['at_limit'] is False
    frames = record['frames']
    assert type(frames) is int and 2 <= frames < 160
    assert record['samples'] == frames * 1920 and record['sample_rate'] == 24000
    assert path.stat().st_size == frames * 1920 * 4
    pcm = np.fromfile(path, '<f4')
    assert np.isfinite(pcm).all() and np.max(np.abs(pcm)) <= 1
    assert float(np.sqrt(np.mean(pcm.astype(np.float64)**2))) > 0.0001
    assert float(np.max(np.abs(pcm))) > 0.01
    return pcm


def asr_fixture(native, assets, output):
    # Test-only independently qualified CPU preprocessing; no runtime dependency.
    os.environ['USE_TORCH'] = '0'
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    import numpy as np
    import soxr
    import transformers
    from transformers import Qwen2TokenizerFast, WhisperFeatureExtractor
    assert transformers.__version__ == '4.57.6' and soxr.__version__ == '0.5.0.post1'
    tokenizer = Qwen2TokenizerFast.from_pretrained(assets, local_files_only=True)
    extractor = WhisperFeatureExtractor.from_dict(json.loads(
        (assets / 'preprocessor_config.json').read_text()))
    output.mkdir(exist_ok=False)
    prompt = ('<|im_start|>system\n<|im_end|>\n<|im_start|>user\n'
              '<|audio_start|><|audio_pad|><|audio_end|><|im_end|>\n'
              '<|im_start|>assistant\n')
    template = tokenizer.encode(prompt, add_special_tokens=False)
    start = template.index(151676)
    assert start == 9 and template.count(151676) == 1
    rows = []
    for index in range(2):
        source = native / f'case-{index}'
        record = json.loads((source / 'result.json').read_text())
        pcm = validate_wave(source / 'pcm.f32', record)
        with wave.open(str(source / 'audio.wav'), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(np.rint(pcm * 32767).astype('<i2').tobytes())
        # ASR consumes the full float waveform; no silence trim or normalization.
        signal = soxr.resample(pcm, 24000, 16000, quality='HQ').astype('<f4')
        assert 0 < signal.size < 204800 and np.isfinite(signal).all()
        mel = extractor._np_extract_fbank_features(signal[None, :], 'cpu')[0]
        frames = signal.size // 160
        assert mel.shape == (128, frames) and np.isfinite(mel).all()
        tokens = frames // 100 * 13 + ((frames % 100) + 7) // 8
        row = dict(frames=frames, samples=int(signal.size), audio_tokens=tokens,
                   audio_start=start, max_new_tokens=128,
                   tokens=template[:start] + [151676] * tokens + template[start+1:])
        for name, value in (('pcm', signal), ('mel', mel)):
            path = output / f'case-{index}-{name}.f32'
            value.astype('<f4').tofile(path)
            row[name], row[name + '_sha256'] = str(path.absolute()), digest(path)
        rows.append(row)
    plan = dict(cases=rows, model_loads=0, full_audio=True, numpy=np.__version__,
                transformers=transformers.__version__, soxr=soxr.__version__)
    path = output / 'plan.json'
    path.write_text(json.dumps(plan, indent=2)+'\n')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('binary', 'asr-binary', 'assets', 'asr-assets', 'plan', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.environ.get('PYTHONOPTIMIZE')
    assert not any(k.startswith('Q4T_ABLATE_') for k in os.environ)
    plan = json.loads(args.plan.read_text())
    validate_plan(plan)
    assert defaults() == plan['default_before']
    assert digest(args.binary) == plan['binary_sha256']
    assert digest(args.asr_binary) == plan['asr_binary_sha256']
    assert {n: digest(Path(__file__).parent / n) for n in DRIVERS} == plan['driver_sources']
    assert {n: digest(ROOT / n) for n in plan['runtime_sources']} == plan['runtime_sources']
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', limits=LIMITS, contract=CONTRACT,
                  defaults_before=defaults(), runs=[], plan_sha256=digest(args.plan))
    # Capture before CPU feature generation temporarily hides CUDA from Python.
    child_env = dict(os.environ)

    def run(name, command):
        remaining = LIMITS['seconds'] - (time.monotonic() - started)
        assert remaining > 0
        record = dict(name=name, command=command)
        result['runs'].append(record)
        step = time.monotonic()
        with (args.output / (name + '.log')).open('wb') as log:
            try:
                child = subprocess.run(command, cwd=ROOT, env=child_env,
                    pass_fds=(lease,), stdout=log, stderr=subprocess.STDOUT,
                    timeout=min(remaining, LIMITS[name + '_seconds']))
                record['exit_code'] = child.returncode
            finally:
                record['seconds'] = time.monotonic() - step
        assert child.returncode == 0, name + ' execution failed'

    try:
        run('native', [str(args.binary.absolute()), str(args.assets.absolute()),
                      str(args.plan.absolute()), str(args.output.absolute() / 'native')])
        nr = json.loads((args.output / 'native/result.json').read_text())
        assert nr['status'] == 'PASS_UTTERANCE_PENDING_CONTENT_CHECK'
        assert nr['gpu_model_loads'] == nr['cases'] == 2
        assert 4 <= nr['frames'] < 320
        assert 0 < nr['generation_advances'] <= 48000
        assert 0 < nr['codec_advances'] <= 4096
        prepared = asr_fixture(args.output / 'native', args.asr_assets,
                               args.output / 'asr-input')
        run('asr', [str(args.asr_binary.absolute()), str(args.asr_assets.absolute()),
                   str(prepared.absolute()), str(args.output.absolute() / 'asr')])
        checks = []
        result['comparisons'] = checks
        for index, case in enumerate(CASES):
            directory = args.output / 'asr' / f'case-{index}'
            receipt = json.loads((directory / 'result.json').read_text())
            assert receipt['eos'] is True and 0 < receipt['generated_tokens'] <= 128
            checks.append(dict(case=index, **content_check(
                (directory / 'text.txt').read_text(), case)))
        assert all(c['passed'] for c in checks), 'actual speech content differs'
        result['native'] = nr
        result['status'] = 'PASS_UTTERANCE_PENDING_LEASE_RECONCILIATION'
    except Exception as error:
        result['status'], result['error'] = 'FAILED', str(error)
    finally:
        result['defaults_after'] = defaults()
        result['defaults_unchanged'] = result['defaults_after'] == result['defaults_before']
        if not result['defaults_unchanged']:
            result['status'], result['error'] = 'FAILED', 'protected default changed'
        result['seconds'] = time.monotonic() - started
        result['artifacts'] = {str(p.relative_to(args.output)): digest(p)
                              for p in sorted(args.output.rglob('*')) if p.is_file()}
        (args.output / 'result.json').write_text(json.dumps(result,
            ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: result[k] for k in ('status', 'seconds', 'defaults_unchanged')}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    raise SystemExit(main())
