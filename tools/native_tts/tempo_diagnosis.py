#!/usr/bin/env python3
"""Fixed reference/slow-back diagnosis; never grants tempo acceptance."""
import argparse
import ctypes as ct
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from acceptance import ROOT, CASES, defaults, digest
from utterance_acceptance import content_check

LIMITS = dict(seconds=1200, gpu_asr_loads=2, cases=6,
              gpu_talker_loads=0, gpu_codec_loads=0, main_llm_loads=0,
              http_requests=0, asr_tokens_per_case=64,
              cpu_reference_calls=6, cpu_reference_samples=1000000)
CONTINUATION_LIMITS = dict(LIMITS, gpu_asr_loads=1, cases=3, cpu_reference_calls=3)
LIBRARY = Path('/usr/lib/aarch64-linux-gnu/libSoundTouchDll.so.1')


class Reference:
    def __init__(self):
        self.api = ct.CDLL(str(LIBRARY))
        self.pointer = ct.POINTER(ct.c_float)
        signatures = {
            'createInstance': (ct.c_void_p, []),
            'destroyInstance': (None, [ct.c_void_p]),
            'getVersionString': (ct.c_char_p, []),
            'setChannels': (ct.c_int, [ct.c_void_p, ct.c_uint]),
            'setSampleRate': (ct.c_int, [ct.c_void_p, ct.c_uint]),
            'setTempo': (None, [ct.c_void_p, ct.c_float]),
            'putSamples': (ct.c_int, [ct.c_void_p, self.pointer, ct.c_uint]),
            'flush': (ct.c_int, [ct.c_void_p]),
            'receiveSamples': (ct.c_uint, [ct.c_void_p, self.pointer, ct.c_uint]),
        }
        for name, (result, args) in signatures.items():
            function = getattr(self.api, 'soundtouch_'+name)
            function.restype, function.argtypes = result, args
            setattr(self, name, function)
        assert self.getVersionString() == b'2.3.2'

    def scale(self, pcm, speed):
        import numpy as np
        handle = self.createInstance()
        assert handle
        try:
            assert self.setChannels(handle, 1) == 1
            assert self.setSampleRate(handle, 24000) == 1
            self.setTempo(handle, speed)
            assert self.putSamples(handle, pcm.ctypes.data_as(self.pointer), pcm.size) == 1
            assert self.flush(handle) == 1
            output = np.empty(LIMITS['cpu_reference_samples'], '<f4')
            count = self.receiveSamples(handle, output.ctypes.data_as(self.pointer), output.size)
            assert 0 < count < output.size
            assert self.receiveSamples(handle, output[count:].ctypes.data_as(self.pointer), 1) == 0
            output = output[:count].copy()
            assert np.isfinite(output).all() and float(np.linalg.norm(output)) > 0
            return output
        finally:
            self.destroyInstance(handle)


