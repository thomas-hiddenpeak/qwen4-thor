#!/usr/bin/env python3
"""Reuse two accepted utterances; verify actual native speed changes with ASR."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import time
import wave

from acceptance import ROOT, CASES, defaults, digest
from utterance_acceptance import content_check

LIMITS = dict(seconds=1200, native_seconds=60, asr0_seconds=600,
              asr1_seconds=600, cases=6, gpu_asr_loads=2,
              gpu_talker_loads=0, gpu_codec_loads=0, main_llm_loads=0,
              cpu_model_loads=0, http_requests=0, asr_tokens_per_case=64,
              native_advances=20000)
SPEEDS = [0.25, 2.0, 4.0]
DRIVERS = ('tempo_replay.cpp', 'tempo_acceptance.py', 'utterance_acceptance.py',
           'acceptance.py')


def prepare(native, assets, output):
    # Previously qualified test-only resampling/features. No TTS recomputation.
    os.environ['USE_TORCH'] = '0'
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    import numpy as np
    import soxr
    import transformers
    from transformers import Qwen2TokenizerFast, WhisperFeatureExtractor
    assert transformers.__version__ == '4.57.6' and soxr.__version__ == '0.5.0.post1'
    extractor = WhisperFeatureExtractor.from_dict(json.loads(
        (assets / 'preprocessor_config.json').read_text()))
    tokenizer = Qwen2TokenizerFast.from_pretrained(assets, local_files_only=True)
    template = tokenizer.encode(
        '<|im_start|>system\n<|im_end|>\n<|im_start|>user\n'
        '<|audio_start|><|audio_pad|><|audio_end|><|im_end|>\n'
        '<|im_start|>assistant\n', add_special_tokens=False)
    assert template.index(151676) == 9 and template.count(151676) == 1
    output.mkdir(exist_ok=False)
    cases = []
    receipts = []
    for index in range(6):
        directory = native / f'case-{index}'
        r = json.loads((directory / 'result.json').read_text())
        expected = math.ceil((76800 if index < 3 else 74880) / SPEEDS[index % 3])
        assert r['samples'] == expected and r['sample_rate'] == 24000
        assert r['speed'] == SPEEDS[index % 3] and r['progressive_chunks'] > 0
        assert 0 < r['maximum_buffered_samples'] <= 65536
        path = directory / 'pcm.f32'
        assert path.stat().st_size == expected * 4
        pcm = np.fromfile(path, '<f4')
        assert np.isfinite(pcm).all() and float(np.max(np.abs(pcm))) <= 1
        assert float(np.linalg.norm(pcm)) > 0
        with wave.open(str(directory / 'audio.wav'), 'rb') as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(),
                    wav.getnframes()) == (24000, 1, 2, expected)
            encoded = np.frombuffer(wav.readframes(expected), '<i2').astype(np.float64) / 32768
            assert encoded.size == expected and np.max(np.abs(encoded-pcm)) <= 1 / 32768
        signal = soxr.resample(pcm, 24000, 16000, quality='HQ').astype('<f4')
        mel = extractor._np_extract_fbank_features(signal[None, :], 'cpu')[0]
        frames = signal.size // 160
        assert mel.shape == (128, frames) and frames <= 3000 and np.isfinite(mel).all()
        tokens = frames // 100 * 13 + ((frames % 100) + 7) // 8
        row = dict(frames=frames, samples=int(signal.size), audio_tokens=tokens,
                   audio_start=9, max_new_tokens=64,
                   tokens=template[:9] + [151676]*tokens + template[10:])
        for name, value in (('pcm', signal), ('mel', mel)):
            path = output / f'case-{index}-{name}.f32'
            value.astype('<f4').tofile(path)
            row[name], row[name+'_sha256'] = str(path.absolute()), digest(path)
        cases.append(row)
        receipts.append(r)
    paths = []
    for index in range(2):
        path = output / f'plan-{index}.json'
        path.write_text(json.dumps(dict(cases=cases[index*3:index*3+3]), indent=2)+'\n')
        paths.append(path)
    return paths, receipts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('binary', 'asr-binary', 'asr-assets', 'plan', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.environ.get('PYTHONOPTIMIZE')
    assert not any(k.startswith('Q4T_ABLATE_') for k in os.environ)
    plan = json.loads(args.plan.read_text())
    assert plan['schema'] == 'q4t-native-speech-tempo-v1' and plan['limits'] == LIMITS
    assert [c['speed'] for c in plan['cases']] == SPEEDS * 2
    assert [c['utterance'] for c in plan['cases']] == [0, 0, 0, 1, 1, 1]
    assert defaults() == plan['default_before']
    assert digest(args.binary) == plan['binary_sha256']
    assert digest(args.asr_binary) == plan['asr_binary_sha256']
    assert {n: digest(Path(__file__).parent / n) for n in DRIVERS} == plan['driver_sources']
    assert {n: digest(ROOT / n) for n in plan['runtime_sources']} == plan['runtime_sources']
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', limits=LIMITS, defaults_before=defaults(),
                  runs=[], plan_sha256=digest(args.plan))
    child_env = dict(os.environ)

    def run(name, command):
        remaining = LIMITS['seconds'] - (time.monotonic()-started)
        assert remaining > 0
        record = dict(name=name, command=command)
        result['runs'].append(record)
        step = time.monotonic()
        with (args.output / (name+'.log')).open('wb') as log:
            try:
                child = subprocess.run(command, cwd=ROOT, env=child_env,
                    pass_fds=(lease,), stdout=log, stderr=subprocess.STDOUT,
                    timeout=min(remaining, LIMITS[name+'_seconds']))
                record['exit_code'] = child.returncode
            finally:
                record['seconds'] = time.monotonic()-step
        assert child.returncode == 0, name+' execution failed'

    try:
        run('native', [str(args.binary.absolute()), str(args.plan.absolute()),
                       str(args.output.absolute() / 'native')])
        nr = json.loads((args.output / 'native/result.json').read_text())
        assert nr['status'] == 'PASS_TEMPO_PENDING_CONTENT' and nr['cases'] == 6
        assert nr['model_loads'] == 0 and 0 < nr['advances'] <= 20000
        paths, result['waveforms'] = prepare(args.output / 'native', args.asr_assets,
                                             args.output / 'asr-input')
        result['comparisons'] = []
        for group, path in enumerate(paths):
            run(f'asr{group}', [str(args.asr_binary.absolute()), str(args.asr_assets.absolute()),
                                str(path.absolute()), str(args.output.absolute() / f'asr{group}')])
            for i in range(3):
                directory = args.output / f'asr{group}' / f'case-{i}'
                receipt = json.loads((directory / 'result.json').read_text())
                assert receipt['eos'] is True and 0 < receipt['generated_tokens'] <= 64
                result['comparisons'].append(dict(case=group*3+i, speed=SPEEDS[i],
                    **content_check((directory / 'text.txt').read_text(), CASES[group])))
        assert all(c['passed'] for c in result['comparisons']), 'tempo actual speech content differs'
        result['status'] = 'PASS_TEMPO_PENDING_LEASE_RECONCILIATION'
    except Exception as error:
        result['status'], result['error'] = 'FAILED', str(error)
    finally:
        result['defaults_after'] = defaults()
        result['defaults_unchanged'] = result['defaults_after'] == result['defaults_before']
        if not result['defaults_unchanged']:
            result['status'], result['error'] = 'FAILED', 'protected default changed'
        result['seconds'] = time.monotonic()-started
        result['artifacts'] = {str(p.relative_to(args.output)): digest(p)
                              for p in sorted(args.output.rglob('*')) if p.is_file()}
        (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: result[k] for k in ('status', 'seconds', 'defaults_unchanged')}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    raise SystemExit(main())
