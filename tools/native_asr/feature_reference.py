#!/usr/bin/env python3
"""Generate bounded, synthetic CPU ASR feature fixtures; never load a model."""
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preprocessor', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    os.environ['USE_TORCH'] = '0'
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    import numpy as np
    import transformers
    from transformers import WhisperFeatureExtractor
    from transformers import audio_utils

    config = json.loads(args.preprocessor.read_text())
    assert config['feature_size'] == 128 and config['n_fft'] == 400
    assert config['hop_length'] == 160 and config['dither'] == 0
    extractor = WhisperFeatureExtractor.from_dict(config)
    impulse = np.zeros(321, dtype=np.float32)
    impulse[0], impulse[157], impulse[-1] = 0.8, -0.4, 0.3
    tone_t = np.arange(8000) / 16000
    mix_t = np.arange(16000) / 16000
    tail_t = np.arange(16179) / 16000
    waveforms = {
        'silence': np.zeros(1600, dtype=np.float32),
        'impulse': impulse,
        'tone': (0.5 * np.sin(2 * np.pi * 440 * tone_t)).astype(np.float32),
        'mixture': (0.2 * np.sin(2 * np.pi * 123 * mix_t) +
                    0.1 * np.sin(2 * np.pi * 3197 * mix_t)).astype(np.float32),
        'noise': np.random.default_rng(421).normal(0, 0.2, 2563).astype(np.float32),
        'tail': (0.3 * np.sin(2 * np.pi * (220 * tail_t + 850 * tail_t**2)) +
                 0.02 * tail_t).astype(np.float32),
    }
    args.output.mkdir(parents=True, exist_ok=False)
    cases = []
    for name, pcm in waveforms.items():
        features = extractor._np_extract_fbank_features(pcm[None, :], 'cpu')[0]
        assert features.shape == (128, len(pcm) // 160)
        assert np.isfinite(features).all()
        row = {'name': name, 'samples': len(pcm), 'frames': features.shape[1]}
        for suffix, values in [('pcm', pcm), ('mel', features)]:
            data = values.astype('<f4').tobytes(order='C')
            filename = f'{name}-{suffix}.f32'
            (args.output / filename).write_bytes(data)
            row[suffix] = {'file': filename, 'bytes': len(data),
                           'sha256': hashlib.sha256(data).hexdigest()}
        cases.append(row)
    torch = sys.modules.get('torch')
    assert torch is None or not torch.cuda.is_initialized()
    sources = [Path(inspect.getfile(WhisperFeatureExtractor)),
               Path(inspect.getfile(audio_utils)), Path(__file__)]
    result = {'status': 'PASS_CPU_REFERENCE_GENERATION_ONLY',
              'numpy': np.__version__, 'transformers': transformers.__version__,
              'sources': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sources},
              'preprocessor_sha256': hashlib.sha256(
                  args.preprocessor.read_bytes()).hexdigest(),
              'model_actions': 0, 'cuda_initialized': False,
              'maximum_absolute_error': 1e-5, 'cases': cases}
    (args.output / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'status': result['status'], 'cases': len(cases),
                      'model_actions': 0, 'cuda_initialized': False}))


if __name__ == '__main__':
    main()
