"""Generate six synthetic CPU-only Qwen Base speaker frontend references.

Extract exact official definitions with AST; omit unrelated imports and cache
decorators only. No production Python dependency or model construction.
"""
import __future__
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import time
import warnings


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def definitions(path, expected_sha, names, scope, float64_equations=False):
    assert digest(path) == expected_sha, str(path)
    tree = ast.parse(path.read_text(), filename=str(path))
    # The conversion source has overload stubs before the concrete definitions.
    selected = {n.name: n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name in names}
    assert selected.keys() == set(names)
    for node in selected.values():
        node.decorator_list = []
    if float64_equations and 'mel_spectrogram' in selected:
        # The explicit .float() otherwise prevents float64 operands. Only
        # precision changes; every equation and constant remains unchanged.
        calls = [n for n in ast.walk(selected['mel_spectrogram'])
                 if isinstance(n, ast.Attribute) and n.attr == 'float']
        assert len(calls) == 1
        calls[0].attr = 'double'
    fragment = ast.Module(body=list(selected.values()), type_ignores=[])
    exec(compile(fragment, str(path), 'exec',
                 flags=__future__.annotations.compiler_flag), scope)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qwen-source', type=Path, required=True)
    parser.add_argument('--librosa-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--float64-equations', action='store_true')
    args = parser.parse_args()
    assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''
    import numpy as np
    import torch
    assert torch.__version__ == '2.14.0+cpu'
    assert np.__version__ == '2.5.2'
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    if args.float64_equations:
        torch.set_default_dtype(torch.float64)
    scope = dict(torch=torch, np=np, warnings=warnings, ParameterError=ValueError)
    sources = [
        (args.librosa_source / 'convert.py',
         '3f0679eeb5706771797ddacc4174bfacd1146abae78e8a583d97d6aa4391985f',
         ['hz_to_mel', 'mel_to_hz', 'fft_frequencies', 'mel_frequencies']),
        (args.librosa_source / 'filters.py',
         'df2914ab2262a5b72e59dba320e35fb934a0825c314c48a2542ade8c23116c4e',
         ['mel']),
        (args.qwen_source,
         '25c42656bcf810f06ef6bc1839bd7083f3c8cfedac3a147c4060b4262b1c96a0',
         ['dynamic_range_compression_torch', 'mel_spectrogram']),
    ]
    for path, identity, names in sources:
        definitions(path, identity, names, scope, args.float64_equations)
    scope['librosa_mel_fn'] = scope['mel']
    impulse = np.zeros(385, dtype=np.float32)
    impulse[0], impulse[193], impulse[-1] = 0.8, -0.4, 0.3
    tone_t = np.arange(2400) / 24000
    mixture_t = np.arange(5001) / 24000
    tail_t = np.arange(24017) / 24000
    waveforms = {
        'silence': np.zeros(512, dtype=np.float32),
        'impulse': impulse,
        'tone': (0.5 * np.sin(2 * np.pi * 440 * tone_t)).astype(np.float32),
        'mixture': (0.2 * np.sin(2 * np.pi * 123 * mixture_t) +
                    0.1 * np.sin(2 * np.pi * 6197 * mixture_t)).astype(np.float32),
        'noise': np.random.default_rng(421).uniform(-0.4, 0.4, 1997).astype(np.float32),
        'tail': (0.3 * np.sin(2 * np.pi * (220 * tail_t + 1850 * tail_t**2)) +
                 0.02 * tail_t).astype(np.float32),
    }
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    records = []
    with torch.inference_mode():
        for name, pcm in waveforms.items():
            signal = torch.from_numpy(pcm)[None, :]
            if args.float64_equations:
                signal = signal.double()
            mel = scope['mel_spectrogram'](signal,
                n_fft=1024, num_mels=128, sampling_rate=24000, hop_size=256,
                win_size=1024, fmin=0, fmax=12000, center=False)
            assert mel.shape == (1, 128, len(pcm) // 256)
            assert torch.isfinite(mel).all()
            row = dict(name=name, samples=len(pcm), frames=mel.shape[2])
            for suffix, values in [('pcm', pcm), ('mel', mel[0].numpy())]:
                raw = values.astype('<f4').tobytes(order='C')
                path = args.output / (name + '-' + suffix + '.f32')
                path.write_bytes(raw)
                row[suffix] = dict(file=path.name, bytes=len(raw), sha256=digest(path))
            records.append(row)
    assert not torch.cuda.is_initialized()
    value = dict(status='PASS_CPU_REFERENCE_GENERATION_ONLY', records=records,
        torch=torch.__version__, numpy=np.__version__, seconds=time.monotonic()-started,
        cuda_initialized=False, model_constructions=0,
        maximum_absolute_logmel_error=1e-4,
        precision='float64_equations' if args.float64_equations else 'official_float32',
        sources=[dict(file=str(p), sha256=s, functions=n) for p,s,n in sources],
        generator_sha256=digest(Path(__file__)),
        adaptations=['AST extraction; unrelated imports omitted',
                     'Memoization decorators omitted; calculations unchanged',
                     'Postponed type annotations; only concrete overload definitions',
                     'Explicit CPU-only PyTorch; source PCM is FP32'])
    if args.float64_equations:
        value['adaptations'].append(
            'Float64 operands/window/FFT/magnitude/reduction/log; official F32 '
            'mel filter values cast to float64; final output stored as F32')
    (args.output / 'manifest.json').write_text(json.dumps(value, indent=2)+'\n')
    print(json.dumps(dict(cases=len(records), seconds=value['seconds'], gpu_actions=0)))


if __name__ == '__main__':
    main()
