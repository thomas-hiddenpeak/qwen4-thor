"""Pinned SpeechBrain CPU feature reference without importing optional pipelines.

Extract unmodified STFT/Filterbank/Fbank/Deltas/ContextWindow definitions by
AST. The precision decorator is an identity for this explicitly float32 CPU
run. Single unpadded utterances use the equivalent full-time sentence mean.
No inference model is constructed and no production dependency is installed.
"""
import __future__
import argparse
import ast
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''
    root = Path(__file__).resolve().parents[2]
    import numpy as np
    import torch
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    scope = dict(torch=torch, math=math, logger=logging.getLogger(__name__),
                 fwd_default_precision=lambda **unused: lambda fn: fn)
    receipt = json.loads((args.source/'source-receipt.json').read_bytes())
    assert receipt['source_revision'] == 'c0241278f93ef02cd5191f03caf4e5f8e1292bf6'
    identities = {r['name']:r['sha256'] for r in receipt['records']}
    definitions = [
        ('speechbrain/processing/features.py',
         {'STFT','Filterbank','Deltas','ContextWindow','spectral_magnitude'}),
        ('speechbrain/lobes/features.py', {'Fbank'})]
    for name, requested in definitions:
        path = args.source/name
        assert digest(path) == identities[name]
        tree = ast.parse(path.read_text(), filename=str(path))
        nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))
                 and n.name in requested]
        assert {n.name for n in nodes} == requested
        fragment = ast.Module(body=nodes, type_ignores=[])
        exec(compile(fragment, str(path), 'exec',
                     flags=__future__.annotations.compiler_flag), scope)
    args.output.mkdir(exist_ok=False)
    started = time.monotonic()
    feature = scope['Fbank'](n_mels=80).eval()
    records = []
    for name in ['silence','impulse','tone','mixture','noise','tail']:
        pcm_path = root/'tests/fixtures/asr/features'/(name+'-pcm.f32')
        raw = pcm_path.read_bytes()
        pcm = np.frombuffer(raw, dtype='<f4').copy()
        with torch.inference_mode():
            mel = feature(torch.from_numpy(pcm).unsqueeze(0))
            normalized = mel - mel.mean(dim=1, keepdim=True)
        assert normalized.shape == (1, 1+len(pcm)//160, 80)
        values = normalized[0].transpose(0, 1).contiguous().numpy().astype('<f4')
        output = args.output/(name+'-mel.f32')
        output.write_bytes(values.tobytes())
        records.append(dict(name=name, pcm_sha256=digest(pcm_path),
            frames=values.shape[1], mel_sha256=digest(output)))
    assert not torch.cuda.is_initialized()
    value = dict(source_revision=receipt['source_revision'], records=records,
        source_sha256={n:identities[n] for n,_ in definitions},
        torch=torch.__version__, numpy=np.__version__, seconds=time.monotonic()-started,
        cuda_initialized=False, model_constructions=0,
        adaptations=['AST extraction of exact standalone definitions; unrelated import graph omitted',
                     'No-op precision decorator: CPU float32, no autocast',
                     'Postponed type annotations for unused filter metadata types',
                     'Full single-utterance mean-only normalization; no padded batch'])
    (args.output/'reference.json').write_text(json.dumps(value, indent=2)+'\n')
    print(json.dumps(dict(cases=len(records), seconds=value['seconds'], gpu_actions=0)))


if __name__ == '__main__':
    main()
