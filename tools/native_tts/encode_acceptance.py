#!/usr/bin/env python3
"""Fixed real-speech codec roundtrip and independent ASR content gate."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

from acceptance import ROOT, CASES, defaults, digest
from utterance_acceptance import content_check

LIMITS = dict(seconds=1200, native_seconds=60, asr_seconds=600,
              cases=8, gpu_asr_loads=3, gpu_talker_loads=0, gpu_codec_loads=0,
              main_llm_loads=0, http_requests=0, cpu_codec_workers=16,
              asr_tokens_per_case=64, artifact_mib=24, disk_headroom_mib=1024)
FORMATS = ['mp3', 'aac', 'opus', 'flac']
DRIVERS = ['encode_replay.cpp', 'encode_acceptance.py',
           'utterance_acceptance.py', 'acceptance.py']


def prepare(native, assets, output):
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    os.environ['USE_TORCH'] = '0'
    import numpy as np
    import transformers
    from transformers import Qwen2TokenizerFast, WhisperFeatureExtractor
    assert transformers.__version__ == '4.57.6'
    tokenizer = Qwen2TokenizerFast.from_pretrained(assets, local_files_only=True)
    extractor = WhisperFeatureExtractor.from_dict(json.loads(
        (assets/'preprocessor_config.json').read_text()))
    template = tokenizer.encode(
        '<|im_start|>system\n<|im_end|>\n<|im_start|>user\n'
        '<|audio_start|><|audio_pad|><|audio_end|><|im_end|>\n'
        '<|im_start|>assistant\n', add_special_tokens=False)
    assert template.index(151676) == 9 and template.count(151676) == 1
    output.mkdir(exist_ok=False)
    cases = []
    for index in range(8):
        directory = native/f'case-{index}'
        record = json.loads((directory/'result.json').read_text())
        pcm_path = directory/'pcm.f32'
        signal = np.fromfile(pcm_path, '<f4')
        assert signal.size == record['samples'] <= 60000
        assert record['sample_rate'] == 16000
        assert record['worker_starts'] == record['worker_reaped'] == 2
        assert np.isfinite(signal).all() and float(np.linalg.norm(signal)) > 0
        # Full decoded PCM, including actual codec priming and tail. No trim,
        # hint text, target-language prompt or change to the accepted ASR.
        mel = extractor._np_extract_fbank_features(signal[None, :], 'cpu')[0]
        frames = signal.size//160
        assert mel.shape == (128, frames) and np.isfinite(mel).all()
        tokens = frames//100*13 + ((frames % 100)+7)//8
        path = output/f'case-{index}-mel.f32'
        mel.astype('<f4').tofile(path)
        cases.append(dict(frames=frames, samples=int(signal.size),
            audio_tokens=tokens, audio_start=9, max_new_tokens=64,
            tokens=template[:9]+[151676]*tokens+template[10:],
            pcm=str(pcm_path.absolute()), pcm_sha256=digest(pcm_path),
            mel=str(path.absolute()), mel_sha256=digest(path)))
    paths = []
    for group in range(3):
        path = output/f'plan-{group}.json'
        path.write_text(json.dumps(dict(cases=cases[group*3:group*3+3]), indent=2)+'\n')
        paths.append(path)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('binary', 'asr-binary', 'asr-assets', 'plan', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.environ.get('PYTHONOPTIMIZE')
    assert not any(k.startswith('Q4T_ABLATE_') for k in os.environ)
    plan = json.loads(args.plan.read_text())
    assert plan['schema'] == 'q4t-speech-encoding-content-v1' and plan['limits'] == LIMITS
    assert [c['format'] for c in plan['cases']] == FORMATS*2
    assert [c['utterance'] for c in plan['cases']] == [0]*4+[1]*4
    assert defaults() == plan['default_before']
    assert digest(args.binary) == plan['binary_sha256']
    assert digest(args.asr_binary) == plan['asr_binary_sha256']
    assert {n: digest(Path(__file__).parent/n) for n in DRIVERS} == plan['driver_sources']
    assert {n: digest(ROOT/n) for n in plan['runtime_sources']} == plan['runtime_sources']
    assert shutil.disk_usage(ROOT).free >= (LIMITS['disk_headroom_mib']+LIMITS['artifact_mib'])*1048576
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', limits=LIMITS, runs=[], comparisons=[],
                  defaults_before=defaults(), plan_sha256=digest(args.plan))
    child_env = dict(os.environ)

    def run(name, command, cap):
        step = time.monotonic()
        record = dict(name=name, command=command)
        result['runs'].append(record)
        with (args.output/(name+'.log')).open('wb') as log:
            child = subprocess.run(command, cwd=ROOT, env=child_env, pass_fds=(lease,),
                stdout=log, stderr=subprocess.STDOUT,
                timeout=min(cap, LIMITS['seconds']-(time.monotonic()-started)))
        record.update(exit_code=child.returncode, seconds=time.monotonic()-step)
        assert child.returncode == 0, name+' failed'
        assert sum(p.stat().st_size for p in args.output.rglob('*') if p.is_file()) <= LIMITS['artifact_mib']*1048576
        assert shutil.disk_usage(ROOT).free >= LIMITS['disk_headroom_mib']*1048576

    try:
        run('native', [str(args.binary.absolute()), '--speech-encode-replay',
            str(args.plan.absolute()), str(args.output.absolute()/'native')], LIMITS['native_seconds'])
        record = json.loads((args.output/'native/result.json').read_text())
        assert record['status'] == 'PASS_ENCODING_PENDING_CONTENT' and record['cases'] == 8
        assert record['gpu_model_loads'] == 0
        assert record['worker_starts'] == record['worker_reaped'] == 16
        paths = prepare(args.output/'native', args.asr_assets, args.output/'asr-input')
        for group, path in enumerate(paths):
            run(f'asr-{group}', [str(args.asr_binary.absolute()), str(args.asr_assets.absolute()),
                str(path.absolute()), str(args.output.absolute()/f'asr-{group}')], LIMITS['asr_seconds'])
            for i in range(min(3, 8-group*3)):
                index = group*3+i
                directory = args.output/f'asr-{group}'/f'case-{i}'
                receipt = json.loads((directory/'result.json').read_text())
                text = (directory/'text.txt').read_text()
                try:
                    check = content_check(text, CASES[index//4])
                except AssertionError as error:
                    check = dict(expected=CASES[index//4]['text'], transcript=text,
                                 passed=False, error=str(error))
                check['passed'] = check['passed'] and receipt['eos'] and 0 < receipt['generated_tokens'] <= 64
                result['comparisons'].append(dict(case=index, format=FORMATS[index%4],
                    utterance=index//4, eos=receipt['eos'], **check))
        assert all(c['passed'] for c in result['comparisons']), 'compressed speech content differs'
        result['status'] = 'PASS_ENCODING_CONTENT_PENDING_LEASE'
    except Exception as error:
        result.update(status='FAILED', error=str(error))
    finally:
        result['defaults_unchanged'] = defaults() == result['defaults_before']
        if not result['defaults_unchanged']:
            result.update(status='FAILED', error='protected default changed')
        result['seconds'] = time.monotonic()-started
        result['artifacts'] = {str(p.relative_to(args.output)): digest(p)
                              for p in sorted(args.output.rglob('*')) if p.is_file()}
        (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k: result[k] for k in ('status', 'seconds', 'defaults_unchanged')}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    raise SystemExit(main())
