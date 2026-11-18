#!/usr/bin/env python3
"""Unmodified pinned official 12 Hz decoder on CPU; no codec encoder claims."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import types
from unittest import mock

from model_reference import digest

SOURCES = {
    'configuration_qwen3_tts_tokenizer_v2':
        '9e30c24394b00cb0366d7da3482b7436468acc1cd3da1a6fe614a1d34653a5e3',
    'modeling_qwen3_tts_tokenizer_v2':
        '844e8dd8c0182ef9c6463c874631c22ef3c5a4fd1899dd657016164cc5379628'}


def import_official(directory):
    package = types.ModuleType('q4t_codec_reference')
    package.__path__ = [str(directory)]
    sys.modules[package.__name__] = package
    for name, expected in SOURCES.items():
        path = directory / (name + '.py')
        assert digest(path) == expected
        spec = importlib.util.spec_from_file_location(package.__name__ + '.' + name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'official-source', 'plan', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    os.fstat(int(os.environ['Q4T_MANAGED_LEASE_FD']))
    assert not os.environ.get('PYTHONOPTIMIZE')
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    import numpy as np
    import torch
    import transformers
    from safetensors.torch import load_file
    assert transformers.__version__ == '4.57.6' and not torch.cuda.is_initialized()
    torch.set_num_threads(8)
    torch.set_num_interop_threads(1)
    started = time.monotonic()
    module = import_official(args.official_source)
    assert digest(args.assets / 'model.safetensors') == (
        '836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258')
    config = module.Qwen3TTSTokenizerV2Config(**json.loads(
        (args.assets / 'config.json').read_text()))
    config.decoder_config._attn_implementation = 'eager'
    decoder = module.Qwen3TTSTokenizerV2Decoder(config.decoder_config)
    weights = load_file(args.assets / 'model.safetensors', device='cpu')
    state = {k.removeprefix('decoder.'): v for k, v in weights.items()
             if k.startswith('decoder.')}
    assert len(weights) == 496 and len(state) == 271
    decoder.load_state_dict(state, strict=True, assign=True)
    del state, weights
    decoder.eval()
    assert all(p.dtype == torch.float32 and p.device.type == 'cpu'
               for p in decoder.parameters())
    assert decoder.pre_transformer.config._attn_implementation == 'eager'
    plan = json.loads(args.plan.read_text())
    assert [len(c['codes']) for c in plan['cases']] == [2, 75, 37]
    assert [c['chunk_frames'] for c in plan['cases']] == [300, 300, 16]
    args.output.mkdir(parents=True, exist_ok=False)
    target = None
    case_target = None
    chunks = []

    def save(name, index, value):
        assert target is not None and value.dtype == torch.float32
        value = value.detach().reshape(-1, value.shape[-1]).contiguous()
        rows = min(value.shape[0], 8)
        for side, part in enumerate((value[:rows], value[-rows:])):
            (target / f'{name}-{index}-{side}.f32').write_bytes(part.numpy().tobytes())

    def start_chunk(child, args):
        nonlocal target
        assert time.monotonic() - started < 600
        target = case_target / f'chunk-{len(chunks)}'
        target.mkdir()
        chunks.append({'input_frames': int(args[0].shape[-1])})

    def hook(name, index, transformer=False):
        def capture(child, args, value):
            value = value.last_hidden_state if transformer else value.transpose(1, 2)
            save(name, index, value)
        return capture

    handles = [decoder.register_forward_pre_hook(start_chunk),
               decoder.pre_conv.register_forward_hook(hook('codec-pre-conv', 0)),
               decoder.pre_transformer.register_forward_hook(
                   hook('codec-transformer', 0, True))]
    for index, up in enumerate(decoder.upsample):
        handles.append(up[1].register_forward_hook(hook('codec-upsample', index)))
    for index in range(4):
        handles.append(decoder.decoder[index+1].register_forward_hook(
            hook('codec-vocoder', index)))
    original_decode = decoder.quantizer.decode

    def quantizer(codes):
        value = original_decode(codes)
        save('codec-rvq', 0, value.transpose(1, 2))
        return value

    results = []
    with torch.inference_mode(), mock.patch.object(decoder.quantizer, 'decode', quantizer):
        for index, case in enumerate(plan['cases']):
            case_target = args.output / f'case-{index}'
            case_target.mkdir()
            chunks = []
            codes = torch.tensor(case['codes'], dtype=torch.long).T.unsqueeze(0)
            assert codes.shape[1] == 16 and ((codes >= 0) & (codes < 2048)).all()
            pcm = decoder.chunked_decode(codes, chunk_size=case['chunk_frames'],
                                          left_context_size=25).flatten()
            assert pcm.numel() == codes.shape[-1] * 1920 and torch.isfinite(pcm).all()
            (case_target / 'pcm.f32').write_bytes(pcm.numpy().tobytes())
            record = {'chunks': chunks, 'samples': pcm.numel()}
            (case_target / 'result.json').write_text(json.dumps(record, indent=2)+'\n')
            results.append(record)
    assert sum(len(r['chunks']) for r in results) == 5
    assert not torch.cuda.is_initialized()
    (args.output / 'result.json').write_text(json.dumps(dict(
        status='PASS_CODEC_REFERENCE_EXECUTION_ONLY', cpu_model_loads=1,
        decoder_forwards=5, cuda_initialized=False, seconds=time.monotonic()-started,
        torch=torch.__version__, transformers=transformers.__version__), indent=2)+'\n')


if __name__ == '__main__':
    main()