def diagnostic_check(text, receipt, expected):
    try:
        result = content_check(text, expected)
    except AssertionError as error:
        result = dict(expected=expected['text'], transcript=text,
                      passed=False, error=str(error))
    result.update(eos=receipt['eos'], generated_tokens=receipt['generated_tokens'])
    result['passed'] = result['passed'] and receipt['eos'] and 0 < receipt['generated_tokens'] <= 64
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'output', 'asr-binary', 'asr-assets'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.environ.get('PYTHONOPTIMIZE')
    assert not any(k.startswith('Q4T_ABLATE_') for k in os.environ)
    plan = json.loads(args.plan.read_text())
    continuation = plan['schema'] == 'q4t-tempo-oracle-diagnosis-continuation-v1'
    assert continuation or plan['schema'] == 'q4t-tempo-oracle-diagnosis-v1'
    limits = CONTINUATION_LIMITS if continuation else LIMITS
    assert plan['limits'] == limits
    assert [v['utterance'] for v in plan['utterances']] == ([1] if continuation else [0, 1])
    assert plan['defaults'] == defaults()
    assert plan['library_sha256'] == digest(LIBRARY)
    assert plan['asr_binary_sha256'] == digest(args.asr_binary)
    assert plan['driver_sources'] == {
        n: digest(Path(__file__).parent/n) for n in plan['driver_sources']}
    args.output.mkdir(parents=True, exist_ok=False)
    child_env = dict(os.environ)
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    os.environ['USE_TORCH'] = '0'
    import numpy as np
    import soxr
    import transformers
    from transformers import Qwen2TokenizerFast, WhisperFeatureExtractor
    assert transformers.__version__ == '4.57.6' and soxr.__version__ == '0.5.0.post1'
    started = time.monotonic()
    result = dict(status='RUNNING', limits=limits, runs=[], comparisons=[],
                  defaults_before=defaults(), acceptance_granted=False)
    try:
        reference = Reference()
        tokenizer = Qwen2TokenizerFast.from_pretrained(args.asr_assets, local_files_only=True)
        extractor = WhisperFeatureExtractor.from_dict(json.loads(
            (args.asr_assets/'preprocessor_config.json').read_text()))
        template = tokenizer.encode(
            '<|im_start|>system\n<|im_end|>\n<|im_start|>user\n'
            '<|audio_start|><|audio_pad|><|audio_end|><|im_end|>\n'
            '<|im_start|>assistant\n', add_special_tokens=False)
        assert template.index(151676) == 9 and template.count(151676) == 1
        for item in plan['utterances']:
            group = item['utterance']
            comparison_start = len(result['comparisons'])
            for name in ('original', 'candidate'):
                assert digest(Path(item[name])) == item[name+'_sha256']
            original = np.fromfile(item['original'], '<f4')
            candidate = np.fromfile(item['candidate'], '<f4')
            assert original.size == (76800 if group == 0 else 74880)
            assert candidate.size == original.size//4
            fast = reference.scale(original, 4)
            signals = [fast, reference.scale(candidate, 0.25),
                       reference.scale(fast, 0.25)]
            names = ['reference_4x', 'native_4x_slowed_back', 'reference_4x_slowed_back']
            rows = []
            for i, (pcm, name) in enumerate(zip(signals, names)):
                directory = args.output/f'case-{group}-{i}'
                directory.mkdir(exist_ok=False)
                pcm.tofile(directory/'pcm24.f32')
                signal = soxr.resample(pcm, 24000, 16000, quality='HQ').astype('<f4')
                mel = extractor._np_extract_fbank_features(signal[None, :], 'cpu')[0]
                frames = signal.size//160
                assert mel.shape == (128, frames) and 0 < frames <= 3000
                assert np.isfinite(signal).all() and np.isfinite(mel).all()
                tokens = frames//100*13 + ((frames % 100)+7)//8
                row = dict(frames=frames, samples=int(signal.size), audio_tokens=tokens,
                           audio_start=9, max_new_tokens=64,
                           tokens=template[:9]+[151676]*tokens+template[10:])
                for label, values in (('pcm', signal), ('mel', mel)):
                    path = directory/(label+'.f32')
                    values.astype('<f4').tofile(path)
                    row[label], row[label+'_sha256'] = str(path.absolute()), digest(path)
                rows.append(row)
                result['comparisons'].append(dict(utterance=group, kind=name,
                    samples24=int(pcm.size), audio_tokens=tokens))
            asr_plan = args.output/f'plan-{group}.json'
            asr_plan.write_text(json.dumps(dict(cases=rows), indent=2)+'\n')
            command = [str(args.asr_binary.absolute()), str(args.asr_assets.absolute()),
                       str(asr_plan.absolute()), str(args.output.absolute()/f'asr-{group}')]
            step = time.monotonic()
            with (args.output/f'asr-{group}.log').open('wb') as log:
                child = subprocess.run(command, cwd=ROOT, env=child_env, pass_fds=(lease,),
                    stdout=log, stderr=subprocess.STDOUT,
                    timeout=min(600, LIMITS['seconds']-(time.monotonic()-started)))
            result['runs'].append(dict(command=command, exit_code=child.returncode,
                                       seconds=time.monotonic()-step))
            assert child.returncode == 0
            for i in range(3):
                directory = args.output/f'asr-{group}'/f'case-{i}'
                receipt = json.loads((directory/'result.json').read_text())
                result['comparisons'][comparison_start+i].update(
                    diagnostic_check((directory/'text.txt').read_text(), receipt, CASES[group]))
        result['status'] = 'DIAGNOSIS_COMPLETE_PENDING_LEASE'
    except Exception as error:
        result.update(status='FAILED_DIAGNOSIS', error=str(error))
    finally:
        result['defaults_unchanged'] = defaults() == result['defaults_before']
        if not result['defaults_unchanged']:
            result.update(status='FAILED_DIAGNOSIS', error='protected default changed')
        result['seconds'] = time.monotonic()-started
        result['artifacts'] = {str(p.relative_to(args.output)): digest(p)
                              for p in sorted(args.output.rglob('*')) if p.is_file()}
        (args.output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: result[k] for k in ('status', 'seconds', 'defaults_unchanged')}))
    return 0 if result['status'].startswith('DIAGNOSIS_COMPLETE') else 1


if __name__ == '__main__':
    raise SystemExit(main())
