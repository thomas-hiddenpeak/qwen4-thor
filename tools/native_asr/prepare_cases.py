#!/usr/bin/env python3
"""Prepare fixed official audio clips for qualification; no model/GPU loading."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import wave


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--official-source', type=Path, required=True)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    os.environ['USE_TORCH'] = '0'
    import numpy as np
    import soxr
    import transformers
    from transformers import Qwen2TokenizerFast, WhisperFeatureExtractor

    source = args.official_source / 'utils.py'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        'b37fb4140e6d9be85f2ecb1552ed16136eb98a57f44b402f8b0b213cb6df2a82')
    tokenizer = Qwen2TokenizerFast.from_pretrained(args.assets,
                                                   local_files_only=True)
    template = json.loads((args.assets / 'chat_template.json').read_text())
    extractor = WhisperFeatureExtractor.from_dict(json.loads(
        (args.assets / 'preprocessor_config.json').read_text()))
    args.output.mkdir(parents=True, exist_ok=False)
    cases = []
    audio_hashes = {
        'en': 'f9b4440ac8393e47c14a6240e9739dea09b645bb1592b8f2dd48feb9666cea7f',
        'zh': '46dbc998c9d1d48111267c40741dd3200f2e5bcf4075f8c4c97f4451160dce50',
    }
    for language in ('zh', 'en'):
        path = args.audio / f'asr_{language}.wav'
        assert hashlib.sha256(path.read_bytes()).hexdigest() == audio_hashes[language]
        # The two fixed public fixtures are ordinary mono PCM WAV. Development
        # preparation uses stdlib PCM decoding and explicit soxr HQ, following
        # the official mono/resample/range-normalize order. It does not certify
        # the future native service codec or require librosa at runtime.
        with wave.open(str(path), 'rb') as wav:
            assert wav.getnchannels() == 1 and wav.getcomptype() == 'NONE'
            rate, width = wav.getframerate(), wav.getsampwidth()
            raw = wav.readframes(wav.getnframes())
        if width == 2:
            pcm = np.frombuffer(raw, '<i2').astype(np.float32) / 32768.0
        else:
            assert width == 3
            values = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
            values = values[:, 0] | (values[:, 1] << 8) | (values[:, 2] << 16)
            values = (values ^ (1 << 23)) - (1 << 23)
            pcm = values.astype(np.float32) / 8388608.0
        if rate != 16000:
            assert rate == 48000
            pcm = soxr.resample(pcm, rate, 16000, quality='HQ')
        peak = np.max(np.abs(pcm))
        if peak > 1:
            pcm = pcm / peak
        pcm = np.clip(pcm, -1, 1).astype(np.float32)
        assert 160 <= len(pcm) <= 480000 and np.isfinite(pcm).all()
        features = extractor._np_extract_fbank_features(pcm[None, :], 'cpu')[0]
        assert features.shape == (128, len(pcm) // 160)
        frames = features.shape[1]
        audio_tokens = frames // 100 * 13 + (frames % 100 + 7) // 8
        prompt = tokenizer.apply_chat_template([
            {'role': 'system', 'content': ''},
            {'role': 'user', 'content': [{'type': 'audio', 'audio': ''}]}],
            chat_template=template['chat_template'],
            add_generation_prompt=True, tokenize=False)
        ids = tokenizer.encode(prompt, add_special_tokens=False)
        assert ids.count(151676) == 1
        audio_start = ids.index(151676)
        ids[audio_start:audio_start + 1] = [151676] * audio_tokens
        case = {'language': language, 'frames': frames,
                'samples': len(pcm), 'audio_tokens': audio_tokens,
                'tokens': ids, 'audio_start': audio_start,
                'prompt': prompt, 'max_new_tokens': 128}
        for kind, values in [('pcm', pcm), ('mel', features)]:
            data = values.astype('<f4').tobytes(order='C')
            destination = (args.output / f'{language}-{kind}.f32').resolve()
            destination.write_bytes(data)
            case[kind] = str(destination)
            case[kind + '_sha256'] = hashlib.sha256(data).hexdigest()
        cases.append(case)
    torch = sys.modules.get('torch')
    assert torch is None or not torch.cuda.is_initialized()
    result = {'status': 'PREPARED_CPU_ONLY', 'cases': cases,
              'official_revision': '7c6daf77a2421100f5fb066495372c00129d39ff',
              'numpy': np.__version__, 'transformers': transformers.__version__,
              'soxr': soxr.__version__,
              'audio_sha256': audio_hashes, 'cuda_initialized': False,
              'decoder': 'stdlib PCM WAV + explicit soxr HQ; runtime codec not qualified'}
    (args.output / 'plan.json').write_text(json.dumps(
        result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': result['status'],
                      'frames': [x['frames'] for x in cases],
                      'audio_tokens': [x['audio_tokens'] for x in cases]}))


if __name__ == '__main__':
    main()
