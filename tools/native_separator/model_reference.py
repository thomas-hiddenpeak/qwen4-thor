"""Fixed original SepFormer definitions on CPU; unrelated imports omitted."""
import __future__
import argparse
import ast
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import time
from types import SimpleNamespace


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['source', 'checkpoints', 'plan', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
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
    plan = json.loads(args.plan.read_bytes())
    receipt = json.loads((args.source / 'code-receipt-02.json').read_bytes())
    assert receipt['revision'] == 'fc2eabb7416b0ae68b3baaadb39972ea1d153985'
    fixed = {r['path']: r['sha256'] for r in receipt['files']}
    tree = json.loads((args.source / 'upstream-tree.json').read_bytes())
    blobs = {r['path']: r['sha'] for r in tree['tree'] if r['type'] == 'blob'}
    sb = SimpleNamespace(nnet=SimpleNamespace())
    scope = dict(torch=torch, nn=nn, F=F, math=math, copy=copy, sb=sb, EPS=1e-8)
    identities = {}

    def extract(name, names):
        path = args.source / name
        raw = path.read_bytes()
        assert digest(path) == fixed[name]
        assert hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw
                            ).hexdigest() == blobs[name]
        parsed = ast.parse(raw, filename=str(path))
        nodes = [n for n in parsed.body
                 if isinstance(n, (ast.ClassDef, ast.FunctionDef))
                 and n.name in names]
        assert {n.name for n in nodes} == set(names)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path),
                     'exec', flags=__future__.annotations.compiler_flag), scope)
        identities[name] = fixed[name]

    extract('speechbrain/nnet/normalization.py', ['LayerNorm'])
    sb.nnet.normalization = SimpleNamespace(LayerNorm=scope['LayerNorm'])
    extract('speechbrain/nnet/attention.py',
            ['MultiheadAttention', 'PositionalwiseFeedForward'])
    sb.nnet.attention = SimpleNamespace(
        MultiheadAttention=scope['MultiheadAttention'],
        PositionalwiseFeedForward=scope['PositionalwiseFeedForward'])
    extract('speechbrain/lobes/models/transformer/Transformer.py',
            ['PositionalEncoding', 'TransformerEncoderLayer', 'TransformerEncoder'])
    extract('speechbrain/lobes/models/dual_path.py', ['select_norm',
        'GlobalLayerNorm', 'CumulativeLayerNorm', 'Encoder', 'Decoder',
        'SBTransformerBlock', 'Dual_Computation_Block', 'Dual_Path_Model'])
    def transformer():
        return scope['SBTransformerBlock'](num_layers=8, d_model=256, nhead=8,
            d_ffn=1024, dropout=0., use_positional_encoding=True, norm_before=True)
    encoder = scope['Encoder'](kernel_size=16, out_channels=256).eval()
    masknet = scope['Dual_Path_Model'](in_channels=256, out_channels=256,
        intra_model=transformer(), inter_model=transformer(), num_layers=2,
        norm='ln', K=250, num_spks=2, linear_layer_after_inter_intra=False,
        skip_around_intra=True).eval()
    decoder = scope['Decoder'](in_channels=256, out_channels=1,
        kernel_size=16, stride=8, bias=False).eval()
    for name, model in [('encoder', encoder), ('masknet', masknet),
                        ('decoder', decoder)]:
        path = args.checkpoints / (name + '.ckpt')
        assert digest(path) == plan['identities'][str(path)]
        model.load_state_dict(torch.load(path, map_location='cpu',
                                         weights_only=True), strict=True)
    args.output.mkdir(exist_ok=False)
    records = []
    for index, case in enumerate(plan['cases']):
        step = time.monotonic()
        assert step - started < 240
        directory = args.output / f'case-{index}'
        directory.mkdir()
        path = Path(case['pcm'])
        assert digest(path) == case['pcm_sha256']
        pcm = np.fromfile(path, '<f4')
        assert pcm.shape == (case['samples'],) and np.isfinite(pcm).all()
        def save(stage, value):
            array = value.detach().float().contiguous().numpy().astype('<f4')
            assert np.isfinite(array).all()
            (directory / f'stage-{stage}.f32').write_bytes(array.tobytes())
        hooks = []
        for block_index, block in enumerate(masknet.dual_mdl):
            def before_inter(module, inputs, block_index=block_index):
                # Official inter input is [K, S, C], after intra norm+skip.
                save(1 + 2 * block_index, inputs[0])
            def after_block(module, inputs, output, block_index=block_index):
                save(2 + 2 * block_index, output[0].permute(1, 2, 0))
            hooks.append(block.inter_mdl.register_forward_pre_hook(before_inter))
            hooks.append(block.register_forward_hook(after_block))
        with torch.inference_mode():
            encoded = encoder(torch.from_numpy(pcm)[None])
            save(0, encoded[0].transpose(0, 1))
            masks = masknet(encoded)
            save(5, masks[:, 0].transpose(1, 2))
            features = torch.stack([encoded] * 2) * masks
            sources = torch.stack([decoder(features[i]).reshape(-1)
                                   for i in range(2)])
            if sources.shape[1] < len(pcm):
                sources = F.pad(sources, (0, len(pcm) - sources.shape[1]))
            else:
                sources = sources[:, :len(pcm)]
            save(6, sources)
            (directory / 'sources.f32').write_bytes(
                sources.contiguous().numpy().astype('<f4').tobytes())
        for hook in hooks:
            hook.remove()
        records.append(dict(case=index, samples=len(pcm),
                            seconds=time.monotonic() - step))
    assert not torch.cuda.is_initialized()
    result = dict(status='PASS_REFERENCE_EXECUTED', cases=records,
        seconds=time.monotonic()-started, gpu_model_loads=0, cpu_model_loads=1,
        cuda_initialized=False, source_sha256=identities, torch=torch.__version__,
        max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        adaptations=['Exact original AST definitions, unrelated imports omitted',
            'CPU F32 eval/inference mode, no MKLDNN or precision reduction',
            'Hooks only project intermediate tensor layouts for comparison',
            'Original separate_batch zero tail, no separate_file peak normalization'])
    (args.output / 'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k: result[k] for k in
                     ['status', 'seconds', 'cuda_initialized']}))


if __name__ == '__main__':
    main()
