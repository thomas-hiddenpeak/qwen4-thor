"""One bounded ASR load for four damaged-waveform negative controls."""
import argparse
from array import array
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from acceptance import CASES, ROOT, defaults, digest
from clock_content import check_content, damage_pcm

LIMITS = dict(seconds=600, asr_seconds=180, asr_loads=1, cases=4,
              max_tokens=64, tts_loads=0, main_llm_loads=0, http_requests=0,
              artifact_mib=16, minimum_free_mib=256)
CONTROLS = [(0, 'silence'), (0, 'first_quarter'),
            (1, 'silence'), (1, 'first_quarter')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.environ.get('PYTHONOPTIMIZE')
    assert not any(k.startswith('Q4T_ABLATE_') for k in os.environ)
    plan = json.loads(args.plan.read_bytes())
    assert plan['schema'] == 'q4t-tts-clock-content-owner-decision-v1'
    assert plan['limits'] == LIMITS and plan['controls'] == [list(x) for x in CONTROLS]
    assert plan['owner_decision']['allowed'] is True
    assert plan['owner_decision']['listening_pending'] is True
    assert defaults() == plan['defaults']
    assert all(digest(Path(name)) == value for name, value in plan['sources'].items())
    assert os.environ.get('PYTHONPATH') == plan['reference_pythonpath']
    assert sys.byteorder == 'little'
    disk = os.statvfs(args.output.parent)
    assert disk.f_bavail * disk.f_frsize >= 256 * 1024**2
    args.output.mkdir(exist_ok=False)
    started = time.monotonic()
    result = dict(status='RUNNING', asr_loads=0, tts_loads=0,
                  main_llm_loads=0, http_requests=0, comparisons=[], positives=[],
                  listening_qualified=False, defaults_before=defaults(),
                  plan_sha256=digest(args.plan))
    child_env = dict(os.environ)
    os.environ.update(CUDA_VISIBLE_DEVICES='', USE_TORCH='0')
    try:
        import numpy as np
        import transformers
        from transformers import Qwen2TokenizerFast, WhisperFeatureExtractor
        assert transformers.__version__ == '4.57.6'
        assets = Path(plan['asr_assets'])
        tokenizer = Qwen2TokenizerFast.from_pretrained(assets, local_files_only=True)
        extractor = WhisperFeatureExtractor.from_dict(json.loads(
            (assets / 'preprocessor_config.json').read_text()))
        template = tokenizer.encode(
            '<|im_start|>system\n<|im_end|>\n<|im_start|>user\n'
            '<|audio_start|><|audio_pad|><|audio_end|><|im_end|>\n'
            '<|im_start|>assistant\n', add_special_tokens=False)
        assert template.index(151676) == 9 and template.count(151676) == 1
        # Reconcile unchanged positives before allocating a new model.
        positive_root = Path(plan['positive_run'])
        prior = json.loads((positive_root / 'result.json').read_bytes())
        assert prior['status'] == 'CLOCK_DIAGNOSTIC_COMPLETED_NOT_ACCEPTANCE'
        assert prior['defaults_unchanged'] is True
        assert len(prior['comparisons']) == 4
        guard = json.loads((positive_root.parent / 'lease/exit.json').read_bytes())
        assert guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0
        for index, row in enumerate(prior['comparisons']):
            path = positive_root / 'asr' / f'case-{index}'
            check = check_content((path / 'text.txt').read_text(),
                                  json.loads((path / 'result.json').read_bytes()),
                                  CASES[row['utterance']])
            assert check['passed']
            result['positives'].append(dict(method=row['method'],
                utterance=row['utterance'], reused=True, **check))
        rows = []
        for index, (utterance, kind) in enumerate(CONTROLS):
            item = plan['original_inputs'][utterance]
            path = Path(item['pcm'])
            assert digest(path) == item['pcm_sha256']
            pcm = array('f')
            pcm.frombytes(path.read_bytes())
            assert len(pcm) == item['samples']
            damaged = damage_pcm(pcm, kind)
            signal = np.frombuffer(damaged, dtype='<f4')
            mel = extractor._np_extract_fbank_features(signal[None, :], 'cpu')[0]
            frames = len(signal) // 160
            assert frames == item['frames'] and mel.shape == (128, frames)
            assert np.isfinite(mel).all()
            count = frames * 4 // 100 * 13 + ((frames * 4 % 100) + 7) // 8
            row = dict(frames=frames, samples=len(signal), audio_tokens=count,
                       audio_start=9, max_new_tokens=64,
                       tokens=template[:9] + [151676] * count + template[10:])
            for name, value in (('pcm', signal), ('mel', mel)):
                destination = args.output / f'case-{index}-{name}.f32'
                value.astype('<f4').tofile(destination)
                row[name], row[name + '_sha256'] = str(destination.absolute()), digest(destination)
            rows.append(row)
        asr_plan = args.output / 'asr-plan.json'
        asr_plan.write_text(json.dumps(dict(
            purpose='ASR_CLOCK_DIAGNOSTIC_NOT_PCM_ACCEPTANCE', cases=rows), indent=2) + '\n')
        # Expected text and negative labels are deliberately absent from this file.
        command = [plan['asr_binary'], plan['asr_assets'], str(asr_plan.absolute()),
                   str(args.output.absolute() / 'asr')]
        result.update(command=command, asr_loads=1)
        remaining = LIMITS['seconds'] - (time.monotonic() - started)
        assert remaining > 0
        with (args.output / 'asr.log').open('wb') as log:
            child = subprocess.run(command, cwd=ROOT, env=child_env,
                pass_fds=(lease,), stdout=log, stderr=subprocess.STDOUT,
                timeout=min(remaining, LIMITS['asr_seconds']))
        result['asr_returncode'] = child.returncode
        assert child.returncode == 0, 'ASR execution failed'
        for index, (utterance, kind) in enumerate(CONTROLS):
            path = args.output / 'asr' / f'case-{index}'
            receipt = json.loads((path / 'result.json').read_bytes())
            assert receipt['frames'] == rows[index]['frames'] * 4
            assert receipt['audio_tokens'] == rows[index]['audio_tokens']
            result['comparisons'].append(dict(utterance=utterance, damage=kind,
                **check_content((path / 'text.txt').read_text(), receipt,
                                CASES[utterance], negative=True)))
        assert all(x['passed'] for x in result['comparisons']), 'damaged content falsely accepted'
        result['status'] = 'PASS_CLOCK_CONTENT_PENDING_LEASE_AND_LISTENING'
    except Exception as error:
        result.update(status='FAILED_CLOCK_CONTENT', error=repr(error))
    finally:
        result['defaults_unchanged'] = defaults() == result['defaults_before']
        result['seconds'] = time.monotonic() - started
        result['artifact_bytes'] = sum(p.stat().st_size for p in args.output.rglob('*') if p.is_file())
        if not result['defaults_unchanged'] or result['artifact_bytes'] > 16 * 1024**2:
            result.update(status='FAILED_PROTECTION', error='default or artifact bound')
        result['artifacts'] = {str(p.relative_to(args.output)): digest(p)
                              for p in args.output.rglob('*') if p.is_file()}
        (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('status', 'seconds', 'asr_loads',
        'defaults_unchanged', 'comparisons')}, ensure_ascii=False))
    return 0 if result['status'] == 'PASS_CLOCK_CONTENT_PENDING_LEASE_AND_LISTENING' else 1


if __name__ == '__main__':
    raise SystemExit(main())
