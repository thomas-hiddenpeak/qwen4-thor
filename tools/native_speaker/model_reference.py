"""Exact fixed upstream ECAPA definitions on CPU, without optional pipelines.

Only unrelated imports are omitted; selected AST definitions remain unchanged.
The reference consumes native Fbank operands for isolated model arithmetic and
independently produces official Fbank matrices for the preprocessing check.
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
import resource
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['source', 'mask-source', 'checkpoint', 'plan', 'native', 'output']:
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''
    import numpy as np
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.set_float32_matmul_precision('highest')
    torch.backends.mkldnn.enabled = False
    started = time.monotonic()
    receipt = json.loads((args.source/'source-receipt.json').read_bytes())
    assert receipt['source_revision'] == 'c0241278f93ef02cd5191f03caf4e5f8e1292bf6'
    identities = {row['name']: row['sha256'] for row in receipt['records']}
    scope = dict(torch=torch, nn=nn, F=F, math=math,
                 logger=logging.getLogger(__name__),
                 fwd_default_precision=lambda **unused: lambda fn: fn)
    sources = {}

    def extract(path, names, expected):
        assert digest(path) == expected
        tree = ast.parse(path.read_text(), filename=str(path))
        nodes = [node for node in tree.body
                 if isinstance(node, (ast.ClassDef, ast.FunctionDef))
                 and node.name in names]
        assert {node.name for node in nodes} == set(names)
        fragment = ast.Module(body=nodes, type_ignores=[])
        exec(compile(fragment, str(path), 'exec',
                     flags=__future__.annotations.compiler_flag), scope)
        sources[str(path)] = expected

    extract(args.mask_source, ['length_to_mask'],
            '48e7013220bb8a7f4f073de76124245d5cf0ef20fcf95497f72766dc2ecc0b6f')
    name = 'speechbrain/nnet/CNN.py'
    extract(args.source/name, ['Conv1d', 'get_padding_elem'], identities[name])
    scope['_Conv1d'] = scope['Conv1d']
    name = 'speechbrain/nnet/normalization.py'
    extract(args.source/name, ['BatchNorm1d'], identities[name])
    scope['_BatchNorm1d'] = scope['BatchNorm1d']
    name = 'speechbrain/lobes/models/ECAPA_TDNN.py'
    extract(args.source/name, ['Conv1d', 'BatchNorm1d', 'TDNNBlock',
        'Res2NetBlock', 'SEBlock', 'AttentiveStatisticsPooling',
        'SERes2NetBlock', 'ECAPA_TDNN'], identities[name])
    for name, names in [
        ('speechbrain/processing/features.py',
         ['STFT', 'Filterbank', 'Deltas', 'ContextWindow', 'spectral_magnitude']),
        ('speechbrain/lobes/features.py', ['Fbank'])]:
        extract(args.source/name, names, identities[name])
    assert args.checkpoint.stat().st_size == 83316686
    assert digest(args.checkpoint) == (
        '0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2')
    state = torch.load(args.checkpoint, map_location='cpu', weights_only=True)
    assert len(state) == 231
    model = scope['ECAPA_TDNN'](input_size=80,
        channels=[1024, 1024, 1024, 1024, 3072], lin_neurons=192).eval()
    model.load_state_dict(state, strict=True)
    del state
    feature = scope['Fbank'](n_mels=80).eval()
    args.output.mkdir(exist_ok=False)
    cases = json.loads(args.plan.read_bytes())['cases']
    assert len(cases) == 4
    records = []
    for index, case in enumerate(cases):
        step = time.monotonic()
        assert step - started < 240
        directory = args.output/f'case-{index}'
        directory.mkdir()
        pcm_path = Path(case['pcm'])
        assert digest(pcm_path) == case['pcm_sha256']
        pcm = np.fromfile(pcm_path, '<f4')
        frames = case['frames']
        assert len(pcm) == case['samples'] and frames == 1+len(pcm)//160
        assert 5 <= frames <= 3001
        native = np.fromfile(args.native/f'case-{index}'/'mel.f32', '<f4')
        assert native.shape == (80*frames,) and np.isfinite(native).all()
        hooks = []
        layers = [*model.blocks, model.mfa, model.asp_bn, model.fc]
        assert len(layers) == 7
        for stage, layer in enumerate(layers):
            def save(unused_layer, unused_input, output, stage=stage):
                value = output.detach().float().contiguous().numpy().astype('<f4')
                assert np.isfinite(value).all()
                (directory/f'stage-{stage}.f32').write_bytes(value.tobytes())
            hooks.append(layer.register_forward_hook(save))
        with torch.inference_mode():
            matrix = feature(torch.from_numpy(pcm).unsqueeze(0))
            matrix = matrix - matrix.mean(dim=1, keepdim=True)
            values = matrix[0].transpose(0, 1).contiguous().numpy().astype('<f4')
            assert values.shape == (80, frames) and np.isfinite(values).all()
            (directory/'mel.f32').write_bytes(values.tobytes())
            inputs = torch.from_numpy(native.reshape(80, frames).T.copy())[None]
            output = model(inputs).reshape(192)
            assert torch.isfinite(output).all()
            (directory/'embedding.f32').write_bytes(output.numpy().astype('<f4').tobytes())
        for hook in hooks:
            hook.remove()
        records.append(dict(case=index, frames=frames, seconds=time.monotonic()-step,
            model_input_sha256=digest(args.native/f'case-{index}'/'mel.f32')))
    assert not torch.cuda.is_initialized()
    value = dict(status='PASS_REFERENCE_EXECUTED', cases=records,
        seconds=time.monotonic()-started, cpu_model_loads=1, gpu_model_loads=0,
        source_sha256=sources, cuda_initialized=False, torch=torch.__version__,
        max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        adaptations=['Exact AST definitions; unrelated import graph omitted',
            'CPU float32 precision decorator identity, postponed annotations',
            'Full single-utterance mean-only normalization',
            'Model consumes identical native features; official frontend compared separately',
            'Global embedding normalization/classifier disabled as default encode_batch'],
        checkpoints={str(args.checkpoint): digest(args.checkpoint)})
    (args.output/'result.json').write_text(json.dumps(value, indent=2)+'\n')
    print(json.dumps({k: value[k] for k in ['status', 'seconds', 'cuda_initialized']}))


if __name__ == '__main__':
    main()
