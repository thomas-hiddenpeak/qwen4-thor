#!/usr/bin/env python3
"""Unmodified pinned Qwen/Mimi reference encoder on CPU under the shared lease."""
import argparse
import json
import os
from pathlib import Path
import time
from unittest import mock

from codec_reference import import_official
from model_reference import digest


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
    import transformers.masking_utils as masking
    import transformers.models.mimi.modeling_mimi as mimi
    from safetensors.torch import load_file
    assert transformers.__version__ == '4.57.6'
    assert not torch.cuda.is_initialized()
    torch.set_num_threads(8)
    torch.set_num_interop_threads(1)
    started = time.monotonic()
    plan = json.loads(args.plan.read_text())
    assert digest(Path(mimi.__file__)) == plan['reference_mimi_sha256']
    assert digest(Path(masking.__file__)) == plan['reference_mask_sha256']
    module = import_official(args.official_source)
    assert digest(args.assets / 'model.safetensors') == (
        '836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258')
    config = module.Qwen3TTSTokenizerV2Config(**json.loads(
        (args.assets / 'config.json').read_text()))
    config.encoder_config._attn_implementation = 'eager'
    assert config.encoder_config.use_cache is False
    assert config.encoder_config.use_streaming is False
    encoder = module.Qwen3TTSTokenizerV2Encoder(config.encoder_config)
    weights = load_file(args.assets / 'model.safetensors', device='cpu')
    state = {k.removeprefix('encoder.'): v for k, v in weights.items()
             if k.startswith('encoder.')}
    assert len(state) == 225 and len(weights) == 496
    encoder.load_state_dict(state, strict=True, assign=True)
    del weights, state
    encoder.eval()
    assert all(p.dtype == torch.float32 and p.device.type == 'cpu'
               for p in encoder.parameters())
    # Execute the official wrapper's encode method with its actual encoder.
    # No trained decoder is loaded or called; no reference math is replaced.
    holder = module.Qwen3TTSTokenizerV2Model.__new__(
        module.Qwen3TTSTokenizerV2Model)
    torch.nn.Module.__init__(holder)
    holder.config = config
    holder.encoder = encoder
    holder.encoder_valid_num_quantizers = config.encoder_valid_num_quantizers
    holder.encode_downsample_rate = config.encode_downsample_rate
    assert holder.encoder_valid_num_quantizers == 16
    args.output.mkdir(parents=True, exist_ok=False)
    target = None
    mask_records = []

    def save(name, index, value):
        assert time.monotonic() - started < 600
        assert target is not None and value.dtype == torch.float32
        value = value.detach().squeeze(0).contiguous()
        assert value.ndim == 2
        if value.shape[0] > 94 and name in (
                'encoder-latent', 'encoder-projection', 'encoder-rvq-residual'):
            (target / f'{name}-{index}-2.f32').write_bytes(
                value[94:95].contiguous().numpy().tobytes())
        rows = min(value.shape[0], 8)
        for side, part in enumerate((value[:rows], value[-rows:])):
            (target / f'{name}-{index}-{side}.f32').write_bytes(
                part.contiguous().numpy().tobytes())

    def hook(name, index, transformer=False):
        def capture(child, arguments, value):
            save(name, index, value[0] if transformer else value.transpose(1, 2))
        return capture

    handles = [encoder.encoder.layers[0].register_forward_hook(
                   hook('encoder-input', 0)),
               encoder.encoder.layers[14].register_forward_hook(
                   hook('encoder-seanet', 0)),
               encoder.downsample.register_forward_hook(hook('encoder-latent', 0))]
    for index in range(4):
        handles.append(encoder.encoder.layers[1 + 3 * index].register_forward_hook(
            hook('encoder-residual', index)))
        handles.append(encoder.encoder.layers[3 + 3 * index].register_forward_hook(
            hook('encoder-downsample', index)))
    for index, layer in enumerate(encoder.encoder_transformer.layers):
        handles.append(layer.register_forward_hook(hook('encoder-transformer', index, True)))
    quantizers = [encoder.quantizer.semantic_residual_vector_quantizer,
                  encoder.quantizer.acoustic_residual_vector_quantizer]
    for index, quantizer in enumerate(quantizers):
        handles.append(quantizer.input_proj.register_forward_hook(
            hook('encoder-projection', index)))

    def attention_mask(child, arguments, kwargs):
        mask = kwargs['attention_mask']
        rows = kwargs['hidden_states'].shape[1]
        assert mask.shape[-2:] == (rows, rows)
        assert mask[0, 0, -1, 0].item() == 0
        assert mask[0, 0, -1, -1].item() == 0
        if rows > 1:
            assert mask[0, 0, 0, 1].item() < -1e30
        mask_records.append(dict(rows=rows, final_row_sees_first=True,
                                 future_masked=True, sliding_window_applied=False))

    handles.append(encoder.encoder_transformer.layers[0].self_attn.
                   register_forward_pre_hook(attention_mask, with_kwargs=True))

    def quantize(layer, original, index):
        def capture(value):
            indices = original(value)
            save('encoder-rvq-residual', index,
                 (value - layer.decode(indices)).transpose(1, 2))
            return indices
        return capture

    from contextlib import ExitStack
    with ExitStack() as patches, torch.inference_mode():
        # Reference executes all 32 quantizers as the official wrapper does;
        # capture only its first 16 returned codes and corresponding stages.
        layers = [quantizers[0].layers[0], *quantizers[1].layers[:15]]
        for index, layer in enumerate(layers):
            patches.enter_context(mock.patch.object(
                layer, 'encode', quantize(layer, layer.encode, index)))
        for index, item in enumerate(plan['cases']):
            if index != 2:
                continue
            target = args.output / f'case-{index}'
            target.mkdir()
            pcm = Path(item['pcm'])
            assert digest(pcm) == item['sha256']
            value = np.fromfile(pcm, '<f4')
            assert value.size == item['samples']
            tensor = torch.from_numpy(value.copy()).unsqueeze(0)
            encoded = holder.encode(tensor, padding_mask=torch.ones_like(
                tensor, dtype=torch.long), return_dict=True)
            codes = encoded.audio_codes[0]
            frames = (item['samples'] + 1919) // 1920
            assert codes.shape == (frames, 16)
            assert ((codes >= 0) & (codes < 2048)).all()
            (target / 'codes.i32').write_bytes(
                codes.to(torch.int32).contiguous().numpy().tobytes())
            (target / 'result.json').write_text(json.dumps(dict(
                samples=value.size, frames=frames,
                transformer_rows=(value.size + 959) // 960,
                mask=mask_records[-1]), indent=2) + '\n')
    for handle in handles:
        handle.remove()
    assert len(mask_records) == 1 and mask_records[-1]['rows'] > 250
    assert not torch.cuda.is_initialized()
    (args.output / 'result.json').write_text(json.dumps(dict(
        status='PASS_ENCODER_DIAGNOSTIC_REFERENCE_ONLY', cpu_model_loads=1,
        encoder_forwards=1, cases=1, cuda_initialized=False,
        seconds=time.monotonic() - started, masks=mask_records), indent=2) + '\n')


if __name__ == '__main__':
    main()
