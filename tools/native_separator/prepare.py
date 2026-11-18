"""Freeze actual component inputs without model execution or GPU activity."""
import argparse
import json
from pathlib import Path
import subprocess
import wave

import numpy as np

from tools.native_separator.acceptance import LIMITS, ROOT, THRESHOLDS, digest
from tools.native_separator.convert_weights import SOURCES

VOICES = {
    'spk1_snt1.wav': '2f7315ccf543b6368528b098cd895098c154f222cd55ff7b5f66b50feb383c56',
    'spk2_snt1.wav': '5f3f534ba99f159358c6ee2e4fb5e42b3b9399f37f4e290651405c76095f26f0',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['source', 'checkpoints', 'assets', 'voices', 'binary',
                 'reference-python', 'output']:
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    for name in vars(args):
        path = getattr(args, name)
        # Preserve the venv launch path: resolving its python symlink would
        # discard that environment's CPU-only reference dependencies.
        setattr(args, name, path.absolute() if name == 'reference_python'
                else path.resolve())
    args.output.mkdir(exist_ok=False)
    files = [args.binary, args.reference_python]
    for name, (size, sha) in SOURCES.items():
        path = args.checkpoints/(name+'.ckpt')
        assert path.stat().st_size == size and digest(path) == sha
        files.append(path)
    files += [args.assets/n for n in ['config.json', 'model.safetensors']]
    assert digest(args.assets/'model.safetensors') == (
        '5f5cbbd10eae7be4e56b752cbc0d27f02305dd0e5cdc3d9c115b51881ca52299')
    for folder in ['tools/native_separator', 'include/q4t/audio', 'src/audio']:
        for path in (ROOT/folder).iterdir():
            if path.is_file() and ('separator' in path.name or
                                  folder == 'tools/native_separator'):
                files.append(path)
    files += [ROOT/'tools/native_speaker/acceptance.py',
              ROOT/'src/quant/lt_cache.cpp', ROOT/'src/io/json.cpp',
              ROOT/'src/trace/sha256.cpp', ROOT/'src/runtime/system_memory.cpp']
    files += list((args.source/'speechbrain').rglob('*.py'))
    files += [args.source/n for n in ['code-receipt-02.json',
                                      'upstream-tree.json', 'inference-receipt.json']]
    # This is a recorded fixture transform, not the production resampler.
    indices = np.arange(-32, 33, dtype=np.float64)
    kernel = .45*np.sinc(.45*indices)*np.hanning(65)
    kernel /= kernel.sum()
    sources = np.zeros((2, 32000), dtype='<f4')
    for i, (name, expected) in enumerate(VOICES.items()):
        path = args.voices/name
        assert digest(path) == expected
        files.append(path)
        with wave.open(str(path), 'rb') as audio:
            assert (audio.getnchannels(), audio.getsampwidth(),
                    audio.getframerate()) == (1, 2, 16000)
            raw = np.frombuffer(audio.readframes(audio.getnframes()), '<i2')
        filtered = np.convolve(raw.astype(np.float64)/32768., kernel,
                               mode='same')[::2]
        assert len(filtered)+1600 <= 32000
        sources[i, 1600:1600+len(filtered)] = filtered*.5
    original = args.output/'known-sources.f32'
    original.write_bytes(sources.tobytes()); files.append(original)
    rng = np.random.default_rng(20261118)
    values = [np.zeros(16), rng.normal(0, .02, 257), sources.sum(axis=0),
              rng.normal(0, .02, 64000)]
    cases = []
    for i, samples in enumerate(values):
        pcm = args.output/f'case-{i}.f32'
        pcm.write_bytes(np.asarray(samples, dtype='<f4').tobytes())
        files.append(pcm)
        cases.append(dict(samples=len(samples), pcm=str(pcm), pcm_sha256=digest(pcm)))
    cases[2].update(known_sources=str(original), known_sources_sha256=digest(original))
    plan = dict(format='q4t-native-separator-component-v1',
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                               cwd=ROOT, text=True).strip(),
        limits=LIMITS, thresholds=THRESHOLDS, cases=cases,
        binary=str(args.binary), assets=str(args.assets),
        source=str(args.source), checkpoints=str(args.checkpoints),
        reference_python=str(args.reference_python), output=str(args.output),
        fixture_transform=dict(sample_rate=8000, source_rate=16000,
            taps=65, cutoff_hz=3600, window='Hann', boundary='zero/same',
            decimation='even indices', coefficient_sum=float(kernel.sum()),
            prefix_samples=1600, source_gain=.5, numpy=np.__version__),
        identities={str(p): digest(p) for p in sorted(set(files))},
        command=['/usr/bin/python3', '-B',
                 str(ROOT/'tools/native_separator/acceptance.py'), '--plan',
                 str(args.output/'plan.json')])
    (args.output/'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
    print(json.dumps(dict(output=str(args.output), cases=len(cases), limits=LIMITS)))


if __name__ == '__main__':
    main()
