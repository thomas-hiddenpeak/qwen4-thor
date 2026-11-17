#!/usr/bin/env python3
"""Pinned official CPU forced-alignment reference, within the shared lease.

As in the ASR reference, encoder eager attention receives its explicit trained
cu_seqlens window mask. No GPU context or Python runtime dependency is added
to the delivered engine. Raw classes are preserved before timestamp repair.
"""
import argparse
import json
import os
from pathlib import Path
import time

from model_reference import SOURCES, digest, import_official


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'official-source', 'plan', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    os.fstat(int(os.environ['Q4T_MANAGED_LEASE_FD']))
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    import numpy as np
    import torch
    import transformers
    assert transformers.__version__ == '4.57.6'
    assert not torch.cuda.is_initialized()
    torch.set_num_threads(8)
    torch.set_num_interop_threads(1)
    started = time.monotonic()
    assert digest(args.assets / 'model.safetensors') == (
        '47831d0e82f96b20e9034dba01a075ee06436654719f6a68289e49f1b65ce0e7')
    module = import_official(args.official_source)
    original = module.Qwen3ASRAudioEncoderLayer.forward

    def masked_forward(self, hidden_states, cu_seqlens,
                       attention_mask=None, **kwargs):
        assert attention_mask is None
        count = hidden_states.shape[0]
        mask = torch.full((1, 1, count, count),
                          torch.finfo(hidden_states.dtype).min,
                          dtype=hidden_states.dtype)
        cuts = cu_seqlens.tolist()
        assert cuts[0] == 0 and cuts[-1] == count
        for first, end in zip(cuts, cuts[1:]):
            assert 0 < end - first <= 104
            mask[:, :, first:end, first:end] = 0
        return original(self, hidden_states, cu_seqlens,
                        attention_mask=mask, **kwargs)

    module.Qwen3ASRAudioEncoderLayer.forward = masked_forward
    model, loading = module.Qwen3ASRForConditionalGeneration.from_pretrained(
        args.assets, dtype=torch.bfloat16, attn_implementation='eager',
        local_files_only=True, trust_remote_code=False, output_loading_info=True)
    assert not any(loading.get(k) for k in (
        'missing_keys', 'unexpected_keys', 'mismatched_keys', 'error_msgs'))
    model.eval()
    thinker = model.thinker
    assert next(model.parameters()).device.type == 'cpu'
    assert tuple(thinker.lm_head.weight.shape) == (5000, 1024)
    assert tuple(thinker.model.embed_tokens.weight.shape) == (152064, 1024)
    assert thinker.lm_head.weight.data_ptr() != (
        thinker.model.embed_tokens.weight.data_ptr())
    assert len(thinker.audio_tower.layers) == 24
    plan = json.loads(args.plan.read_text())
    assert 1 <= len(plan['cases']) <= 2
    args.output.mkdir(exist_ok=False, parents=True)
    records = []
    for index, case in enumerate(plan['cases']):
        assert time.monotonic() - started < 240
        output = args.output / f'case-{index}'
        output.mkdir()

        def save(value, name):
            assert value.dtype == torch.bfloat16 and torch.isfinite(value).all()
            value.detach().contiguous().view(torch.uint16).numpy().tofile(
                output / name)

        assert digest(Path(case['mel'])) == case['mel_sha256']
        mel = torch.from_numpy(np.fromfile(case['mel'], '<f4').reshape(
            128, case['frames'])).bfloat16()
        hook = thinker.audio_tower.layers[0].register_forward_pre_hook(
            lambda layer, inputs: save(inputs[0], 'encoder-input.bf16'))
        try:
            with torch.inference_mode():
                audio = thinker.audio_tower(mel, feature_lens=torch.tensor(
                    [case['frames']], dtype=torch.long)).last_hidden_state
        finally:
            hook.remove()
        assert tuple(audio.shape) == (case['audio_tokens'], 1024)
        save(audio, 'encoder.bf16')
        ids = torch.tensor([case['tokens']])
        with torch.inference_mode():
            embeddings = thinker.model.embed_tokens(ids)
            embeddings[:, 1:1 + audio.shape[0]] = audio
            hidden = thinker.model(inputs_embeds=embeddings,
                attention_mask=torch.ones_like(ids),
                use_cache=False).last_hidden_state
            selected = hidden[ids == 151705]
            assert selected.shape[0] == len(case['words']) * 2
            save(selected, 'selected.bf16')
            logits = thinker.lm_head(selected)
            predicted = logits.argmax(dim=-1).cpu().numpy().astype('<u4')
        save(logits, 'logits.bf16')
        predicted.tofile(output / 'classes.u32')
        record = {'case': index, 'slots': len(predicted)}
        (output / 'result.json').write_text(json.dumps(record) + '\n')
        records.append(record)
        print(json.dumps(record), flush=True)
    assert not torch.cuda.is_initialized()
    result = {'status': 'REFERENCE_COMPLETED_PENDING_COMPARISON',
              'device': 'cpu', 'dtype': 'bfloat16', 'sources': SOURCES,
              'torch': torch.__version__, 'transformers': transformers.__version__,
              'checkpoint_loading': loading,
              'attention': 'official causal decoder; explicit encoder window mask',
              'forwards': len(records), 'cases': records,
              'seconds': time.monotonic() - started}
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
